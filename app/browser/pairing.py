"""Pairing a Chrome extension with a Formline profile, and checking its credential.

1. The extension sends a phone number (`start_pairing`). Formline sends a 6-digit code to that phone,
   by text or, for landlines, by a voice call that reads it out.
2. The person types the code into the extension (`confirm_pairing`). If the phone has no profile yet,
   they also give a name and choose a 4-digit PIN; if it has several, they say which person they are.
3. The extension gets a random token, shown once. Only its SHA-256 is stored. The token (never the
   phone number) authenticates the extension's websocket.

Calling from that phone number later finds this browser (identification); the PIN still has to be
entered on the call before Formline drives it (authorization).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import select

from app.config import get_settings
from app.core import identity
from app.db import session_scope
from app.events import log_activity
from app.models import BrowserInstallation, PairingRequest

log = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=10)
MAX_CODE_ATTEMPTS = 5
MAX_REQUESTS_PER_PHONE = 3  # per CODE_TTL, so nobody can make Formline spam a number


class PairingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def normalize_phone(raw: str) -> str:
    """'(217) 555-0104' -> '+12175550104'. US numbers without a country code get +1."""
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    raise PairingError("bad_phone", "Enter the phone number you'll call from, with area code.")


def twilio_configured() -> bool:
    s = get_settings()
    return bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_phone_number)


# ---------------------------------------------------------------- step 1: send a code

@dataclass
class PairingStart:
    pairing_id: str
    phone: str
    delivery: str
    dev_code: Optional[str] = None  # only when Twilio isn't set up and dev endpoints are on


def start_pairing(phone_raw: str, delivery: str = "sms") -> PairingStart:
    phone = normalize_phone(phone_raw)
    delivery = "call" if delivery == "call" else "sms"
    since = _now() - CODE_TTL
    with session_scope() as s:
        recent = [r for r in s.exec(select(PairingRequest).where(PairingRequest.phone == phone)).all()
                  if _aware(r.created_at) > since]
        if len(recent) >= MAX_REQUESTS_PER_PHONE:
            raise PairingError("rate_limited", "Too many codes sent to this number. Try again in a few minutes.")
        code = f"{secrets.randbelow(1_000_000):06d}"
        pairing_id = "pr_" + secrets.token_urlsafe(12)
        s.add(PairingRequest(id=pairing_id, phone=phone, code_hash=_hash(pairing_id, code), delivery=delivery,
                             expires_at=_now() + CODE_TTL))
        s.commit()
    try:
        deliver_code(phone, code, delivery)
    except Exception as e:
        # Nothing was sent, so this attempt shouldn't count against the number's limit.
        with session_scope() as s:
            req = s.get(PairingRequest, pairing_id)
            if req is not None:
                s.delete(req)
                s.commit()
        log.warning("pairing code delivery (%s) to %s failed: %s", delivery, phone, e)
        raise PairingError("delivery_failed", _delivery_problem(e, delivery))
    dev = code if (get_settings().dev_endpoints and not twilio_configured()) else None
    return PairingStart(pairing_id, phone, delivery, dev)


def _delivery_problem(error: Exception, delivery: str) -> str:
    """What to tell the person when Twilio won't send the code."""
    code = getattr(error, "code", None)
    if code in (21215, 21216, 13227, 21408, 21612):  # geo permissions / region not enabled
        what = "call" if delivery == "call" else "text"
        other = "Text me" if delivery == "call" else "Call me"
        return (f"Formline isn't allowed to {what} that number yet (Twilio error {code}: outbound "
                f"{'calls' if delivery == 'call' else 'texts'} to this country are turned off for the account). "
                f"Try “{other}”, or ask whoever runs the Twilio account to enable it.")
    if code in (21211, 21614, 21217):
        return "That doesn't look like a phone number Twilio can reach. Check the number and try again."
    return f"Formline couldn't {'call' if delivery == 'call' else 'text'} that number just now ({error.__class__.__name__}" \
           f"{f' {code}' if code else ''}). Try again in a minute."


def deliver_code(phone: str, code: str, delivery: str) -> None:
    """Text or call the code to the phone. Never written to the transcript or activity log."""
    s = get_settings()
    if not twilio_configured():
        # Development without Twilio: the server console is the delivery channel.
        log.warning("[offline] Formline pairing code for %s: %s", phone, code)
        return
    from twilio.rest import Client

    client = Client(s.twilio_account_sid, s.twilio_auth_token)
    if delivery == "call":
        spoken = ", ".join(code)
        twiml = (f"<Response><Pause length=\"1\"/><Say>Your Formline pairing code is {spoken}. "
                 f"Again, {spoken}.</Say></Response>")
        client.calls.create(to=phone, from_=s.twilio_phone_number, twiml=twiml)
    else:
        client.messages.create(to=phone, from_=s.twilio_phone_number,
                               body=f"Your Formline pairing code is {code}. It expires in 10 minutes. "
                                    "If you didn't ask for it, ignore this text.")


