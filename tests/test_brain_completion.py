from datetime import datetime, timedelta, timezone

from app.contracts import FieldMismatch, FormField, VerificationResult
from app.core import identity
from app.core.turn import handle_turn
from app.contracts import TurnRequest
from app.db import session_scope
from app.engines import form_engine
from app.memory import profile as memory
from app.models import Message, Session, Task
from sqlmodel import select
from sqlmodel import select


ANSWERS = [
    "Ana Lopez",
    "01/02/1990",
    "12 Oak Street, Springfield, IL 62701",
    "3",
    "yes",
    "Corner Market",
    "$1,300 per month",
    "skip",
]


def _complete_sample_form():
    phone = "+15550010202"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    task = form_engine.start_form(profile.id, "sample_benefits", phone=phone, channel="sms")
    for answer in ANSWERS:
        task = form_engine.answer_field(task.id, answer)
    task, _ = form_engine.process_readback(task.id, "yes")
    return task


def test_form_completes_only_after_pdf_verification_passes():
    task = _complete_sample_form()

    assert task.status == "completed"
    assert task.output_pdf_path.endswith(f"task_{task.id}.pdf")
    assert task.verification["ok"] is True
    assert task.verification["fields_checked"] == 7


def test_readback_correction_is_recorded_and_rechecked():
    phone = "+15550010606"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    task = form_engine.start_form(profile.id, "sample_benefits", phone=phone, channel="sms")
    for answer in ANSWERS:
        task = form_engine.answer_field(task.id, answer)

    task, outcome = form_engine.process_readback(task.id, "change full name to Ana Rivera")
    assert outcome == "corrected"
    assert task.answers["applicant_name"]["value"] == "Ana Rivera"
    assert task.answers["applicant_name"]["source"] == "corrected"
    task, outcome = form_engine.process_readback(task.id, "yes")
    assert outcome == "confirmed" and task.status == "completed"


def test_messy_income_normalizes_weekly_pay_and_varies_flag():
    field = FormField(id="income", label="Income", type="money", question_hint="?")

    answer, source = form_engine._answer_from_user(None, field, "$300 a week, it changes")

    assert answer["value"] == 1299.0
    assert answer["varies"] is True
    assert source == "asked"
    assert form_engine._normalize_field_value(field, "$1,300 each month") == 1300.0


def test_failed_pdf_verification_needs_attention(monkeypatch):
    monkeypatch.setattr(
        form_engine,
        "verify_pdf",
        lambda *_args, **_kwargs: VerificationResult(
            ok=False,
            fields_checked=7,
            mismatches=[FieldMismatch(pdf_field="applicant_name", expected="Ana Lopez", actual="")],
        ),
    )

    task = _complete_sample_form()

    assert task.status == "needs_attention"
    assert task.verification["ok"] is False
    assert task.verification["mismatches"][0]["pdf_field"] == "applicant_name"


def test_failed_verification_reply_names_attention_field_without_success_receipt(monkeypatch):
    phone = "+15550011616"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    sess = identity.get_session(phone)
    sess.profile_id = profile.id
    sess.state = "menu"
    identity.save_session(sess)
    monkeypatch.setattr(
        form_engine,
        "verify_pdf",
        lambda *_args, **_kwargs: VerificationResult(
            ok=False,
            fields_checked=7,
            mismatches=[FieldMismatch(pdf_field="applicant_name", expected="Ana Lopez", actual="")],
        ),
    )

    handle_turn(TurnRequest(phone=phone, channel="sms", text="sample benefits application"))
    for answer in ANSWERS:
        result = handle_turn(TurnRequest(phone=phone, channel="sms", text=answer))
    result = handle_turn(TurnRequest(phone=phone, channel="sms", text="yes"))

    assert "couldn't verify" in result.reply.lower()
    assert "full name" in result.reply.lower()
    assert result.followup_sms == []
    with session_scope() as db:
        task = db.exec(select(Task).where(Task.profile_id == profile.id)).first()
        assert task.status == "needs_attention"


def test_verified_completion_returns_runback_and_masked_sms_receipt():
    phone = "+15550010303"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    sess = identity.get_session(phone)
    sess.profile_id = profile.id
    identity.save_session(sess)
    task = form_engine.start_form(profile.id, "sample_benefits", phone=phone, channel="sms")
    for answer in ANSWERS[:-1]:
        task = form_engine.answer_field(task.id, answer)

    handle_turn(TurnRequest(phone=phone, channel="sms", text="1234"))
    result = handle_turn(TurnRequest(phone=phone, channel="sms", text="yes"))

    assert "verified" in result.reply.lower()
    assert len(result.followup_sms) == 1
    assert "1234" not in result.followup_sms[0]
    assert identity.get_session(phone).active_task_id is None


