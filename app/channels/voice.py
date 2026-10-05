"""Twilio voice + ConversationRelay.

POST /twilio/voice         (configure on the Twilio number) -> TwiML that connects ConversationRelay
WS   /twilio/voice/relay   ConversationRelay streams transcribed speech here; we reply with text
POST /twilio/voice/status  <Connect action>: called when the relay session ends

Twilio does the speech-to-text and text-to-speech; this file only moves text between Twilio and
the brain (`handle_turn`, same as SMS). Message formats:
https://www.twilio.com/docs/voice/conversationrelay/websocket-messages

Websocket auth: /twilio/voice is signed by Twilio, so it issues a one-time token that goes in the
relay URL. The websocket refuses connections without a valid, unused token.

At connect, the brain gets an empty-text voice turn: "the caller just connected, greet them".
"""

import asyncio
import logging
import secrets
import threading
import time
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse

from app.agent.call import CallController
from app.channels.messaging import signature_ok
from app.channels.outbound import send_sms
from app.config import get_settings
from app.contracts import TurnRequest, TurnResult
from app.core import identity
from app.core.turn import handle_turn
from app.events import log_event
from app.formcall.language import CALL_TAGS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/twilio")

# Brain language code -> BCP-47 tag for Twilio speech. Add a tag here and a <Language> is declared.
LANG_TAGS = {"en": "en-US", "es": "es-US"}
# Every language a call can switch its voice to mid-call (browser calls follow whatever the caller speaks).
SPEECH_TAGS = {**CALL_TAGS, **LANG_TAGS}
GREETING = {"en": "Hi, this is Formline.", "es": "Hola, habla Formline."}
SORRY = {"en": "Sorry, something went wrong. Could you say that again?",
         "es": "Perdón, algo salió mal. ¿Puede repetirlo?"}
FILLER = {"en": "One moment.", "es": "Un momento."}
RECORDING_NOTICE = {"en": " This call is recorded.", "es": " Esta llamada se graba."}
FALLBACK = ("Sorry, Formline can't take calls right now. Please text this number instead. "
            "Lo sentimos, por favor envíe un mensaje de texto a este número.")

DTMF_PAUSE_S = 2.0  # keypad digits are sent to the brain after this pause, on '#', or at 4 digits
TOKEN_TTL_S = 120
FILLER_AFTER_S = 2.5  # say "One moment." if the brain is slower than this
TURN_TIMEOUT_S = 25.0  # give up on a turn after this and apologize, rather than leave dead air


# ---------------------------------------------------------------- one-time relay tokens

_tokens: dict[str, float] = {}
_tokens_lock = threading.Lock()


def issue_token() -> str:
    now = time.monotonic()
    token = secrets.token_urlsafe(18)
    with _tokens_lock:
        for t, exp in list(_tokens.items()):
            if exp < now:
                del _tokens[t]
        _tokens[token] = now + TOKEN_TTL_S
    return token


def redeem_token(token: str) -> bool:
    with _tokens_lock:
        exp = _tokens.pop(token, None)
    return exp is not None and exp >= time.monotonic()


# ---------------------------------------------------------------- helpers

def caller_language(phone: str) -> str:
    """'en' or 'es' from the caller's profile; English for unknown or shared phones that disagree."""
    langs = {p.preferred_language for p in identity.profiles_for_phone(phone)}
    lang = langs.pop() if len(langs) == 1 else "en"
    return lang if lang in LANG_TAGS else "en"


def relay_url(token: str) -> str:
    host = urlparse(get_settings().public_base_url).netloc
    return f"wss://{host}/twilio/voice/relay?t={token}"


def speech_seconds(text: str) -> float:
    """Roughly how long TTS takes to say `text` (about 150 words a minute), capped."""
    return min(15.0, 1.0 + len(text.split()) / 2.5)


def greeting(lang: str) -> str:
    return GREETING[lang] + (RECORDING_NOTICE[lang] if get_settings().record_calls else "")


def start_recording(call_sid: str) -> None:
    """Record the call (both sides, separate channels) for demo backup footage.
    Recordings appear in the Twilio console under Monitor > Logs > Call recordings."""
    s = get_settings()
    if not (call_sid and s.twilio_account_sid and s.twilio_auth_token):
        return
    from twilio.rest import Client

    Client(s.twilio_account_sid, s.twilio_auth_token).calls(call_sid).recordings.create(
        recording_channels="dual")
    log.info("recording call %s", call_sid)


