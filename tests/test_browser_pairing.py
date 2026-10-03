"""Pairing the extension, its credential, and the browser websocket."""

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.browser import pairing
from app.browser.hub import hub
from app.core import identity
from app.db import session_scope
from app.main import app
from app.models import BrowserInstallation


@pytest.fixture
def codes(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(pairing, "deliver_code", lambda phone, code, delivery: sent.append((phone, code, delivery)))
    return sent


def pair_new(codes, phone="(217) 555-0104", name="Margaret", pin="4821"):
    started = pairing.start_pairing(phone)
    code = codes[-1][1]
    assert pairing.confirm_pairing(started.pairing_id, code)["status"] == "need_profile"
    return pairing.confirm_pairing(started.pairing_id, code, name=name, pin=pin, label="Chrome on Windows")


def test_new_number_pairs_with_name_and_pin(codes):
    out = pair_new(codes)
    assert out["status"] == "paired" and out["phone"] == "+12175550104"
    profile = identity.profiles_for_phone("+12175550104")[0]
    assert profile.display_name == "Margaret"
    assert identity.check_pin(profile.id, "4821")
    with session_scope() as s:
        inst = s.exec(select(BrowserInstallation)).one()
    assert inst.token_hash == hashlib.sha256(out["token"].encode()).hexdigest()  # only the hash is stored
    assert out["token"] not in inst.token_hash
    assert pairing.authenticate(out["installation_id"], out["token"]).profile_id == profile.id
    assert pairing.authenticate(out["installation_id"], "wrong") is None
    assert [p.id for p in pairing.paired_profiles("+12175550104")] == [profile.id]


def test_existing_profile_pairs_without_new_pin(codes):
    p = identity.create_profile("+12175550104", display_name="Maria")
    identity.set_pin(p.id, "1111")
    started = pairing.start_pairing("2175550104", delivery="call")
    assert codes[-1][2] == "call"  # landlines get the code by voice
    out = pairing.confirm_pairing(started.pairing_id, codes[-1][1])
    assert out["status"] == "paired" and out["profile_name"] == "Maria"


def test_shared_phone_asks_who(codes):
    for name in ("James", "Denise"):
        p = identity.create_profile("+12175550199", display_name=name)
        identity.set_pin(p.id, "2222")
    started = pairing.start_pairing("+12175550199")
    code = codes[-1][1]
    out = pairing.confirm_pairing(started.pairing_id, code)
    assert out == {"status": "choose_profile", "profiles": ["James", "Denise"]}
    out = pairing.confirm_pairing(started.pairing_id, code, name="Denise")
    assert out["status"] == "paired" and out["profile_name"] == "Denise"


def test_wrong_codes_lock_the_request_and_codes_are_single_use(codes):
    started = pairing.start_pairing("+12175550104")
    for _ in range(pairing.MAX_CODE_ATTEMPTS):
        with pytest.raises(pairing.PairingError) as e:
            pairing.confirm_pairing(started.pairing_id, "000000" if codes[-1][1] != "000000" else "111111")
        assert e.value.code == "wrong_code"
    with pytest.raises(pairing.PairingError) as e:
        pairing.confirm_pairing(started.pairing_id, codes[-1][1])
    assert e.value.code == "too_many_attempts"

    out = pair_new(codes, phone="+12175550105")
    with pytest.raises(pairing.PairingError):
        pairing.confirm_pairing(out["installation_id"], "123456")


def test_rate_limit_and_bad_phone(codes):
    for _ in range(pairing.MAX_REQUESTS_PER_PHONE):
        pairing.start_pairing("+12175550104")
    with pytest.raises(pairing.PairingError) as e:
        pairing.start_pairing("+12175550104")
    assert e.value.code == "rate_limited"
    with pytest.raises(pairing.PairingError) as e:
        pairing.start_pairing("555")
    assert e.value.code == "bad_phone"


def test_revoked_installation_cannot_connect(codes):
    out = pair_new(codes)
    pairing.revoke(out["installation_id"])
    assert pairing.authenticate(out["installation_id"], out["token"]) is None


def test_forget_me_removes_paired_browsers(codes):
    from app.memory.profile import forget_profile

    out = pair_new(codes)
    forget_profile(identity.profiles_for_phone("+12175550104")[0].id)
    assert pairing.authenticate(out["installation_id"], out["token"]) is None


# ---------------------------------------------------------------- HTTP and websocket

def test_pairing_endpoints_and_dev_code(monkeypatch):
    monkeypatch.setenv("FORMLINE_DEV_ENDPOINTS", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    with TestClient(app) as client:
        r = client.post("/browser/pair/start", json={"phone": "217-555-0104"})
        assert r.status_code == 200
        body = r.json()
        assert len(body["dev_code"]) == 6  # only because Twilio isn't configured and dev endpoints are on
        r = client.post("/browser/pair/confirm", json={"pairing_id": body["pairing_id"], "code": "000000"})
        assert r.status_code in (400, 200)
        r = client.post("/browser/pair/confirm", json={"pairing_id": body["pairing_id"], "code": body["dev_code"],
                                                       "name": "Margaret", "pin": "4821"})
        assert r.json()["status"] == "paired"
        assert client.post("/browser/pair/start", json={"phone": "12"}).status_code == 400


def test_no_dev_code_without_dev_mode(codes):
    assert pairing.start_pairing("+12175550104").dev_code is None


def test_websocket_requires_token_and_registers_browser(codes):
    out = pair_new(codes)
    with TestClient(app) as client:
        with client.websocket_connect("/browser/ws", headers={"origin": "chrome-extension://abc"}) as ws:
            ws.send_json({"type": "hello", "installation_id": out["installation_id"], "token": out["token"],
                          "tab": {"tab_id": 7, "url": "https://portal.test/", "title": "Portal"}})
            assert ws.receive_json() == {"type": "welcome", "profile_name": "Margaret"}
            profile_id = identity.profiles_for_phone("+12175550104")[0].id
            conns = hub.for_profile(profile_id)
            assert len(conns) == 1 and conns[0].tab["tab_id"] == 7
        assert hub.for_profile(profile_id) == []  # gone when the socket closes

        with client.websocket_connect("/browser/ws") as ws:
            ws.send_json({"type": "hello", "installation_id": out["installation_id"], "token": "nope"})
            assert ws.receive_json() == {"type": "error", "error": "unauthorized"}


def test_websocket_refuses_web_pages():
    from starlette.websockets import WebSocketDisconnect

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/browser/ws", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_json()


def test_failed_delivery_explains_why_and_does_not_use_up_the_limit(monkeypatch):
    class TwilioRefused(Exception):
        code = 21215

    def refuse(phone, code, delivery):
        raise TwilioRefused("Account not authorized to call +17733086960")

    monkeypatch.setattr(pairing, "deliver_code", refuse)
    for _ in range(pairing.MAX_REQUESTS_PER_PHONE + 1):
        with pytest.raises(pairing.PairingError) as e:
            pairing.start_pairing("+17733086960", delivery="call")
        assert e.value.code == "delivery_failed"
        assert "isn't allowed to call that number" in e.value.message and "Text me" in e.value.message
    with session_scope() as s:
        from app.models import PairingRequest

        assert s.exec(select(PairingRequest)).all() == []  # never counted toward the limit
