import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.channels import messaging
from app.contracts import TurnRequest, TurnResult

BASE = "https://formline-test.ngrok-free.app"
TOKEN = "test-auth-token"


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def turns(monkeypatch):
    """Replace the brain; record every TurnRequest it gets."""
    seen: list[TurnRequest] = []

    def fake(req: TurnRequest) -> TurnResult:
        seen.append(req)
        return TurnResult(reply="ok")

    monkeypatch.setattr(messaging, "handle_turn", fake)
    return seen


@pytest.fixture
def signed(monkeypatch):
    """Turn signature validation on with a known token and public URL."""
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURES", "true")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    from app.config import get_settings

    get_settings.cache_clear()


def params(**extra) -> dict:
    return {"From": "+15550001111", "Body": "hi", "MessageSid": "SM" + uuid.uuid4().hex, **extra}


def sign(data: dict, url: str = BASE + "/twilio/messaging") -> dict:
    return {"X-Twilio-Signature": RequestValidator(TOKEN).compute_signature(url, data)}


# ---------------------------------------------------------------- signatures

def test_unsigned_request_allowed_when_validation_off(client, turns):
    assert client.post("/twilio/messaging", data=params()).status_code == 200
    assert len(turns) == 1


def test_missing_signature_rejected(client, turns, signed):
    assert client.post("/twilio/messaging", data=params()).status_code == 403
    assert not turns


def test_bad_signature_rejected(client, turns, signed):
    data = params()
    headers = sign({**data, "Body": "something else"})
    assert client.post("/twilio/messaging", data=data, headers=headers).status_code == 403
    assert not turns


def test_valid_signature_uses_public_url_not_local_host(client, turns, signed):
    data = params()
    resp = client.post("/twilio/messaging", data=data, headers=sign(data))
    assert resp.status_code == 200 and "<Message>ok</Message>" in resp.text
    # Signed for the local URL instead of the public one: rejected.
    resp = client.post("/twilio/messaging", data=params(),
                       headers=sign(params(), "http://testserver/twilio/messaging"))
    assert resp.status_code == 403


# ---------------------------------------------------------------- media

def test_mms_downloaded_with_auth_and_passed_to_brain(client, turns, monkeypatch, tmp_path):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    from app.config import get_settings

    get_settings.cache_clear()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/broken"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"\xff\xd8 fake jpeg", headers={"content-type": "image/jpeg"})

    real = httpx.AsyncClient
    monkeypatch.setattr(messaging.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    data = params(Body="", NumMedia="3",
                  MediaUrl0="https://api.twilio.com/media/one", MediaContentType0="image/jpeg",
                  MediaUrl1="https://api.twilio.com/media/broken", MediaContentType1="image/png",
                  MediaUrl2="https://api.twilio.com/media/two", MediaContentType2="image/png")
    assert client.post("/twilio/messaging", data=data).status_code == 200

    sid = data["MessageSid"]
    paths = turns[0].media_paths
    assert [Path(p).name for p in paths] == [f"{sid}_0.jpg", f"{sid}_2.png"]  # the broken one is skipped
    assert all(Path(p).parent == tmp_path / "media" and Path(p).read_bytes() for p in paths)
    assert all(r.headers["authorization"].startswith("Basic ") for r in requests)


def test_no_media_means_no_download(client, turns, monkeypatch):
    monkeypatch.setattr(messaging.httpx, "AsyncClient", lambda **kw: pytest.fail("should not download"))
    client.post("/twilio/messaging", data=params(NumMedia="0"))
    assert turns[0].media_paths == []


# ---------------------------------------------------------------- retries and errors

def test_twilio_retry_is_handled_once(client, turns):
    data = params()
    first = client.post("/twilio/messaging", data=data)
    retry = client.post("/twilio/messaging", data=data)
    assert "<Message>" in first.text
    assert retry.status_code == 200 and "<Message>" not in retry.text
    assert len(turns) == 1


def test_brain_error_becomes_friendly_reply(client, monkeypatch):
    def boom(req):
        raise RuntimeError("brain exploded")

    monkeypatch.setattr(messaging, "handle_turn", boom)
    resp = client.post("/twilio/messaging", data=params())
    assert resp.status_code == 200
    assert messaging.SORRY in resp.text


# ---------------------------------------------------------------- replies

def test_long_reply_split_into_several_texts(client, monkeypatch):
    long = " ".join(f"Sentence number {i} explains part of your letter." for i in range(80))
    monkeypatch.setattr(messaging, "handle_turn", lambda req: TurnResult(reply=long))
    resp = client.post("/twilio/messaging", data=params())
    assert resp.text.count("<Message>") >= 3


def test_split_reply_boundaries():
    assert messaging.split_reply("short") == ["short"]
    assert messaging.split_reply("") == []
    text = ("First paragraph. " * 60).strip() + "\n\n" + "Second paragraph. " * 60
    parts = messaging.split_reply(text, limit=1500)
    assert all(len(p) <= 1500 for p in parts)
    assert " ".join(parts).split() == text.split()  # nothing lost
    assert all(p.endswith(".") for p in parts)  # cut at sentence ends
    assert messaging.split_reply("x" * 3100, limit=1500) == ["x" * 1500, "x" * 1500, "x" * 100]


def test_followups_sent_after_the_reply(client, monkeypatch):
    sent = []
    monkeypatch.setattr(messaging, "handle_turn",
                        lambda req: TurnResult(reply="done", followup_sms=["receipt 1", "receipt 2"]))
    monkeypatch.setattr(messaging, "send_sms", lambda to, body: sent.append((to, body)))
    resp = client.post("/twilio/messaging", data=params())
    assert "<Message>done</Message>" in resp.text
    assert sent == [("+15550001111", "receipt 1"), ("+15550001111", "receipt 2")]
