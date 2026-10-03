"""Sessions, profiles, PINs, shared phones. Owner: Lane B.

The brain (Lane A) decides *when* to ask for a PIN or who's calling; these functions
do the storage and checks.

PIN model: 4 digits, salted PBKDF2 hash. A correct PIN counts for 30 minutes in that session.
After 3 wrong attempts in a row the profile locks; only a partner can unlock it, with
`reset_pin` from the dashboard, after which the person sets a new PIN.

Shared phones: one phone can have several profiles. `profiles_for_phone` lists them,
`match_profile` finds the one the caller named, and `select_profile` switches the session to it
(which drops the previous person's PIN verification and active task).
"""

import hashlib
import hmac
import os
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.db import session_scope
from app.events import log_activity, log_event
from app.models import PinGuard, Profile, Session

PIN_VALID_FOR = timedelta(minutes=30)
PIN_MAX_ATTEMPTS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- sessions

def get_session(phone: str) -> Session:
    """Conversation state for this phone, created on first contact."""
    with session_scope() as s:
        sess = s.exec(select(Session).where(Session.phone == phone)).first()
        if sess is not None:
            return sess
        try:
            sess = Session(phone=phone)
            s.add(sess)
            s.commit()
            s.refresh(sess)
            return sess
        except IntegrityError:  # two first messages at once: the other one created it
            s.rollback()
            return s.exec(select(Session).where(Session.phone == phone)).one()


def save_session(sess: Session) -> None:
    sess.updated_at = _now()
    with session_scope() as s:
        s.merge(sess)
        s.commit()


def note_channel(sess: Session, channel: str) -> bool:
    """Record the channel for this turn. Returns True if the person switched channels
    (and logs a `channel_switch` metrics event). The caller saves the session."""
    previous = sess.last_channel
    switched = previous is not None and previous != channel
    sess.last_channel = channel
    if switched:
        log_event("channel_switch", phone=sess.phone, profile_id=sess.profile_id, channel=channel,
                  **{"from": previous, "to": channel})
    return switched


# ---------------------------------------------------------------- profiles

def profiles_for_phone(phone: str) -> list[Profile]:
    with session_scope() as s:
        return list(s.exec(select(Profile).where(Profile.phone == phone).order_by(Profile.id)).all())


def get_profile(profile_id: int) -> Optional[Profile]:
    with session_scope() as s:
        return s.get(Profile, profile_id)


def create_profile(phone: str, *, language: str = "en", display_name: Optional[str] = None) -> Profile:
    with session_scope() as s:
        p = Profile(phone=phone, preferred_language=language, display_name=display_name)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p


def update_profile(profile_id: int, **fields) -> None:
    with session_scope() as s:
        p = s.get(Profile, profile_id)
        for k, v in fields.items():
            setattr(p, k, v)
        p.updated_at = _now()
        s.add(p)
        s.commit()


