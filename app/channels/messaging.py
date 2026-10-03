"""Twilio SMS/MMS webhook. Owner: Lane B.

POST /twilio/messaging  (configure on the Twilio number)

- Rejects requests without a valid X-Twilio-Signature (unless TWILIO_VALIDATE_SIGNATURES=false).
- Downloads MMS photos to data/media/<MessageSid>_<i>.<ext> and passes the paths to the brain.
- Ignores Twilio retries of a MessageSid it has already handled.
- Never returns a 500: any error becomes a friendly apology text.
- Splits long replies into several texts; followup texts go out after the reply.
"""

import logging
import mimetypes
import threading
from collections import OrderedDict
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.channels.outbound import send_sms
from app.config import get_settings
from app.contracts import TurnRequest
from app.core.turn import handle_turn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/twilio")

SORRY = "Sorry, something went wrong. Please try again."
MAX_SMS_CHARS = 1500  # Twilio's limit is 1600; leave room so carriers don't split mid-word
MAX_MEDIA = 10  # Twilio sends at most 10 attachments per message


# ---------------------------------------------------------------- signatures

def public_url(request: Request) -> str:
    """The URL Twilio signed. ngrok rewrites the host, so rebuild it from PUBLIC_BASE_URL."""
    url = get_settings().public_base_url.rstrip("/") + request.url.path
    return f"{url}?{request.url.query}" if request.url.query else url


def signature_ok(request: Request, params: dict[str, str]) -> bool:
    s = get_settings()
    if not s.twilio_validate_signatures:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    if not (signature and s.twilio_auth_token):
        return False
    return RequestValidator(s.twilio_auth_token).validate(public_url(request), params, signature)


# ---------------------------------------------------------------- retries

class _SeenSids:
    """MessageSids already handled, newest last. In memory: a restart forgets them, which only
    matters if Twilio retries across a restart."""

    def __init__(self, size: int = 5000):
        self._sids: OrderedDict[str, None] = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def first_time(self, sid: str) -> bool:
        with self._lock:
            if sid in self._sids:
                return False
            self._sids[sid] = None
            if len(self._sids) > self._size:
                self._sids.popitem(last=False)
            return True


_seen = _SeenSids()


# ---------------------------------------------------------------- media

def media_dir() -> Path:
    return get_settings().data_dir / "media"


def _extension(content_type: str) -> str:
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else None
    return {".jpe": ".jpg", ".jpeg": ".jpg"}.get(ext, ext) or ".bin"


async def download_media(params: dict[str, str]) -> list[str]:
    """Download MediaUrl0..N with the account's credentials. Skips (and logs) any that fail."""
    s = get_settings()
    sid = params.get("MessageSid") or "unknown"
    try:
        count = min(int(params.get("NumMedia") or 0), MAX_MEDIA)
    except ValueError:
        count = 0
    if not count:
        return []
    media_dir().mkdir(parents=True, exist_ok=True)
    paths = []
    auth = (s.twilio_account_sid, s.twilio_auth_token) if s.twilio_account_sid else None
    async with httpx.AsyncClient(auth=auth, follow_redirects=True, timeout=20) as client:
        for i in range(count):
            url = params.get(f"MediaUrl{i}")
            if not url:
                continue
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                log.exception("could not download media %d of %s", i, sid)
                continue
            content_type = params.get(f"MediaContentType{i}") or resp.headers.get("content-type", "")
            path = media_dir() / f"{sid}_{i}{_extension(content_type)}"
            path.write_bytes(resp.content)
            paths.append(str(path))
    return paths


# ---------------------------------------------------------------- replies

def split_reply(text: str, limit: int = MAX_SMS_CHARS) -> list[str]:
    """Split a long reply into texts of at most `limit` chars, at paragraph, line, sentence, or
    word boundaries where possible."""
    text = text.strip()
    parts = []
    while len(text) > limit:
        window = text[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut < limit // 2:
            cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
            cut = cut + 1 if cut >= limit // 2 else window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def twiml(*messages: str) -> Response:
    resp = MessagingResponse()
    for m in messages:
        resp.message(m)
    return Response(content=str(resp), media_type="application/xml")


def _send_followups(phone: str, texts: list[str]) -> None:
    for text in texts:
        try:
            send_sms(phone, text)
        except Exception:
            log.exception("followup sms to %s failed", phone)


# ---------------------------------------------------------------- webhook

@router.post("/messaging")
async def inbound_message(request: Request, background: BackgroundTasks) -> Response:
    try:
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
    except Exception:
        log.exception("unreadable webhook body")
        return twiml(SORRY)

    if not signature_ok(request, params):
        log.warning("rejected /twilio/messaging request with a bad or missing signature")
        return Response(status_code=403)

    sid = params.get("MessageSid")
    if sid and not _seen.first_time(sid):
        log.info("ignoring Twilio retry of %s", sid)
        return twiml()

    phone = params.get("From", "")
    try:
        media_paths = await download_media(params)
        result = await run_in_threadpool(
            handle_turn, TurnRequest(phone=phone, channel="sms", text=params.get("Body", ""),
                                     media_paths=media_paths))
    except Exception:
        log.exception("turn failed for an inbound sms")
        return twiml(SORRY)

    if result.followup_sms:
        background.add_task(_send_followups, phone, list(result.followup_sms))
    return twiml(*split_reply(result.reply))
