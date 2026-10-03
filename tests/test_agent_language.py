"""A browser-agent call answers in the language the caller speaks, and switches the phone voice to match."""

import pytest
from fastapi.testclient import TestClient

from app.agent.prompts import system_prompt
from app.core import identity
from app.main import app
from test_agent_call import (PHONE, browser, call, keypad, paired, say, text_in, texts,  # noqa: F401  (fixtures)
                             wait_for)


@pytest.fixture
def autodetect(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("FORMLINE_VOICE_AUTODETECT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("FORMLINE_VOICE_AUTODETECT")
    get_settings.cache_clear()


def heard_until(ws, needle, limit=40):
    """Every relay message up to the spoken line containing `needle`."""
    msgs = []
    for _ in range(limit):
        msg = ws.receive_json()
        msgs.append(msg)
        if msg.get("type") == "text" and needle.lower() in msg["token"].lower():
            return msgs
    raise AssertionError(f"never heard {needle!r}; got {msgs}")


def recording(model):
    systems: list[str] = []

    async def decide(system, messages):
        systems.append(system)
        return await model(system, messages)

    return decide, systems


def test_spanish_caller_is_answered_in_spanish(paired, browser, monkeypatch):
    from app.agent import runner

    _, profile, model = paired
    decide, systems = recording(model)
    monkeypatch.setattr(runner, "llm_decide", decide)
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CAL1"})
        heard_until(ws, "PIN")
        keypad(ws, "4821")
        heard_until(ws, "What would you like help with?")

        say(ws, "Necesito hacer una cita con el doctor Smith, por favor.")
        msgs = heard_until(ws, "about?")
        switch = [m for m in msgs if m.get("type") == "language"]
        assert switch and switch[0]["ttsLanguage"] == "es-US"
        assert msgs.index(switch[0]) < next(i for i, m in enumerate(msgs) if m.get("type") == "text")
        assert systems and all("the caller speaks Spanish" in s for s in systems)

        say(ws, "Para.")
        heard_until(ws, "Listo, me detuve")  # Formline's own lines follow too
    assert identity.get_profile(profile.id).preferred_language == "es"  # remembered for the next call


def test_short_replies_dont_change_the_language(paired, browser):
    _, profile, _ = paired
    identity.update_profile(profile.id, preferred_language="es")
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CAL2"})
        heard_until(ws, "PIN de 4 dígitos")
        keypad(ws, "4821")
        heard_until(ws, "¿En qué le ayudo?")
        say(ws, "Okay.")  # one word: not enough to switch
        say(ws, "Para.")
        msgs = heard_until(ws, "Listo, me detuve")
        assert not any(m.get("type") == "language" and m["ttsLanguage"] == "en-US" for m in msgs)


def test_english_caller_on_a_spanish_profile_gets_english(paired, browser):
    _, profile, _ = paired
    identity.update_profile(profile.id, preferred_language="es")
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CAL3"})
        heard_until(ws, "PIN de 4 dígitos")
        keypad(ws, "4821")
        heard_until(ws, "¿En qué le ayudo?")
        say(ws, "I need to make an appointment with Dr. Smith.")
        msgs = heard_until(ws, "about?")
        assert any(m.get("type") == "language" and m["ttsLanguage"] == "en-US" for m in msgs)
    assert identity.get_profile(profile.id).preferred_language == "en"


def test_language_is_saved_only_after_the_pin(paired, browser):
    _, profile, _ = paired
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CAL4"})
        heard_until(ws, "PIN")
        say(ws, "Hola, quiero revisar mis citas, por favor.")  # before the PIN: answered in Spanish...
        msgs = heard_until(ws, "los 4 dígitos de su PIN")
        assert any(m.get("type") == "language" and m["ttsLanguage"] == "es-US" for m in msgs)
        assert identity.get_profile(profile.id).preferred_language == "en"  # ...but caller ID alone saves nothing
        keypad(ws, "4821")
        heard_until(ws, "Veo que tiene abierto")
    assert identity.get_profile(profile.id).preferred_language == "es"


def test_names_and_addresses_dont_switch_whatever_the_recognizer_says(paired, browser, autodetect):
    _, profile, _ = paired
    identity.update_profile(profile.id, preferred_language="es")
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CAL5"})
        heard_until(ws, "PIN de 4 dígitos")
        keypad(ws, "4821")
        heard_until(ws, "¿En qué le ayudo?")
        ws.send_json({"type": "prompt", "voicePrompt": "412 Elm Street, Springfield, Illinois", "last": True,
                      "lang": "en-US"})
        say(ws, "Para.")
        msgs = heard_until(ws, "Listo, me detuve")
        assert not any(m.get("type") == "language" for m in msgs)
    assert identity.get_profile(profile.id).preferred_language == "es"


def test_a_spanish_text_is_answered_in_spanish(paired, browser, texts):
    with TestClient(app) as client:
        text_in(client, "Hola, quiero revisar mis citas, por favor.")
        wait_for(texts, "responda con su PIN de 4 dígitos")


def test_prompt_tells_the_model_to_translate_what_it_reads():
    prompt = system_prompt("es")
    assert "the caller speaks Spanish" in prompt
    assert "translate it into the caller's language" in prompt
    assert "evidence is the opposite" in prompt  # quotes stay in the page's language so they can be verified
