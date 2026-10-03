from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select
from starlette.websockets import WebSocketDisconnect

from app.channels import voice
from app.contracts import TurnRequest, TurnResult
from app.core import identity
from app.db import session_scope
from app.models import Event

BASE = "https://formline-test.ngrok-free.app"
CALLER = "+15550004444"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def brain(monkeypatch):
    """Scripted brain: replies come from `brain.replies[text]`, default echoes the text."""

    class Brain:
        def __init__(self):
            self.requests: list[TurnRequest] = []
            self.replies: dict[str, TurnResult] = {}

        def __call__(self, req: TurnRequest) -> TurnResult:
            self.requests.append(req)
            return self.replies.get(req.text, TurnResult(reply=f"you said {req.text or '(nothing)'}"))

    b = Brain()
    monkeypatch.setattr(voice, "handle_turn", b)
    monkeypatch.setattr(voice, "speech_seconds", lambda text: 0)
    return b


def relay_element(xml: str):
    return ElementTree.fromstring(xml).find("./Connect/ConversationRelay")


def setup_msg() -> dict:
    return {"type": "setup", "sessionId": "VX1", "callSid": "CA1", "from": CALLER, "to": "+16073899899"}


# ---------------------------------------------------------------- TwiML

def test_inbound_call_connects_conversation_relay(client):
    resp = client.post("/twilio/voice", data={"From": CALLER, "CallSid": "CA1"})
    assert resp.status_code == 200
    root = ElementTree.fromstring(resp.text)
    assert root.find("./Connect").get("action") == BASE + "/twilio/voice/status"
    relay = relay_element(resp.text)
    url = urlparse(relay.get("url"))
    assert (url.scheme, url.netloc, url.path) == ("wss", "formline-test.ngrok-free.app", "/twilio/voice/relay")
    assert parse_qs(url.query)["t"]
    assert relay.get("language") == "en-US" and relay.get("welcomeGreeting") == voice.GREETING["en"]
    assert relay.get("dtmfDetection") == "true"
    assert {lang.get("code") for lang in relay.findall("Language")} == {"en-US", "es-US"}


def test_spanish_caller_greeted_in_spanish(client):
    identity.create_profile(CALLER, language="es")
    relay = relay_element(client.post("/twilio/voice", data={"From": CALLER}).text)
    assert relay.get("language") == "es-US" and relay.get("welcomeGreeting") == voice.GREETING["es"]


def test_unsigned_call_rejected_when_validation_on(client, monkeypatch):
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURES", "true")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    from app.config import get_settings

    get_settings.cache_clear()
    assert client.post("/twilio/voice", data={"From": CALLER}).status_code == 403


def test_failed_relay_session_falls_back_to_texting(client):
    resp = client.post("/twilio/voice/status", data={"SessionStatus": "failed", "ErrorCode": "64101"})
    assert "<Say>" in resp.text and "text this number" in resp.text and "<Hangup" in resp.text
    resp = client.post("/twilio/voice/status", data={"SessionStatus": "completed"})
    assert "<Say>" not in resp.text and "<Hangup" in resp.text


# ---------------------------------------------------------------- websocket auth

def test_relay_requires_a_one_time_token(client, brain, monkeypatch):
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURES", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/twilio/voice/relay?t=forged") as ws:
            ws.receive_json()

    token = voice.issue_token()
    with client.websocket_connect(f"/twilio/voice/relay?t={token}") as ws:
        ws.send_json(setup_msg())
        assert ws.receive_json()["type"] == "text"
    with pytest.raises(WebSocketDisconnect):  # already used
        with client.websocket_connect(f"/twilio/voice/relay?t={token}") as ws:
            ws.receive_json()


# ---------------------------------------------------------------- conversation

def test_setup_greets_then_prompts_go_to_the_brain(client, brain):
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        assert ws.receive_json() == {"type": "text", "token": "you said (nothing)", "last": True}
        ws.send_json({"type": "prompt", "voicePrompt": "I need food stamps", "lang": "en-US", "last": True})
        assert ws.receive_json()["token"] == "you said I need food stamps"
    assert [(r.phone, r.channel, r.text) for r in brain.requests] == [
        (CALLER, "voice", ""), (CALLER, "voice", "I need food stamps")]
    with session_scope() as s:
        latency = s.exec(select(Event).where(Event.type == "voice_latency")).all()
    assert len(latency) == 1 and latency[0].data["ms"] >= 0 and latency[0].channel == "voice"


def test_language_switch_changes_tts_and_transcription(client, brain):
    brain.replies["español por favor"] = TurnResult(reply="Claro, sigamos en español.", language="es")
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        ws.receive_json()
        ws.send_json({"type": "prompt", "voicePrompt": "español por favor", "last": True})
        assert ws.receive_json() == {"type": "language", "ttsLanguage": "es-US", "transcriptionLanguage": "es-US"}
        assert ws.receive_json()["token"] == "Claro, sigamos en español."


def test_end_call_ends_session_after_speaking(client, brain):
    brain.replies["bye"] = TurnResult(reply="Goodbye!", end_call=True)
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        ws.receive_json()
        ws.send_json({"type": "prompt", "voicePrompt": "bye", "last": True})
        assert ws.receive_json()["token"] == "Goodbye!"
        assert ws.receive_json() == {"type": "end"}