def _xml(resp: VoiceResponse) -> Response:
    return Response(content=str(resp), media_type="application/xml")


def fallback_twiml() -> Response:
    resp = VoiceResponse()
    resp.say(FALLBACK)
    resp.hangup()
    return _xml(resp)


async def _form(request: Request) -> Optional[dict[str, str]]:
    try:
        return {k: str(v) for k, v in (await request.form()).items()}
    except Exception:
        log.exception("unreadable voice webhook body")
        return None


# ---------------------------------------------------------------- webhooks

@router.post("/voice")
async def inbound_call(request: Request) -> Response:
    params = await _form(request)
    if params is None:
        return fallback_twiml()
    if not signature_ok(request, params):
        log.warning("rejected /twilio/voice request with a bad or missing signature")
        return Response(status_code=403)
    try:
        lang = await run_in_threadpool(caller_language, params.get("From", ""))
        resp = VoiceResponse()
        connect = resp.connect(action=get_settings().public_base_url.rstrip("/") + "/twilio/voice/status")
        extra = {}
        if get_settings().voice_autodetect:
            # Recognize whatever language is spoken, whatever the profile says; each prompt reports which.
            extra = {"transcription_language": "multi", "speech_model": "nova-3-general"}
        relay = connect.conversation_relay(url=relay_url(issue_token()), welcome_greeting=greeting(lang),
                                           language=LANG_TAGS[lang], dtmf_detection=True,
                                           interruptible="any", **extra)
        for tag in LANG_TAGS.values():
            relay.language(code=tag)
        return _xml(resp)
    except Exception:
        log.exception("could not build voice TwiML")
        return fallback_twiml()


@router.post("/voice/status")
async def relay_ended(request: Request) -> Response:
    """The relay session ended. If it failed (e.g. our websocket broke), apologize and hang up."""
    params = await _form(request) or {}
    if params and not signature_ok(request, params):
        return Response(status_code=403)
    if params.get("SessionStatus") == "failed":
        log.error("voice relay failed: %s %s", params.get("ErrorCode"), params.get("ErrorMessage"))
        return fallback_twiml()
    resp = VoiceResponse()
    resp.hangup()
    return _xml(resp)


# ---------------------------------------------------------------- relay websocket