# ---------------------------------------------------------------- step 2: check the code

def confirm_pairing(pairing_id: str, code: str, *, name: Optional[str] = None, pin: Optional[str] = None,
                    label: Optional[str] = None) -> dict:
    """Returns {"status": "paired", installation_id, token, profile_name}, or asks for more:
    {"status": "need_profile"} (new number: name + PIN) or {"status": "choose_profile", "profiles": [...]}."""
    with session_scope() as s:
        req = s.get(PairingRequest, pairing_id or "")
        if req is None or req.verified_at is not None:
            raise PairingError("unknown", "That pairing request isn't valid anymore. Start again.")
        if _aware(req.expires_at) < _now():
            raise PairingError("expired", "That code expired. Start again to get a new one.")
        if req.attempts >= MAX_CODE_ATTEMPTS:
            raise PairingError("too_many_attempts", "Too many wrong codes. Start again to get a new one.")
        if not hmac.compare_digest(req.code_hash, _hash(pairing_id, re.sub(r"\D", "", code or ""))):
            req.attempts += 1
            s.add(req)
            s.commit()
            left = MAX_CODE_ATTEMPTS - req.attempts
            raise PairingError("wrong_code", f"That code isn't right. {left} tries left.")
        phone = req.phone

    profile = _profile_for(phone, name, pin)
    if isinstance(profile, dict):
        return profile  # needs more information; the code stays valid for the retry

    token = secrets.token_urlsafe(32)
    installation_id = "br_" + secrets.token_urlsafe(9)
    with session_scope() as s:
        req = s.get(PairingRequest, pairing_id)
        req.verified_at = _now()
        s.add(req)
        s.add(BrowserInstallation(id=installation_id, profile_id=profile.id, token_hash=_hash(token),
                                  label=(label or "Chrome")[:60]))
        s.commit()
    log_activity("browser_paired", f"Paired a browser ({(label or 'Chrome')[:60]})", profile_id=profile.id)
    return {"status": "paired", "installation_id": installation_id, "token": token,
            "profile_name": profile.display_name or "", "phone": phone}


def _profile_for(phone: str, name: Optional[str], pin: Optional[str]):
    profiles = identity.profiles_for_phone(phone)
    pin = (pin or "").strip()
    if not profiles:
        if not (name and name.strip()) or not (len(pin) == 4 and pin.isdigit()):
            return {"status": "need_profile"}
        profile = identity.create_profile(phone, display_name=name.strip()[:60])
        identity.set_pin(profile.id, pin)
        return identity.get_profile(profile.id)
    if len(profiles) == 1:
        profile = profiles[0]
    else:
        profile = identity.match_profile(phone, name or "")
        if profile is None:
            return {"status": "choose_profile", "profiles": [p.display_name or "Unnamed" for p in profiles]}
    if not profile.pin_hash:  # e.g. a partner reset it: choose a new one now
        if not (len(pin) == 4 and pin.isdigit()):
            return {"status": "need_pin", "profile_name": profile.display_name or ""}
        identity.set_pin(profile.id, pin)
    return profile


# ---------------------------------------------------------------- credentials

def authenticate(installation_id: str, token: str) -> Optional[BrowserInstallation]:
    with session_scope() as s:
        inst = s.get(BrowserInstallation, installation_id or "")
        if inst is None or inst.revoked_at is not None:
            return None
        if not hmac.compare_digest(inst.token_hash, _hash(token or "")):
            return None
        inst.last_seen_at = _now()
        s.add(inst)
        s.commit()
        s.refresh(inst)
        return inst


def revoke(installation_id: str) -> None:
    with session_scope() as s:
        inst = s.get(BrowserInstallation, installation_id)
        if inst is not None and inst.revoked_at is None:
            inst.revoked_at = _now()
            s.add(inst)
            s.commit()


def installations_for_profile(profile_id: int) -> list[BrowserInstallation]:
    with session_scope() as s:
        return list(s.exec(select(BrowserInstallation).where(
            BrowserInstallation.profile_id == profile_id, BrowserInstallation.revoked_at.is_(None))).all())


def paired_profiles(phone: str) -> list:
    """Profiles on this phone that have at least one paired browser."""
    return [p for p in identity.profiles_for_phone(phone) if installations_for_profile(p.id)]
