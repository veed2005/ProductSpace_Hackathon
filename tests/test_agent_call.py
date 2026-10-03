"""A phone call that drives a paired browser, over the real ConversationRelay websocket."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.agent import runner
from app.browser import pairing
from app.browser.hub import hub
from app.core import identity
from app.db import session_scope
from app.main import app
from app.models import BrowserTask, Message
from fake_portal import FakeConnection, ScriptedModel

PHONE = "+12175550104"


@pytest.fixture
def paired(monkeypatch):
    sent = []
    monkeypatch.setattr(pairing, "deliver_code", lambda phone, code, delivery: sent.append(code))
    started = pairing.start_pairing(PHONE)
    out = pairing.confirm_pairing(started.pairing_id, sent[-1], name="Margaret", pin="4821")
    profile = identity.profiles_for_phone(PHONE)[0]
    model = ScriptedModel()
    monkeypatch.setattr(runner, "llm_decide", model)
    return out["installation_id"], profile, model


@pytest.fixture
def browser(paired):
    installation_id, profile, _ = paired
    conn = FakeConnection(installation_id, profile.id)
    hub.register(conn)
    yield conn
    hub.unregister(conn)


def until(ws, needle, limit=30):
    """Read relay messages until a spoken line contains `needle`; return everything spoken."""
    spoken = []
    for _ in range(limit):
        msg = ws.receive_json()
        if msg.get("type") == "text":
            spoken.append(msg["token"])
            if needle.lower() in msg["token"].lower():
                return spoken
        if msg.get("type") == "end":
            spoken.append("<end>")
            if needle == "<end>":
                return spoken
    raise AssertionError(f"never heard {needle!r}; heard {spoken}")


def call(client):
    return client.websocket_connect("/twilio/voice/relay")


def keypad(ws, digits):
    for d in digits:
        ws.send_json({"type": "dtmf", "digit": d})


def say(ws, text):
    ws.send_json({"type": "prompt", "voicePrompt": text, "last": True})


def test_golden_path_by_phone(paired, browser):
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CA1"})
        until(ws, "PIN")
        keypad(ws, "4821")
        heard = until(ws, "What would you like help with?")
        assert "I can see you have Riverbend Health patient portal open" in heard[-1]

        say(ws, "I need to make an appointment with Dr. Smith.")
        until(ws, "What would you like to see Dr. Smith about?")
        say(ws, "My knee has been hurting.")
        until(ws, "Which would you prefer?")
        say(ws, "Thursday.")
        heard = until(ws, "book it?")
        assert browser.browser.portal.booked == 0
        say(ws, "Yes.")
        until(ws, "RB-41234")

    assert browser.browser.portal.booked == 1
    with session_scope() as s:
        task = s.exec(select(BrowserTask)).one()
        transcript = [m.text for m in s.exec(select(Message).order_by(Message.id)).all()]
    assert task.status == "completed"
    assert "4821" not in " ".join(transcript) and "[PIN]" in transcript  # the PIN never reaches the transcript


def test_wrong_pin_then_lockout(paired, browser):
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CA2"})
        until(ws, "PIN")
        say(ws, "one one one one")
        assert "2 tries left" in until(ws, "tries left")[-1]
        say(ws, "1111")
        until(ws, "1 try left")
        say(ws, "1111")
        until(ws, "locked")
        until(ws, "<end>")
    assert identity.pin_locked(paired[1].id)
    assert browser.browser.portal.actions == []


def test_spoken_pin_words_work(paired, browser):
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CA3"})
        until(ws, "PIN")
        say(ws, "Four, eight, two, one.")
        until(ws, "What would you like help with?")


def test_no_browser_connected(paired):
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CA4"})
        until(ws, "PIN")
        keypad(ws, "4821")
        until(ws, "I don't see a connected browser right now")


def test_unpaired_caller_still_gets_the_form_assistant():
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": "+15550009999", "callSid": "CA5"})
        until(ws, "what language")


def test_stop_during_a_call(paired, browser):
    _, _, model = paired
    with TestClient(app) as client, call(client) as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CA6"})
        until(ws, "PIN")
        keypad(ws, "4821")
        until(ws, "What would you like help with?")
        say(ws, "I need to make an appointment with Dr. Smith.")
        until(ws, "about?")
        say(ws, "Stop.")
        until(ws, "I stopped")
    with session_scope() as s:
        assert s.exec(select(BrowserTask)).one().status == "stopped"


# ---------------------------------------------------------------- texting

@pytest.fixture
def texts(monkeypatch):
    sent: list[str] = []
    from app.agent import sms

    monkeypatch.setattr(sms, "send_sms", lambda phone, body, **kw: sent.append(body))
    yield sent
    import asyncio

    asyncio.run(sms.reset())


def text_in(client, body):
    r = client.post("/twilio/messaging", data={"From": PHONE, "Body": body, "MessageSid": "SM" + uuid.uuid4().hex})
    assert r.status_code == 200
    return r.text


def wait_for(sent, needle, timeout=5.0):
    import time

    end = time.time() + timeout
    while time.time() < end:
        if any(needle.lower() in s.lower() for s in sent):
            return
        time.sleep(0.02)
    raise AssertionError(f"never texted {needle!r}; sent {sent}")


def test_texting_drives_the_browser_and_remembers_the_goal(paired, browser, texts):
    with TestClient(app) as client:
        assert "<Message>" not in text_in(client, "Book an appointment with Dr. Smith")  # replies come as texts
        wait_for(texts, "reply with your 4-digit PIN")
        text_in(client, "4821")
        wait_for(texts, "I'll get started")  # starts on the goal given before the PIN
        wait_for(texts, "What would you like to see Dr. Smith about?")
        text_in(client, "My knee has been hurting.")
        wait_for(texts, "Which would you prefer?")
        text_in(client, "Thursday")
        wait_for(texts, "book it?")
        text_in(client, "yes")
        wait_for(texts, "RB-41234")
    assert browser.browser.portal.booked == 1


def test_text_opening_with_the_pin(paired, browser, texts):
    with TestClient(app) as client:
        text_in(client, "4821")
        wait_for(texts, "What would you like help with?")
    assert not any("reply with your 4-digit PIN" in t for t in texts)
