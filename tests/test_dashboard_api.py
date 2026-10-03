from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.core import identity
from app.dashboard.api import mask_message
from app.dashboard.demo import DemoPhones, seed_demo
from app.db import session_scope
from app.events import log_message
from app.models import Task

PHONE = DemoPhones().returning


def _client():
    from app.main import app
    return TestClient(app)


def _active_task(answers: dict, current: str = "monthly_income", pdf: str | None = None) -> int:
    pid = identity.profiles_for_phone(PHONE)[0].id
    with session_scope() as s:
        t = Task(profile_id=pid, kind="fill_form", form_id="sample_benefits", status="active",
                 answers=answers, current_field=current, output_pdf_path=pdf)
        s.add(t)
        s.commit()
        s.refresh(t)
    sess = identity.get_session(PHONE)
    sess.profile_id, sess.active_task_id = pid, t.id
    identity.save_session(sess)
    return t.id


def _ans(value, source):
    return {"value": value, "source": source, "updated_at": datetime.now(timezone.utc).isoformat()}


def test_dashboard_page_and_assets_are_served():
    with _client() as c:
        assert "Formline" in c.get("/dashboard").text
        assert c.get("/dashboard/static/dashboard.js").status_code == 200
        assert c.get("/dashboard/static/dashboard.css").status_code == 200
        assert c.get("/dashboard/static/../api.py").status_code == 404


def test_people_lists_seeded_personas_with_masked_phones():
    seed_demo()
    with _client() as c:
        people = c.get("/api/people").json()
    by_phone = {p["phone"]: p for p in people}
    maria = by_phone[PHONE]
    assert maria["phone_masked"].endswith("0101") and "202" not in maria["phone_masked"]
    assert maria["language"] == "es"
    assert by_phone[DemoPhones().shared]["profile_count"] == 2


def test_task_view_colors_fields_by_source_and_masks_sensitive():
    seed_demo()
    task_id = _active_task({
        "applicant_name": _ans({"first": "Maria", "middle": "Elena", "last": "Garcia"}, "memory"),
        "date_of_birth": _ans("1988-03-14", "memory"),
        "employed": _ans("no", "asked"),
        "monthly_income": _ans(1450, "corrected"),
        "ssn_last4": _ans("6789", "asked"),
    })
    with _client() as c:
        t = c.get(f"/api/tasks/{task_id}").json()
        assert c.get(f"/api/phones/{PHONE}/task").json()["id"] == task_id
    fields = {f["id"]: f for f in t["fields"]}
    assert fields["applicant_name"]["value"] == "Maria Elena Garcia"
    assert fields["applicant_name"]["source"] == "memory"
    assert fields["date_of_birth"]["value"] == "Mar 14, 1988"
    assert fields["monthly_income"]["value"] == "$1,450"
    assert "6789" not in fields["ssn_last4"]["value"]
    assert fields["employer"]["applies"] is False  # condition: employed == yes
    assert t["counts"]["memory"] == 2 and t["counts"]["corrected"] == 1
    assert t["progress"] == {"answered": 5, "total": 7}
    assert fields["monthly_income"]["current"] is True


def test_messages_transcript_masks_pin_like_replies():
    seed_demo()
    log_message(PHONE, "in", "voice", "Hola")
    log_message(PHONE, "out", "sms", "¿Tu PIN?")
    log_message(PHONE, "in", "sms", "1 2 3 4")
    with _client() as c:
        msgs = c.get(f"/api/phones/{PHONE}/messages").json()
    assert [m["channel"] for m in msgs] == ["voice", "sms", "sms"]
    assert msgs[0]["text"] == "Hola"
    assert "1 2 3 4" not in msgs[2]["text"]


def test_mask_message_rules():
    assert mask_message("in", "6789") != "6789"
    assert mask_message("in", "my pin is 1234") == "my pin is 1234"  # longer text is left alone
    assert mask_message("in", "about 1300 a month") == "about 1300 a month"
    assert mask_message("out", "1234") == "1234"


def test_pdf_download(tmp_path):
    seed_demo()
    pdf = tmp_path / "filled.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    task_id = _active_task({}, pdf=str(pdf))
    missing = _active_task({})
    with _client() as c:
        r = c.get(f"/api/tasks/{task_id}/pdf")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        assert c.get(f"/api/tasks/{missing}/pdf").status_code == 404
        assert c.get(f"/api/tasks/{task_id}").json()["has_pdf"] is True
