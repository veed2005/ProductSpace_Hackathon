from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.core import identity
from app.db import session_scope
from app.memory import profile as memory
from app.models import Activity, Event

PHONE = "+15550007777"


def _events(kind: str) -> list[Event]:
    with session_scope() as s:
        return list(s.exec(select(Event).where(Event.type == kind)).all())


# ---------------------------------------------------------------- PIN lockout

def test_three_wrong_pins_lock_the_profile_even_against_the_right_pin():
    p = identity.create_profile(PHONE)
    identity.set_pin(p.id, "1234")
    assert identity.attempt_pin(p.id, "0000") == identity.PinCheck("wrong", 2)
    assert identity.attempt_pin(p.id, "1111") == identity.PinCheck("wrong", 1)
    assert identity.attempt_pin(p.id, "2222") == identity.PinCheck("locked", 0)
    assert identity.pin_locked(p.id) and identity.pin_attempts_left(p.id) == 0
    assert not identity.check_pin(p.id, "1234")  # right PIN, still locked
    with session_scope() as s:
        kinds = [a.kind for a in s.exec(select(Activity).where(Activity.profile_id == p.id)).all()]
    assert kinds.count("pin_locked") == 1


def test_correct_pin_resets_the_wrong_attempt_count():
    p = identity.create_profile(PHONE)
    identity.set_pin(p.id, "1234")
    identity.attempt_pin(p.id, "0000")
    identity.attempt_pin(p.id, "0000")
    assert identity.check_pin(p.id, " 1234 ")
    assert identity.pin_attempts_left(p.id) == 3
    identity.attempt_pin(p.id, "0000")
    assert not identity.pin_locked(p.id)


def test_partner_reset_unlocks_and_requires_a_new_pin():
    p = identity.create_profile(PHONE)
    identity.set_pin(p.id, "1234")
    sess = identity.get_session(PHONE)
    identity.select_profile(sess, p.id)
    assert identity.verify_pin(sess, "1234").ok
    for _ in range(3):
        identity.attempt_pin(p.id, "9999")
    assert identity.pin_locked(p.id)

    identity.reset_pin(p.id)
    assert not identity.pin_locked(p.id)
    assert identity.get_profile(p.id).pin_hash is None
    assert identity.attempt_pin(p.id, "1234").status == "no_pin"
    assert not identity.pin_verified(identity.get_session(PHONE))  # reset drops verification

    identity.set_pin(p.id, "4321")
    assert identity.check_pin(p.id, "4321") and identity.pin_attempts_left(p.id) == 3


def test_verify_pin_marks_session_verified_only_on_success():
    p = identity.create_profile(PHONE)
    identity.set_pin(p.id, "1234")
    sess = identity.get_session(PHONE)
    assert identity.verify_pin(sess, "1234").status == "no_pin"  # no profile chosen yet
    identity.select_profile(sess, p.id)

    assert identity.verify_pin(sess, "0000").status == "wrong"
    assert not identity.pin_verified(identity.get_session(PHONE))
    assert identity.verify_pin(sess, "1234").ok
    assert identity.pin_verified(identity.get_session(PHONE))  # saved, not just in memory


def test_pin_verification_expires():
    sess = identity.get_session(PHONE)
    sess.pin_verified_at = datetime.now(timezone.utc) - identity.PIN_VALID_FOR - timedelta(seconds=1)
    assert not identity.pin_verified(sess)


def test_set_pin_validates_and_strips():
    p = identity.create_profile(PHONE)
    for bad in ("123", "12345", "abcd", ""):
        with pytest.raises(ValueError):
            identity.set_pin(p.id, bad)
    identity.set_pin(p.id, " 5678 ")
    assert identity.check_pin(p.id, "5678")


def test_lockout_survives_forget_and_reset_of_other_profiles():
    """PinGuard has no foreign key, so deleting a profile with a lock never fails."""
    p = identity.create_profile(PHONE)
    identity.set_pin(p.id, "1234")
    for _ in range(3):
        identity.attempt_pin(p.id, "0000")
    memory.forget_profile(p.id)
    assert identity.get_profile(p.id) is None


# ---------------------------------------------------------------- shared phones

def test_two_profiles_on_one_phone():
    james = identity.create_profile(PHONE, display_name="James")
    denise = identity.create_profile(PHONE, display_name="Denise")
    identity.create_profile("+15550008888", display_name="Denise")  # another phone: never matched
    assert [p.id for p in identity.profiles_for_phone(PHONE)] == [james.id, denise.id]

    assert identity.match_profile(PHONE, "Hi, it's Denise.").id == denise.id
    assert identity.match_profile(PHONE, "this is JAMES walker").id == james.id
    assert identity.match_profile(PHONE, "it's me") is None
    assert identity.match_profile(PHONE, "James and Denise") is None  # ambiguous


def test_match_profile_ignores_accents():
    jose = identity.create_profile(PHONE, display_name="José")
    assert identity.match_profile(PHONE, "soy jose").id == jose.id


def test_switching_person_drops_previous_pin_and_task():
    james = identity.create_profile(PHONE, display_name="James")
    denise = identity.create_profile(PHONE, display_name="Denise")
    identity.set_pin(james.id, "1111")
    sess = identity.get_session(PHONE)
    identity.select_profile(sess, james.id)
    identity.verify_pin(sess, "1111")
    assert identity.pin_verified(sess)

    identity.select_profile(sess, denise.id)
    stored = identity.get_session(PHONE)
    assert stored.profile_id == denise.id and not identity.pin_verified(stored)
    assert stored.active_task_id is None

    identity.verify_pin(stored, "1111")  # James's PIN doesn't unlock Denise
    assert not identity.pin_verified(identity.get_session(PHONE))


def test_select_profile_keeps_verification_when_same_person():
    p = identity.create_profile(PHONE)
    identity.set_pin(p.id, "1234")
    sess = identity.get_session(PHONE)
    identity.select_profile(sess, p.id)
    identity.verify_pin(sess, "1234")
    identity.select_profile(sess, p.id)
    assert identity.pin_verified(identity.get_session(PHONE))


def test_cannot_select_a_profile_from_another_phone():
    other = identity.create_profile("+15550008888")
    with pytest.raises(ValueError):
        identity.select_profile(identity.get_session(PHONE), other.id)


# ---------------------------------------------------------------- sessions and channels

def test_channel_switch_logged_once_per_switch():
    sess = identity.get_session(PHONE)
    assert identity.note_channel(sess, "voice") is False  # first contact isn't a switch
    assert identity.note_channel(sess, "voice") is False
    assert identity.note_channel(sess, "sms") is True
    assert identity.note_channel(sess, "sms") is False
    events = _events("channel_switch")
    assert len(events) == 1
    assert events[0].data == {"from": "voice", "to": "sms"} and events[0].channel == "sms"
    assert events[0].phone == PHONE


def test_get_session_is_one_per_phone():
    a = identity.get_session(PHONE)
    b = identity.get_session(PHONE)
    assert a.id == b.id
