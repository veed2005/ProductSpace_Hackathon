"""Wiring tests across modules. If one of these breaks, a contract between modules broke."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.contracts import TurnRequest
from app.core import identity
from app.core.turn import handle_turn
from app.engines import form_library
from app.memory import profile as memory
from app.pdf.fill import fill_pdf
from app.pdf.verify import verify_pdf


def test_handle_turn_returns_reply_on_both_channels():
    for channel in ("sms", "voice"):
        result = handle_turn(TurnRequest(phone="+15550001111", channel=channel, text="hello"))
        assert result.reply


def test_sms_webhook_returns_twiml():
    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/twilio/messaging", data={"From": "+15550001111", "Body": "hi"})
    assert resp.status_code == 200
    assert "<Message>" in resp.text


def test_every_form_in_library_loads_and_has_its_pdf():
    forms = form_library.list_forms()
    assert forms, "form library is empty"
    for meta in forms:
        schema = form_library.load_schema(meta.form_id)
        assert schema.form_id == meta.form_id
        assert form_library.pdf_path(meta.form_id).exists(), f"{meta.form_id} is missing form.pdf"
        ids = [f.id for f in schema.fields]
        assert len(ids) == len(set(ids)), f"{meta.form_id} has duplicate field ids"


def test_schema_pdf_fields_exist_in_pdf():
    from app.pdf.verify import read_fields

    for meta in form_library.list_forms():
        pdf_fields = set(read_fields(form_library.pdf_path(meta.form_id)))
        for field in form_library.load_schema(meta.form_id).fields:
            if field.pdf_field:
                assert field.pdf_field in pdf_fields, f"{meta.form_id}: {field.pdf_field} not in PDF"


def test_fill_and_verify_round_trip(tmp_path: Path):
    values = {"applicant_name": "Ana Lopez", "monthly_income": "1300"}
    out = fill_pdf(form_library.pdf_path("sample_benefits"), values, tmp_path / "out.pdf")
    assert verify_pdf(out, values).ok
    assert not verify_pdf(out, {"applicant_name": "Someone Else"}).ok


def test_memory_and_pin():
    p = identity.create_profile("+15550002222", language="es")
    memory.set_fact(p.id, "address", "412 Elm St", source_type="conversation")
    fact = memory.get_fact(p.id, "address")
    assert fact.value == "412 Elm St" and fact.fresh
    identity.set_pin(p.id, "1234")
    assert identity.check_pin(p.id, "1234")
    assert not identity.check_pin(p.id, "0000")


def test_dev_turn_endpoint_is_off_by_default():
    from app.main import app

    with TestClient(app) as client:
        assert client.post("/dev/turn", json={"phone": "+1555", "channel": "sms", "text": "hi"}).status_code == 404


def test_startup_syncs_form_library_so_tasks_can_reference_forms():
    from app.db import session_scope
    from app.main import app
    from app.models import Task

    with TestClient(app):
        p = identity.create_profile("+15550003333")
        with session_scope() as s:
            s.add(Task(profile_id=p.id, kind="fill_form", form_id="sample_benefits"))
            s.commit()
