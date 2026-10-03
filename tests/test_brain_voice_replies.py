"""Replies as they really arrive: voice transcripts with punctuation, natural Spanish answers,
and PINs that must never reach the transcript."""

import pytest
from sqlmodel import select

from app.contracts import TurnRequest
from app.core import identity
from app.core.turn import _language_choice, handle_turn
from app.db import session_scope
from app.engines.form_engine import _coerce_yes_no, reply_key
from app.models import Message


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()


def say(phone: str, text: str, channel: str = "voice"):
    return handle_turn(TurnRequest(phone=phone, channel=channel, text=text))


def test_reply_key_strips_transcript_punctuation():
    assert reply_key("Sí.") == "sí"
    assert reply_key("  Yes!  ") == "yes"
    assert reply_key("¿No?") == "no"
    assert reply_key("Don't use them.") == "don't use them"
    assert reply_key(None) == ""


def test_voice_onboarding_accepts_punctuated_answers():
    phone = "+15550010001"
    say(phone, "")
    assert say(phone, "Español.").language == "es"
    say(phone, "Sí.")
    assert identity.get_session(phone).state == "awaiting_pin_setup"
    say(phone, "1 2 3 4.")
    assert identity.get_session(phone).state == "menu"
    profile = identity.profiles_for_phone(phone)[0]
    assert identity.check_pin(profile.id, "1234")


@pytest.mark.parametrize("answer, lang", [
    ("en español por favor", "es"), ("Español.", "es"), ("spanish", "es"), ("es", "es"),
    ("English.", "en"), ("in english please", "en"), ("en", "en"), ("inglés", "en"),
    ("necesito ayuda en un formulario", None), ("hello", None),
])
def test_language_choice(answer, lang):
    assert _language_choice(reply_key(answer)) == lang


def test_spanish_answer_containing_en_gets_spanish():
    phone = "+15550010002"
    say(phone, "hola", channel="sms")
    assert say(phone, "en español por favor", channel="sms").language == "es"


def test_pin_never_stored_in_transcript():
    phone = "+15550010003"
    for text in ("hi", "English", "yes", "4826"):
        say(phone, text, channel="sms")
    with session_scope() as s:
        texts = [m.text for m in s.exec(select(Message).where(Message.phone == phone)).all()]
    assert not any("4826" in t for t in texts)
    assert "[PIN]" in texts


def test_yes_no_field_accepts_punctuation():
    assert _coerce_yes_no("Yes.") == "yes"
    assert _coerce_yes_no("No!") == "no"
