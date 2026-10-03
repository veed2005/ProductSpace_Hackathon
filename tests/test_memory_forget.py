from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.core import identity
from app.db import session_scope
from app.events import log_activity, log_event, log_message
from app.memory import profile as memory
from app.models import Activity, Document, Event, Message, Profile, ProfileFact, Reminder, Session, Task
from app.reminders import create_reminder

PHONE = "+12025550150"


def _person(name: str, tmp_path, *, with_files: bool = True) -> int:
    pid = identity.create_profile(PHONE, display_name=name).id
    memory.set_fact(pid, "monthly_income", 1300, source_type="form")
    memory.set_fact(pid, "address", {"city": "Springfield"}, source_type="form")
    pdf = tmp_path / f"filled_{name}.pdf"
    photo = tmp_path / "media" / f"letter_{name}.jpg"
    if with_files:
        pdf.write_bytes(b"%PDF")
        photo.parent.mkdir(exist_ok=True)
        photo.write_bytes(b"jpg")
    with session_scope() as s:
        doc = Document(profile_id=pid, media_paths=[str(photo)], document_type="renewal notice")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        task = Task(profile_id=pid, kind="fill_form", form_id="sample_benefits", document_id=doc.id,
                    output_pdf_path=str(pdf), answers={"applicant_name": {"value": name, "source": "asked"}})
        s.add(task)
        s.commit()
        s.refresh(task)
        tid = task.id
    log_message(PHONE, "in", "sms", f"I am {name}", profile_id=pid, task_id=tid)
    log_activity("form_completed", f"Filled a form for {name}", profile_id=pid, task_id=tid)
    log_event("form_completed", phone=PHONE, profile_id=pid, task_id=tid, duration_s=300)
    create_reminder(pid, datetime.now(timezone.utc) + timedelta(days=3), "Renew soon", task_id=tid)
    return pid


def _count(model, **where):
    with session_scope() as s:
        q = select(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return len(s.exec(q).all())


def test_forget_deletes_everything_about_the_person(tmp_path):
    from app.dashboard.demo import sync_forms
    sync_forms()
    pid = _person("Ana", tmp_path)
    sess = identity.get_session(PHONE)
    with session_scope() as s:
        task_id = s.exec(select(Task.id).where(Task.profile_id == pid)).first()
    sess.profile_id, sess.active_task_id = pid, task_id
    identity.save_session(sess)
    log_message(PHONE, "in", "sms", "hi (before we knew who this was)")

    result = memory.forget_profile(pid)

    assert result.facts == 2 and result.tasks == 1 and result.documents == 1 and result.files == 2
    assert "1 form" in result.summary()
    for model in (ProfileFact, Task, Document, Reminder, Activity):
        assert _count(model, profile_id=pid) == 0, model.__name__
    assert _count(Profile, id=pid) == 0
    assert _count(Message, phone=PHONE) == 0  # last profile on the phone: whole transcript goes
    assert _count(Session, phone=PHONE) == 0
    assert not list(tmp_path.glob("filled_*.pdf")) and not list((tmp_path / "media").glob("*"))
    # Metrics survive without identity.
    with session_scope() as s:
        ev = s.exec(select(Event).where(Event.type == "form_completed")).one()
    assert ev.profile_id is None and ev.phone is None and ev.data["duration_s"] == 300


def test_forget_on_shared_phone_leaves_the_other_person_alone(tmp_path):
    from app.dashboard.demo import sync_forms
    sync_forms()
    ana = _person("Ana", tmp_path)
    ben = _person("Ben", tmp_path)
    sess = identity.get_session(PHONE)
    sess.profile_id = ana
    identity.save_session(sess)
    log_message(PHONE, "in", "sms", "who is this line from?")

    memory.forget_profile(ana)

    assert _count(Profile, id=ben) == 1
    assert _count(ProfileFact, profile_id=ben) == 2
    assert _count(Task, profile_id=ben) == 1
    assert _count(Message, profile_id=ben) == 1
    assert _count(Message, phone=PHONE) == 2  # Ben's line + the unattributed line
    remaining = identity.get_session(PHONE)
    assert remaining.profile_id is None and remaining.state == "new"
    assert (tmp_path / "filled_Ben.pdf").exists()


def test_forget_missing_profile_is_a_no_op():
    assert memory.forget_profile(9999).facts == 0


def test_forget_never_deletes_files_outside_data_dir(tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "keep.pdf"
    outside.write_bytes(b"%PDF")
    pid = identity.create_profile(PHONE).id
    with session_scope() as s:
        s.add(Task(profile_id=pid, kind="fill_form", output_pdf_path=str(outside)))
        s.commit()
    memory.forget_profile(pid)
    assert outside.exists()


def test_freshness_boundary():
    pid = identity.create_profile(PHONE).id
    now = datetime.now(timezone.utc)
    memory.set_fact(pid, "monthly_income", 1, source_type="form", confirmed_at=now - timedelta(days=29, hours=23))
    assert memory.get_value(pid, "monthly_income").fresh
    memory.set_fact(pid, "monthly_income", 1, source_type="form", confirmed_at=now - timedelta(days=30, minutes=1))
    assert not memory.get_value(pid, "monthly_income").fresh