def test_sensitive_answer_is_masked_in_message_and_dashboard_event(monkeypatch):
    phone = "+15550010404"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    sess = identity.get_session(phone)
    sess.profile_id = profile.id
    identity.save_session(sess)
    task = form_engine.start_form(profile.id, "sample_benefits", phone=phone, channel="sms")
    with session_scope() as db:
        stored_task = db.get(Task, task.id)
        stored_task.answers = {
            field_id: {"value": value, "source": "asked"}
            for field_id, value in {
                "applicant_name": "Ana Lopez",
                "date_of_birth": "1990-01-02",
                "address": "12 Oak Street",
                "household_size": 3,
                "employed": "no",
                "monthly_income": 1300,
            }.items()
        }
        stored_task.current_field = "ssn_last4"
        db.add(stored_task)
        db.commit()
        stored_session = db.exec(select(Session).where(Session.phone == phone)).first()
        stored_session.state = "form"
        stored_session.active_task_id = task.id
        db.add(stored_session)
        db.commit()

    events = []
    monkeypatch.setattr("app.engines.form_engine.publish", lambda event_type, **payload: events.append((event_type, payload)))
    handle_turn(TurnRequest(phone=phone, channel="sms", text="1234"))

    with session_scope() as db:
        inbound = db.exec(select(Message).where(Message.phone == phone, Message.direction == "in")).first()
    assert inbound.text == "[sensitive answer]"
    field_event = next(payload for kind, payload in events if kind == "field_filled")
    assert field_event["value"] == "[redacted]"


def test_memory_prefill_requires_pin_and_batch_confirmation_and_reasks_stale_value():
    phone = "+15550010808"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    identity.set_pin(profile.id, "2468")
    memory.set_fact(profile.id, "name", {"first": "Ana", "last": "Lopez"}, source_type="form")
    memory.set_fact(profile.id, "date_of_birth", "1990-01-02", source_type="form")
    memory.set_fact(profile.id, "monthly_income", 900, source_type="form",
                    confirmed_at=datetime.now(timezone.utc) - timedelta(days=40))
    sess = identity.get_session(phone)
    sess.profile_id = profile.id
    sess.pin_verified_at = datetime.now(timezone.utc)
    sess.state = "menu"
    identity.save_session(sess)

    prompt = handle_turn(TurnRequest(phone=phone, channel="sms", text="sample benefits application"))

    assert "should i use them" in prompt.reply.lower()
    assert "Ana Lopez" not in prompt.reply
    with session_scope() as db:
        task = db.exec(select(Task).where(Task.profile_id == profile.id)).first()
        assert task.answers["applicant_name"]["source"] == "memory"
        assert "monthly_income" not in task.answers
        stored_session = db.exec(select(Session).where(Session.phone == phone)).first()
        assert stored_session.pending["stale_hints"]["monthly_income"] == 900

    next_prompt = handle_turn(TurnRequest(phone=phone, channel="sms", text="yes"))
    assert "date of birth" in next_prompt.reply.lower() or "home address" in next_prompt.reply.lower()
    assert memory.get_fact(profile.id, "name").fresh


def test_sample_form_completes_in_spanish_with_messy_weekly_income():
    phone = "+15550011515"
    profile = identity.create_profile(phone, language="es", display_name="Ana")
    sess = identity.get_session(phone)
    sess.profile_id = profile.id
    sess.state = "menu"
    identity.save_session(sess)

    result = handle_turn(TurnRequest(phone=phone, channel="sms", text="sample benefits application"))
    assert "nombre completo" in result.reply.lower()
    for answer in ["Ana Lopez", "1990-01-02", "12 Oak Street, Springfield", "3", "sí",
                   "Mercado", "$300 a la semana, cambia", "paso"]:
        result = handle_turn(TurnRequest(phone=phone, channel="sms", text=answer))

    assert "esto es lo que tengo" in result.reply.lower()
    assert "1,299" in result.reply
    assert "nombre completo" in result.reply.lower()
    completed = handle_turn(TurnRequest(phone=phone, channel="sms", text="sí"))
    assert "verifiqué" in completed.reply.lower()
    assert len(completed.followup_sms) == 1