def test_followup_sms_sent(client, brain, monkeypatch):
    sent = []
    monkeypatch.setattr(voice, "send_sms", lambda to, body: sent.append((to, body)))
    brain.replies["photo"] = TurnResult(reply="I'll text you now.", followup_sms=["Reply with a photo."])
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        ws.receive_json()
        ws.send_json({"type": "prompt", "voicePrompt": "photo", "last": True})
        ws.receive_json()
        ws.send_json({"type": "prompt", "voicePrompt": "ok", "last": True})  # let the background send finish
        ws.receive_json()
    assert sent == [(CALLER, "Reply with a photo.")]


def test_keypad_digits_collected_into_one_turn(client, brain):
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        ws.receive_json()
        for d in "1234":
            ws.send_json({"type": "dtmf", "digit": d})
        assert ws.receive_json()["token"] == "you said 1234"
        ws.send_json({"type": "dtmf", "digit": "2"})
        ws.send_json({"type": "dtmf", "digit": "#"})
        assert ws.receive_json()["token"] == "you said 2"


def test_brain_error_is_spoken_not_dropped(client, monkeypatch):
    monkeypatch.setattr(voice, "handle_turn", lambda req: (_ for _ in ()).throw(RuntimeError("boom")))
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        assert ws.receive_json()["token"] == voice.SORRY["en"]


def test_interrupt_and_error_messages_are_harmless(client, brain):
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        ws.receive_json()
        ws.send_json({"type": "interrupt", "utteranceUntilInterrupt": "I can", "durationUntilInterruptMs": 900})
        ws.send_json({"type": "error", "description": "something"})
        ws.send_json({"type": "prompt", "voicePrompt": "still here", "last": True})
        assert ws.receive_json()["token"] == "you said still here"


def test_speech_seconds_scales_and_caps():
    assert voice.speech_seconds("") == 1.0
    assert 3 < voice.speech_seconds(" ".join(["word"] * 10)) < 6
    assert voice.speech_seconds(" ".join(["word"] * 1000)) == 15.0


# ---------------------------------------------------------------- demo hardening (B6)

def test_slow_brain_says_one_moment_first(client, monkeypatch):
    import time as _time

    def slow(req):
        _time.sleep(0.3 if req.text else 0)
        return TurnResult(reply=f"answer to {req.text}")

    monkeypatch.setattr(voice, "handle_turn", slow)
    monkeypatch.setattr(voice, "FILLER_AFTER_S", 0.05)
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        ws.receive_json()
        ws.send_json({"type": "prompt", "voicePrompt": "hello", "last": True})
        assert ws.receive_json()["token"] == voice.FILLER["en"]
        assert ws.receive_json()["token"] == "answer to hello"


def test_brain_that_never_answers_gets_an_apology(client, monkeypatch):
    import time as _time

    monkeypatch.setattr(voice, "handle_turn", lambda req: _time.sleep(0.5) or TurnResult(reply="too late"))
    monkeypatch.setattr(voice, "FILLER_AFTER_S", 0.05)
    monkeypatch.setattr(voice, "TURN_TIMEOUT_S", 0.15)
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        assert ws.receive_json()["token"] == voice.FILLER["en"]
        assert ws.receive_json()["token"] == voice.SORRY["en"]


def test_fast_brain_has_no_filler(client, brain):
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        assert ws.receive_json()["token"] == "you said (nothing)"


def test_recording_announced_and_started_when_enabled(client, brain, monkeypatch):
    monkeypatch.setenv("FORMLINE_RECORD_CALLS", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    started = []
    monkeypatch.setattr(voice, "start_recording", lambda sid: started.append(sid))
    relay = relay_element(client.post("/twilio/voice", data={"From": CALLER}).text)
    assert relay.get("welcomeGreeting") == voice.GREETING["en"] + voice.RECORDING_NOTICE["en"]
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        ws.receive_json()
    assert started == ["CA1"]


def test_recording_off_by_default(client, brain, monkeypatch):
    started = []
    monkeypatch.setattr(voice, "start_recording", lambda sid: started.append(sid))
    relay = relay_element(client.post("/twilio/voice", data={"From": CALLER}).text)
    assert relay.get("welcomeGreeting") == voice.GREETING["en"]
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        ws.receive_json()
    assert started == []


# ---------------------------------------------------------------- automatic language detection


def _autodetect(monkeypatch):
    monkeypatch.setenv("FORMLINE_VOICE_AUTODETECT", "true")
    from app.config import get_settings

    get_settings.cache_clear()


def test_autodetect_listens_for_both_languages(client, monkeypatch):
    _autodetect(monkeypatch)
    relay = relay_element(client.post("/twilio/voice", data={"From": CALLER}).text)
    assert relay.get("transcriptionLanguage") == "multi" and relay.get("speechModel") == "nova-3-general"


def test_detected_language_reaches_the_brain_and_switch_keeps_listening_for_both(client, brain, monkeypatch):
    _autodetect(monkeypatch)
    brain.replies["hola"] = TurnResult(reply="¡Hola!", language="es")
    with client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json(setup_msg())
        ws.receive_json()
        ws.send_json({"type": "prompt", "voicePrompt": "hola", "lang": "es-US", "last": True})
        assert ws.receive_json() == {"type": "language", "ttsLanguage": "es-US"}  # recognition stays "multi"
        assert ws.receive_json()["token"] == "¡Hola!"
    assert brain.requests[-1].language_hint == "es-US"