class VoiceCall:
    """One ConversationRelay session: Twilio messages in, brain replies out."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.phone = ""
        self.lang = "en"  # brain language code
        self.digits = ""
        self._dtmf_timer: Optional[asyncio.Task] = None
        self._turn_lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()
        self.ended = False
        # Set when the caller has a paired browser: the call drives their browser instead of the form brain.
        self.controller: Optional[CallController] = None
        self._prompt_at: Optional[float] = None
        self._send_lock = asyncio.Lock()

    async def send(self, message: dict) -> None:
        async with self._send_lock:  # agent speech, filler and replies can come from different tasks
            if not self.ended:
                await self.ws.send_json(message)

    async def run(self) -> None:
        try:
            while not self.ended:
                await self.handle(await self.ws.receive_json())
        except WebSocketDisconnect:
            pass
        finally:
            if self._dtmf_timer:
                self._dtmf_timer.cancel()
            if self.controller:
                await self.controller.close()

    async def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "setup":
            self.phone = msg.get("from", "")
            self.lang = await run_in_threadpool(caller_language, self.phone)
            if get_settings().record_calls:
                self._in_background(run_in_threadpool(start_recording, msg.get("callSid", "")))
            self.controller = await CallController.for_caller(self.phone, channel="voice", say=self.say_text,
                                                              end=self.end_call, on_language=self.switch_language)
            if self.controller:
                await self.controller.start()
            else:
                await self.turn("", timed=False)  # let the brain greet (or say "welcome back")
        elif kind == "prompt":
            if msg.get("last", True) and msg.get("voicePrompt", "").strip():
                log.info("caller speech tagged %s by the recognizer", msg.get("lang"))
                if self.controller:
                    self._prompt_at = time.perf_counter()
                    # returns at once; work continues
                    await self.controller.on_utterance(msg["voicePrompt"], hint=msg.get("lang"))
                else:
                    await self.turn(msg["voicePrompt"], hint=msg.get("lang"))
        elif kind == "dtmf":
            await self.digit(str(msg.get("digit", "")))
        elif kind == "interrupt":
            log.info("caller interrupted after %s ms", msg.get("durationUntilInterruptMs"))
        elif kind == "error":
            log.error("ConversationRelay error: %s", msg.get("description"))

    async def digit(self, d: str) -> None:
        """Collect keypad digits (handy for the PIN); flush on '#', at 4 digits, or after a pause."""
        if self._dtmf_timer:
            self._dtmf_timer.cancel()
            self._dtmf_timer = None
        if d != "#":
            self.digits += d
        if d == "#" or len(self.digits) >= 4:
            await self.flush_digits()
        elif self.digits:
            self._dtmf_timer = asyncio.create_task(self._flush_after_pause())

    async def _flush_after_pause(self) -> None:
        await asyncio.sleep(DTMF_PAUSE_S)
        self._dtmf_timer = None
        await self.flush_digits()

    async def flush_digits(self) -> None:
        digits, self.digits = self.digits, ""
        if digits and self.controller:
            await self.controller.on_utterance(digits)
        elif digits:
            await self.turn(digits)

    async def say_text(self, text: str) -> None:
        """Speak one line now (browser-agent calls send several per caller turn: status, question, result)."""
        if self._prompt_at is not None:
            log_event("voice_latency", phone=self.phone, channel="voice",
                      ms=round((time.perf_counter() - self._prompt_at) * 1000))
            self._prompt_at = None
        self._last_spoken = text
        await self.send({"type": "text", "token": text, "last": True})

    async def end_call(self) -> None:
        await asyncio.sleep(speech_seconds(getattr(self, "_last_spoken", "")))
        await self.send({"type": "end"})
        self.ended = True

    async def turn(self, text: str, *, timed: bool = True, hint: Optional[str] = None) -> None:
        async with self._turn_lock:
            if self.ended:
                return
            started = time.perf_counter()
            brain = asyncio.ensure_future(run_in_threadpool(
                handle_turn, TurnRequest(phone=self.phone, channel="voice", text=text, language_hint=hint)))
            result = await self._await_brain(brain)
            await self.speak(result)
            if timed:
                log_event("voice_latency", phone=self.phone, channel="voice",
                          ms=round((time.perf_counter() - started) * 1000))
            for text_msg in result.followup_sms:
                self._in_background(run_in_threadpool(send_sms, self.phone, text_msg))
            if result.end_call:
                await asyncio.sleep(speech_seconds(result.reply))  # "end" cuts speech off
                await self.send({"type": "end"})
                self.ended = True

    async def _await_brain(self, brain: asyncio.Future) -> TurnResult:
        """The brain's result. Says "One moment." if it's slow; apologizes if it fails or takes
        longer than TURN_TIMEOUT_S, so the caller never sits in silence."""
        sorry = TurnResult(reply=SORRY.get(self.lang, SORRY["en"]), language=self.lang)
        try:
            return await asyncio.wait_for(asyncio.shield(brain), FILLER_AFTER_S)
        except asyncio.TimeoutError:
            await self.send({"type": "text", "token": FILLER.get(self.lang, FILLER["en"]), "last": True})
        except Exception:
            log.exception("voice turn failed")
            return sorry
        try:
            return await asyncio.wait_for(brain, max(0.0, TURN_TIMEOUT_S - FILLER_AFTER_S))
        except asyncio.TimeoutError:
            log.error("voice turn took over %ss; apologized instead", TURN_TIMEOUT_S)
        except Exception:
            log.exception("voice turn failed")
        return sorry

    async def switch_language(self, lang: str) -> None:
        """Speak (and, without autodetect, listen) in `lang` from the next line on."""
        if lang not in SPEECH_TAGS or lang == self.lang:
            return
        tag = SPEECH_TAGS[lang]
        switch = {"type": "language", "ttsLanguage": tag}
        if not get_settings().voice_autodetect:  # with "multi" recognition, keep listening for both
            switch["transcriptionLanguage"] = tag
        await self.send(switch)
        self.lang = lang

    async def speak(self, result: TurnResult) -> None:
        await self.switch_language(result.language)
        if result.reply.strip():
            await self.send({"type": "text", "token": result.reply, "last": True})

    def _in_background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(_log_failure)


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("followup sms failed: %s", task.exception())


@router.websocket("/voice/relay")
async def relay(ws: WebSocket) -> None:
    if get_settings().twilio_validate_signatures and not redeem_token(ws.query_params.get("t", "")):
        log.warning("refused a relay websocket without a valid token")
        await ws.close(code=1008)
        return
    await ws.accept()
    await VoiceCall(ws).run()
