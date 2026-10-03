"""Reminder texts for deadlines and follow-ups. Owner: Lane B.

Callers: Lane A (after a document with a deadline, with the person's OK), Lane D (dashboard
"Send reminder now").
"""

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select

from app.channels.outbound import send_sms
from app.db import session_scope
from app.events import log_activity, log_event
from app.models import Profile, Reminder


def create_reminder(profile_id: int, due_at: datetime, message: str, *, task_id: Optional[int] = None,
                    document_id: Optional[int] = None) -> int:
    with session_scope() as s:
        r = Reminder(profile_id=profile_id, due_at=due_at, message=message, task_id=task_id,
                     document_id=document_id)
        s.add(r)
        s.commit()
        s.refresh(r)
    log_activity("reminder_scheduled", f"Scheduled a reminder for {due_at:%b %d}: {message}",
                 profile_id=profile_id, task_id=task_id)
    return r.id


def send_now(reminder_id: int) -> bool:
    """Send one reminder immediately (demo control). Returns False if already sent or missing."""
    with session_scope() as s:
        r = s.get(Reminder, reminder_id)
        if r is None or r.status != "pending":
            return False
        profile = s.get(Profile, r.profile_id)
        send_sms(profile.phone, r.message, profile_id=r.profile_id, task_id=r.task_id)
        r.status = "sent"
        r.sent_at = datetime.now(timezone.utc)
        s.add(r)
        s.commit()
    log_event("reminder_sent", profile_id=r.profile_id, reminder_id=reminder_id)
    log_activity("reminder_sent", f"Sent reminder: {r.message}", profile_id=r.profile_id, task_id=r.task_id)
    return True


def pending_reminders(profile_id: Optional[int] = None) -> list[Reminder]:
    with session_scope() as s:
        q = select(Reminder).where(Reminder.status == "pending")
        if profile_id is not None:
            q = q.where(Reminder.profile_id == profile_id)
        return list(s.exec(q.order_by(Reminder.due_at)).all())


# TODO(Lane B, Phase B4): APScheduler job that calls send_now() for reminders whose due_at has passed.


def start_scheduler() -> None:
    """Called once at app startup (app/main.py). No-op until Phase B4."""


def stop_scheduler() -> None:
    """Called at app shutdown."""
