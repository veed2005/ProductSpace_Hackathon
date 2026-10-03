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


# ---------------------------------------------------------------- beyond English and Spanish

def reporting(model, code):
    """The scripted model, also naming the language it heard (as the real one does)."""
    async def decide(system, messages):
        decision = await model(system, messages)
        decision.language = code
        return decision

    return decide


def test_hindi_caller_is_answered_in_hindi(paired, browser, monkeypatch):
    from app.agent import runner

    _, profile, model = paired
    decide, systems = recording(model)
    monkeypatch.setattr(runner, "llm_decide", decide)
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CAL7"})
        heard_until(ws, "PIN")
        keypad(ws, "4821")
        heard_until(ws, "What would you like help with?")

        say(ws, "मेरा नाम Google पर search करेंगे?")  # Hindi with English words mixed in, as people speak it
        msgs = heard_until(ws, "about?")
        switch = [m for m in msgs if m.get("type") == "language"]
        assert switch and switch[0]["ttsLanguage"] == "hi-IN"
        assert msgs.index(switch[0]) < next(i for i, m in enumerate(msgs) if m.get("type") == "text")
        assert systems and all("the caller speaks Hindi" in s for s in systems)
    assert identity.get_profile(profile.id).preferred_language == "hi"


def test_a_saved_hindi_preference_starts_the_call_in_hindi(paired, browser):
    _, profile, _ = paired
    identity.update_profile(profile.id, preferred_language="hi")
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CAL8"})
        msgs = heard_until(ws, "PIN")  # no model offline, so the line itself stays English; the voice switches
        assert any(m.get("type") == "language" and m["ttsLanguage"] == "hi-IN" for m in msgs)


def test_one_foreign_word_in_an_english_sentence_doesnt_switch(paired, browser):
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CAL9"})
        heard_until(ws, "PIN")
        keypad(ws, "4821")
        heard_until(ws, "What would you like help with?")
        say(ws, "My name is चुपिया.")  # the recognizer wrote a name in Devanagari
        say(ws, "Stop.")
        msgs = heard_until(ws, "Okay, I stopped")
        assert not any(m.get("type") == "language" for m in msgs)


def test_the_model_names_a_language_the_word_lists_dont_know(paired, browser, monkeypatch):
    from app.agent import runner

    _, profile, model = paired
    monkeypatch.setattr(runner, "llm_decide", reporting(model, "fr"))
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CAL10"})
        heard_until(ws, "PIN")
        keypad(ws, "4821")
        heard_until(ws, "What would you like help with?")
        say(ws, "Je voudrais prendre un rendez-vous avec le docteur Smith.")
        msgs = heard_until(ws, "about?")
        switch = [m for m in msgs if m.get("type") == "language"]
        assert switch and switch[0]["ttsLanguage"] == "fr-FR"
        assert msgs.index(switch[0]) < next(i for i, m in enumerate(msgs) if m.get("type") == "text")
    assert identity.get_profile(profile.id).preferred_language == "fr"


def test_the_model_cannot_switch_on_a_short_reply_or_an_unknown_code(paired, browser, monkeypatch):
    from app.agent import runner

    _, profile, model = paired
    codes = iter(["fr", "xx", "xx", "xx", "xx", "xx"])

    async def decide(system, messages):
        decision = await model(system, messages)
        decision.language = next(codes, "xx")
        return decision

    monkeypatch.setattr(runner, "llm_decide", decide)
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CAL11"})
        heard_until(ws, "PIN")
        keypad(ws, "4821")
        heard_until(ws, "What would you like help with?")
        say(ws, "Dr. Smith.")  # two words: "fr" from the model is not enough
        msgs = heard_until(ws, "about?")
        assert not any(m.get("type") == "language" for m in msgs)
    assert identity.get_profile(profile.id).preferred_language == "en"


def test_the_recognizers_tag_switches_a_full_sentence(paired, browser, autodetect):
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CAL12"})
        heard_until(ws, "PIN")
        keypad(ws, "4821")
        heard_until(ws, "What would you like help with?")
        ws.send_json({"type": "prompt", "voicePrompt": "Ich brauche einen Termin bei Doktor Smith.", "last": True,
                      "lang": "de-DE"})
        msgs = heard_until(ws, "about?")
        assert any(m.get("type") == "language" and m["ttsLanguage"] == "de-DE" for m in msgs)


def test_script_and_explicit_requests():
    from app.formcall import language as lang

    assert lang.detect_script("नया tab खोलकर सोच करूं.") == "hi"
    assert lang.detect_script("Привет, мне нужна помощь") == "ru"
    assert lang.detect_script("予約をお願いします") == "ja"
    assert lang.detect_script("My name is चुपिया.") is None
    assert lang.detect_script("412 Elm Street") is None
    assert lang.requested_call_switch("Can you answer in Hindi?") == "hi"
    assert lang.requested_call_switch("French, please") == "fr"
    assert lang.requested_call_switch("Háblame en español") == "es"
    assert lang.requested_call_switch("I live in Springfield") is None


def test_formlines_own_lines_are_translated_once_and_keep_their_slots(monkeypatch):
    from app.formcall import language as lang
    from app.llm import client as llm

    calls = []

    def fake_text(*, system, messages, **kw):
        calls.append(messages[0]["content"])
        english = messages[0]["content"]
        return "BROKEN" if "{left}" in english else "HI " + english

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "text", fake_text)
    assert lang.phrase("hi", "t.greet", "Hi {name}.") == "HI Hi {name}."
    assert lang.phrase("hi", "t.greet", "Hi {name}.") == "HI Hi {name}." and len(calls) == 1  # cached
    assert lang.phrase("hi", "t.left", "You have {left} tries.") == "You have {left} tries."  # lost its slot: English
    assert lang.phrase("en", "t.greet", "Hi {name}.") == "Hi {name}."
