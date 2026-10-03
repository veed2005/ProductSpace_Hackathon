from datetime import date, timedelta

from sqlmodel import select

from app.contracts import Deadline, DocumentExplanation, TurnRequest
from app.core import identity
from app.core.turn import handle_turn
from app.db import session_scope
from app.models import Document, Event, Reminder, Task


def test_document_deadline_and_related_form_are_consent_gated(monkeypatch):
    phone = "+15550011010"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    sess = identity.get_session(phone)
    sess.profile_id = profile.id
    sess.state = "menu"
    identity.save_session(sess)
    explanation = DocumentExplanation(
        document_type="court notice",
        plain_summary="A court hearing is scheduled about your benefits.",
        deadlines=[Deadline.model_construct(date=date.today() + timedelta(days=4), description="Court hearing")],
        reference_numbers=["CASE-123"],
        related_form_id="sample_benefits",
        high_stakes=True,
        confidence=0.94,
    )
    monkeypatch.setattr("app.core.turn.document_engine.explain_document", lambda *_args, **_kwargs: explanation)

    result = handle_turn(TurnRequest(phone=phone, channel="sms", text="explain this", media_paths=["notice.jpg"]))
    assert "legal" in result.reply.lower()
    assert "reminder" in result.reply.lower()
    with session_scope() as db:
        document = db.exec(select(Document).where(Document.profile_id == profile.id)).first()
        assert document.document_type == "court notice"
        assert db.exec(select(Reminder).where(Reminder.profile_id == profile.id)).first() is None

    reminder_offer = handle_turn(TurnRequest(phone=phone, channel="sms", text="yes"))
    assert "related form" in reminder_offer.reply.lower()
    with session_scope() as db:
        reminder = db.exec(select(Reminder).where(Reminder.profile_id == profile.id)).first()
        assert reminder is not None and reminder.document_id == document.id

    form_prompt = handle_turn(TurnRequest(phone=phone, channel="sms", text="yes"))
    assert "full name" in form_prompt.reply.lower()
    with session_scope() as db:
        task = db.exec(select(Task).where(Task.profile_id == profile.id)).first()
        event = db.exec(select(Event).where(Event.type == "form_started", Event.task_id == task.id)).first()
        assert event.data["from_document_id"] == document.id


def test_unclear_document_requests_retake_without_guessing(monkeypatch):
    phone = "+15550011111"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    sess = identity.get_session(phone)
    sess.profile_id = profile.id
    sess.state = "menu"
    identity.save_session(sess)
    explanation = DocumentExplanation(
        document_type="unknown", plain_summary="Maybe a court notice.", confidence=0.2,
        unreadable_parts=["date"], high_stakes=True,
    )
    monkeypatch.setattr("app.core.turn.document_engine.explain_document", lambda *_args, **_kwargs: explanation)

    result = handle_turn(TurnRequest(phone=phone, channel="sms", text="letter", media_paths=["blurry.jpg"]))

    assert "clearer" in result.reply.lower()
    assert "legal" in result.reply.lower()
    with session_scope() as db:
        assert db.exec(select(Reminder)).first() is None