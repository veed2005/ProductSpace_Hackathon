"""Reminder texts for deadlines and follow-ups. Owner: Lane B.

Callers: Lane A (after a document with a deadline, with the person's OK), Lane D (dashboard
"Send reminder now").

A background scheduler (started with the app) checks every minute and sends reminders whose
`due_at` has passed. Reminder.status: pending -> sending -> sent | failed, or canceled.
Sending claims the row first, so the scheduler and "Send now" can never text the same reminder twice.
A failed send is not retried automatically (no repeated texts if Twilio rejects the number).

  uv run python -m app.reminders list                       # pending reminders
  uv run python -m app.reminders send <id>                  # send one now
  uv run python -m app.reminders test +1XXXXXXXXXX [--minutes 1]   # schedule a test reminder
"""

import logging
import re
import sys
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import update
from sqlmodel import select

from app.channels.outbound import send_sms
from app.db import session_scope
from app.events import log_activity, log_event
from app.models import Profile, Reminder

log = logging.getLogger(__name__)

CHECK_EVERY_S = 60

# Full SSNs (with or without dashes) never go out by text. Case numbers and dates are fine.
_SSN = re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b")


def scrub(message: str) -> str:
    """Mask anything that looks like a full SSN."""
    return _SSN.sub(lambda m: "•••-••-" + re.sub(r"\D", "", m.group())[-4:], message)


def _utc(dt: datetime) -> datetime:
    """Aware UTC. Naive datetimes are taken as UTC (SQLite drops tzinfo)."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def create_reminder(profile_id: int, due_at: datetime, message: str, *, task_id: Optional[int] = None,
                    document_id: Optional[int] = None) -> int:
    due_at = _utc(due_at)
    message = scrub(message)
    with session_scope() as s:
        r = Reminder(profile_id=profile_id, due_at=due_at, message=message, task_id=task_id,
                     document_id=document_id)
        s.add(r)
        s.commit()
        s.refresh(r)
    log_activity("reminder_scheduled", f"Scheduled a reminder for {due_at:%b %d}: {message}",
                 profile_id=profile_id, task_id=task_id)
    return r.id


def _claim(reminder_id: int) -> bool:
    """Atomically move a pending reminder to 'sending'. False if someone else got it first."""
    with session_scope() as s:
        result = s.execute(update(Reminder).where(Reminder.id == reminder_id, Reminder.status == "pending")
                        .values(status="sending"))
        s.commit()
        return result.rowcount == 1


def send_now(reminder_id: int) -> bool:
    """Send one reminder immediately (demo control and scheduler). Returns False if it was
    missing, already sent or being sent, canceled, or the text failed."""
    if not _claim(reminder_id):
        return False
    with session_scope() as s:
        r = s.get(Reminder, reminder_id)
        phone = s.get(Profile, r.profile_id).phone
    try:
        send_sms(phone, scrub(r.message), profile_id=r.profile_id, task_id=r.task_id)
        ok = True
    except Exception:
        log.exception("reminder %s failed to send", reminder_id)
        ok = False
    with session_scope() as s:
        r = s.get(Reminder, reminder_id)
        r.status = "sent" if ok else "failed"
        r.sent_at = datetime.now(timezone.utc) if ok else None
        s.add(r)
        s.commit()
    if not ok:
        log_activity("reminder_failed", f"Couldn't send reminder: {r.message}", profile_id=r.profile_id,
                     task_id=r.task_id)
        return False
    log_event("reminder_sent", profile_id=r.profile_id, reminder_id=reminder_id)
    log_activity("reminder_sent", f"Sent reminder: {r.message}", profile_id=r.profile_id, task_id=r.task_id)
    return True


def pending_reminders(profile_id: Optional[int] = None) -> list[Reminder]:
    with session_scope() as s:
        q = select(Reminder).where(Reminder.status == "pending")
        if profile_id is not None:
            q = q.where(Reminder.profile_id == profile_id)
        return list(s.exec(q.order_by(Reminder.due_at)).all())


def due_reminders(now: Optional[datetime] = None) -> list[Reminder]:
    now = now or datetime.now(timezone.utc)
    return [r for r in pending_reminders() if _utc(r.due_at) <= now]


def send_due_reminders(now: Optional[datetime] = None) -> int:
    """Send every pending reminder whose time has come. Returns how many were sent."""
    sent = 0
    for r in due_reminders(now):
        try:
            sent += send_now(r.id)
        except Exception:  # never let one reminder kill the scheduler job
            log.exception("reminder %s crashed while sending", r.id)
    return sent


# ---------------------------------------------------------------- scheduler

_scheduler = None


def start_scheduler() -> None:
    """Called once at app startup (app/main.py). First check runs one interval after startup."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(timezone=timezone.utc)
    _scheduler.add_job(send_due_reminders, "interval", seconds=CHECK_EVERY_S, id="send_due_reminders",
                       coalesce=True, max_instances=1)
    _scheduler.start()
    log.info("reminder scheduler started (every %ss)", CHECK_EVERY_S)


def stop_scheduler() -> None:
    """Called at app shutdown."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


# ---------------------------------------------------------------- CLI

def _main(argv: list[str]) -> int:
    import argparse
    from datetime import timedelta

    from app.core import identity
    from app.db import init_db

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    send = sub.add_parser("send")
    send.add_argument("id", type=int)
    test = sub.add_parser("test")
    test.add_argument("phone")
    test.add_argument("--minutes", type=float, default=1)
    args = ap.parse_args(argv)
    init_db()

    if args.cmd == "list":
        for r in pending_reminders():
            print(f"#{r.id}  due {_utc(r.due_at):%Y-%m-%d %H:%M} UTC  profile {r.profile_id}  {r.message}")
    elif args.cmd == "send":
        print("sent" if send_now(args.id) else "not sent (missing, not pending, or the text failed)")
    elif args.cmd == "test":
        profiles = identity.profiles_for_phone(args.phone)
        pid = profiles[0].id if profiles else identity.create_profile(args.phone, display_name="Test").id
        due = datetime.now(timezone.utc) + timedelta(minutes=args.minutes)
        rid = create_reminder(pid, due, "Formline test reminder. Reply STOP to opt out.")
        print(f"reminder #{rid} due {due:%H:%M:%S} UTC; the running server sends it within a minute after that")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
