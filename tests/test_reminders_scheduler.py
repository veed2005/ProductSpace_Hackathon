import threading
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app import reminders
from app.core import identity
from app.db import session_scope
from app.models import Activity, Event, Reminder

PHONE = "+15550006666"


def _profile() -> int:
    return identity.create_profile(PHONE).id


def _status(rid: int) -> str:
    with session_scope() as s:
        return s.get(Reminder, rid).status


def _capture(monkeypatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(reminders, "send_sms", lambda to, body, **kw: sent.append((to, body)))
    return sent


def test_send_now_texts_once_and_records_it(monkeypatch):
    sent = _capture(monkeypatch)
    rid = reminders.create_reminder(_profile(), datetime.now(timezone.utc) + timedelta(days=1), "Interview Friday")
    assert reminders.send_now(rid) is True
    assert reminders.send_now(rid) is False  # already sent
    assert sent == [(PHONE, "Interview Friday")]
    with session_scope() as s:
        r = s.get(Reminder, rid)
        assert r.status == "sent" and r.sent_at is not None
        assert s.exec(select(Event).where(Event.type == "reminder_sent")).one().data == {"reminder_id": rid}


def test_send_now_missing_reminder():
    assert reminders.send_now(999) is False


def test_scheduler_job_sends_only_due_reminders(monkeypatch):
    sent = _capture(monkeypatch)
    pid = _profile()
    now = datetime.now(timezone.utc)
    due = reminders.create_reminder(pid, now - timedelta(minutes=1), "due now")
    later = reminders.create_reminder(pid, now + timedelta(hours=1), "later")
    assert reminders.send_due_reminders(now) == 1
    assert sent == [(PHONE, "due now")]
    assert _status(due) == "sent" and _status(later) == "pending"
    assert reminders.send_due_reminders(now + timedelta(hours=2)) == 1
    assert _status(later) == "sent"


def test_due_time_compared_in_utc(monkeypatch):
    _capture(monkeypatch)
    chicago = timezone(timedelta(hours=-5))
    now = datetime.now(timezone.utc)
    # 10 minutes from now, expressed in Chicago time: not due yet.
    rid = reminders.create_reminder(_profile(), (now + timedelta(minutes=10)).astimezone(chicago), "soon")
    assert reminders.send_due_reminders(now) == 0
    assert reminders.send_due_reminders(now + timedelta(minutes=11)) == 1
    assert _status(rid) == "sent"


def test_failed_text_marked_failed_and_not_retried(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("Twilio said no")

    monkeypatch.setattr(reminders, "send_sms", boom)
    rid = reminders.create_reminder(_profile(), datetime.now(timezone.utc) - timedelta(minutes=1), "x")
    assert reminders.send_due_reminders() == 0
    assert _status(rid) == "failed"
    assert reminders.due_reminders() == []  # the scheduler won't keep retrying it
    with session_scope() as s:
        assert s.exec(select(Activity).where(Activity.kind == "reminder_failed")).first()


def test_scheduler_and_send_now_never_double_send(monkeypatch):
    sent = []
    gate = threading.Event()

    def slow_send(to, body, **kw):
        gate.wait(2)
        sent.append(body)

    monkeypatch.setattr(reminders, "send_sms", slow_send)
    rid = reminders.create_reminder(_profile(), datetime.now(timezone.utc) - timedelta(minutes=1), "once")
    results = []
    threads = [threading.Thread(target=lambda: results.append(reminders.send_now(rid))) for _ in range(3)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join()
    assert sorted(results) == [False, False, True]
    assert sent == ["once"]


def test_full_ssn_never_goes_out(monkeypatch):
    sent = _capture(monkeypatch)
    rid = reminders.create_reminder(_profile(), datetime.now(timezone.utc),
                                    "Bring card 123-45-6789 and 987654321, case IL-SNAP-448120")
    reminders.send_now(rid)
    body = sent[0][1]
    assert "123-45-6789" not in body and "987654321" not in body
    assert "6789" in body and "4321" in body and "IL-SNAP-448120" in body


def test_canceled_reminder_not_sent(monkeypatch):
    sent = _capture(monkeypatch)
    rid = reminders.create_reminder(_profile(), datetime.now(timezone.utc) - timedelta(minutes=1), "nope")
    with session_scope() as s:
        r = s.get(Reminder, rid)
        r.status = "canceled"
        s.add(r)
        s.commit()
    assert reminders.send_due_reminders() == 0 and not sent


def test_scheduler_starts_once_and_stops():
    reminders.start_scheduler()
    first = reminders._scheduler
    reminders.start_scheduler()
    assert reminders._scheduler is first and first.running
    assert first.get_job("send_due_reminders") is not None
    reminders.stop_scheduler()
    assert reminders._scheduler is None


def test_cli_test_command_schedules_a_reminder(capsys):
    assert reminders._main(["test", PHONE, "--minutes", "1"]) == 0
    assert "reminder #" in capsys.readouterr().out
    assert len(reminders.pending_reminders()) == 1