def _fold(text: str) -> str:
    """Lowercase, accents removed: 'José' -> 'jose'."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def match_profile(phone: str, text: str) -> Optional[Profile]:
    """The profile on this phone whose name the caller said ("it's Denise", "soy José").
    None if no name matches, or if more than one does."""
    words = set("".join(c if c.isalnum() else " " for c in _fold(text)).split())
    matches = [p for p in profiles_for_phone(phone)
               if p.display_name and set(_fold(p.display_name).split()) & words]
    return matches[0] if len(matches) == 1 else None


def select_profile(sess: Session, profile_id: int) -> None:
    """Switch the session to another person on the same phone, and save it.

    Switching to a different person drops the PIN verification and active task, which belonged
    to whoever was talking before. `state` and `pending` are left to the brain.
    Raises ValueError if the profile doesn't belong to this phone.
    """
    p = get_profile(profile_id)
    if p is None or p.phone != sess.phone:
        raise ValueError(f"profile {profile_id} is not on this phone")
    if sess.profile_id != profile_id:
        sess.profile_id = profile_id
        sess.pin_verified_at = None
        sess.active_task_id = None
    save_session(sess)


# ---------------------------------------------------------------- PIN

PinStatus = Literal["ok", "wrong", "locked", "no_pin"]


@dataclass
class PinCheck:
    status: PinStatus
    attempts_left: int  # wrong tries left before the profile locks (0 when locked)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _hash_pin(pin: str, salt: bytes) -> str:
    return salt.hex() + ":" + hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 100_000).hex()


def _clear_guard(profile_id: int) -> None:
    with session_scope() as s:
        guard = s.get(PinGuard, profile_id)
        if guard:
            s.delete(guard)
            s.commit()


def set_pin(profile_id: int, pin: str) -> None:
    pin = pin.strip()
    if not (len(pin) == 4 and pin.isdigit()):
        raise ValueError("PIN must be 4 digits")
    update_profile(profile_id, pin_hash=_hash_pin(pin, os.urandom(16)))
    _clear_guard(profile_id)


def pin_locked(profile_id: int) -> bool:
    with session_scope() as s:
        guard = s.get(PinGuard, profile_id)
        return bool(guard and guard.locked_at)


def pin_attempts_left(profile_id: int) -> int:
    with session_scope() as s:
        guard = s.get(PinGuard, profile_id)
    if guard is None:
        return PIN_MAX_ATTEMPTS
    return 0 if guard.locked_at else max(0, PIN_MAX_ATTEMPTS - guard.failed_attempts)


def attempt_pin(profile_id: int, pin: str) -> PinCheck:
    """Check a PIN and count the attempt. A locked profile always fails, even with the right PIN."""
    p = get_profile(profile_id)
    if not p or not p.pin_hash:
        return PinCheck("no_pin", PIN_MAX_ATTEMPTS)
    with session_scope() as s:
        guard = s.get(PinGuard, profile_id) or PinGuard(profile_id=profile_id)
        if guard.locked_at:
            return PinCheck("locked", 0)
        salt_hex, _ = p.pin_hash.split(":", 1)
        if hmac.compare_digest(p.pin_hash, _hash_pin(pin.strip(), bytes.fromhex(salt_hex))):
            if guard.failed_attempts:
                guard.failed_attempts, guard.updated_at = 0, _now()
                s.add(guard)
                s.commit()
            return PinCheck("ok", PIN_MAX_ATTEMPTS)
        guard.failed_attempts += 1
        guard.updated_at = _now()
        just_locked = guard.failed_attempts >= PIN_MAX_ATTEMPTS
        if just_locked:
            guard.locked_at = guard.updated_at
        s.add(guard)
        s.commit()
        left = max(0, PIN_MAX_ATTEMPTS - guard.failed_attempts)
    if just_locked:
        log_activity("pin_locked", f"PIN locked after {PIN_MAX_ATTEMPTS} wrong attempts. A partner can reset it.",
                     profile_id=profile_id)
        return PinCheck("locked", 0)
    return PinCheck("wrong", left)


def check_pin(profile_id: int, pin: str) -> bool:
    """True if the PIN is right and the profile isn't locked. Counts wrong attempts."""
    return attempt_pin(profile_id, pin).ok


def verify_pin(sess: Session, pin: str) -> PinCheck:
    """Check a PIN for the session's current profile; on success mark the session verified
    (`pin_verified_at`) and save it. Use `.status` to choose the reply:
    ok / wrong (`.attempts_left`) / locked (a partner must reset) / no_pin (ask them to set one)."""
    if sess.profile_id is None:
        return PinCheck("no_pin", PIN_MAX_ATTEMPTS)
    result = attempt_pin(sess.profile_id, pin)
    if result.ok:
        sess.pin_verified_at = _now()
        save_session(sess)
    return result


def pin_verified(sess: Session) -> bool:
    """True if the PIN was entered recently in this session."""
    if sess.pin_verified_at is None:
        return False
    at = sess.pin_verified_at
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return _now() - at < PIN_VALID_FOR


def reset_pin(profile_id: int) -> None:
    """Partner-initiated reset from the dashboard: clears the PIN and any lockout, and drops PIN
    verification on the phone's session. The person sets a new PIN on next contact."""
    update_profile(profile_id, pin_hash=None)
    _clear_guard(profile_id)
    with session_scope() as s:
        for sess in s.exec(select(Session).where(Session.profile_id == profile_id)).all():
            sess.pin_verified_at = None
            s.add(sess)
        s.commit()
