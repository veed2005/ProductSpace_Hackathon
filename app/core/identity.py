"""Sessions, profiles, PINs, shared phones. Owner: Lane B.

The brain (Lane A) decides *when* to ask for a PIN or who's calling; these functions
do the storage and checks.
"""

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import select

from app.db import session_scope
from app.models import Profile, Session

PIN_VALID_FOR = timedelta(minutes=30)


def get_session(phone: str) -> Session:
    """Conversation state for this phone, created on first contact."""
    with session_scope() as s:
        sess = s.exec(select(Session).where(Session.phone == phone)).first()
        if sess is None:
            sess = Session(phone=phone)
            s.add(sess)
            s.commit()
            s.refresh(sess)
        return sess


def save_session(sess: Session) -> None:
    sess.updated_at = datetime.now(timezone.utc)
    with session_scope() as s:
        s.merge(sess)
        s.commit()


def profiles_for_phone(phone: str) -> list[Profile]:
    with session_scope() as s:
        return list(s.exec(select(Profile).where(Profile.phone == phone)).all())


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
        p.updated_at = datetime.now(timezone.utc)
        s.add(p)
        s.commit()


def _hash_pin(pin: str, salt: bytes) -> str:
    return salt.hex() + ":" + hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 100_000).hex()


def set_pin(profile_id: int, pin: str) -> None:
    if not (len(pin) == 4 and pin.isdigit()):
        raise ValueError("PIN must be 4 digits")
    update_profile(profile_id, pin_hash=_hash_pin(pin, os.urandom(16)))


def check_pin(profile_id: int, pin: str) -> bool:
    p = get_profile(profile_id)
    if not p or not p.pin_hash:
        return False
    salt_hex, _ = p.pin_hash.split(":", 1)
    return hmac.compare_digest(p.pin_hash, _hash_pin(pin.strip(), bytes.fromhex(salt_hex)))


def pin_verified(sess: Session) -> bool:
    """True if the PIN was entered recently in this session."""
    if sess.pin_verified_at is None:
        return False
    at = sess.pin_verified_at
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at < PIN_VALID_FOR
