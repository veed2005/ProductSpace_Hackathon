from fastapi.testclient import TestClient

from app.core import identity
from app.dashboard.demo import DemoPhones, seed_demo
from app.reminders import pending_reminders


def _client():
    from app.main import app
    return TestClient(app)


def test_dashboard_refuses_proxied_requests_but_twilio_still_works():
    with _client() as c:
        assert c.get("/api/people").status_code == 200
        for header in ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded"):
            assert c.get("/api/people", headers={header: "1.2.3.4"}).status_code == 403
            assert c.post("/api/demo/reset", headers={header: "1.2.3.4"}).status_code == 403
        assert c.get("/dashboard", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403
        r = c.post("/twilio/messaging", data={"From": "+15550001111", "Body": "hi"},
                   headers={"X-Forwarded-For": "1.2.3.4"})
        assert r.status_code == 200


def test_remote_override(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("FORMLINE_DASHBOARD_REMOTE", "true")
    get_settings.cache_clear()
    with _client() as c:
        assert c.get("/api/people", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 200
    get_settings.cache_clear()


def test_seed_and_reset_with_a_real_demo_phone():
    with _client() as c:
        r = c.post("/api/demo/seed", json={"returning_phone": "+12175550123"})
        assert r.status_code == 200 and r.json()["returning"]["phone"] == "+12175550123"
        assert identity.profiles_for_phone("+12175550123")
        assert c.post("/api/demo/seed", json={"returning_phone": "not a phone"}).status_code == 400
        identity.create_profile("+12025550199")
        assert c.post("/api/demo/reset").status_code == 200
    assert identity.profiles_for_phone("+12025550199") == []
    assert identity.profiles_for_phone(DemoPhones().returning)


def test_send_reminder_now_and_reset_pin():
    seed_demo()
    reminder = pending_reminders()[0]
    maria = identity.profiles_for_phone(DemoPhones().returning)[0]
    with _client() as c:
        state = c.get("/api/demo/state").json()
        assert state["pins"]["maria"] == "1234" and state["reminders"][0]["id"] == reminder.id
        assert c.post(f"/api/demo/reminders/{reminder.id}/send").json() == {"sent": True}
        assert c.post(f"/api/demo/reminders/{reminder.id}/send").status_code == 404  # already sent
        assert c.post(f"/api/profiles/{maria.id}/reset-pin").json() == {"reset": True}
        assert c.post("/api/profiles/9999/reset-pin").status_code == 404
        assert c.get("/favicon.ico").headers["content-type"].startswith("image/svg")
    assert pending_reminders() == []
    assert not identity.check_pin(maria.id, "1234")


def test_completed_task_shows_comparison_with_first_form():
    from datetime import datetime, timedelta, timezone

    from app.db import session_scope
    from app.models import Task
    seed_demo()
    maria = identity.profiles_for_phone(DemoPhones().returning)[0]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        t = Task(profile_id=maria.id, kind="fill_form", form_id="sample_benefits", status="completed",
                 started_at=now - timedelta(seconds=198), completed_at=now)
        s.add(t)
        s.commit()
        s.refresh(t)
    with _client() as c:
        comp = c.get(f"/api/tasks/{t.id}").json()["comparison"]
    assert comp["first_s"] == 660 and comp["this_s"] == 198 and comp["faster_pct"] == 70
