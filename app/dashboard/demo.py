"""Demo personas, seeding, and reset. Owner: Lane D.

Used by scripts/seed_demo.py, scripts/reset_demo.py, and (Phase D5) the dashboard's demo controls.
All personas are fake. Phone numbers default to the 555-01xx range reserved for fiction; pass real
team phones for the live demo.
"""

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import SQLModel, select

from app.config import get_settings
from app.core import identity
from app.db import get_engine, init_db, session_scope
from app.engines import form_library
from app.memory import profile as memory
from app.reminders import create_reminder
from app.models import Activity, Document, Event, Form, Message, Profile, ProfileFact, Reminder, Session, Task


@dataclass
class DemoPhones:
    returning: str = "+12025550101"  # Maria: full profile, stale income, one completed form
    new: str = "+12025550102"  # nobody: first contact during the demo
    shared: str = "+12025550103"  # James and Denise share this phone


DEMO_PINS = {"maria": "1234", "james": "1111", "denise": "2222"}


def _ago(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def sync_forms() -> None:
    """Mirror the on-disk form library into the Form table (Task.form_id references it)."""
    with session_scope() as s:
        for meta in form_library.list_forms():
            schema = form_library.load_schema(meta.form_id)
            row = s.get(Form, meta.form_id) or Form(id=meta.form_id, name=meta.name)
            row.name, row.aliases, row.agency, row.description = meta.name, meta.aliases, meta.agency, meta.description
            row.reviewed, row.field_count = schema.reviewed, len(schema.fields)
            s.add(row)
        s.commit()


def _clear_phone(phone: str) -> None:
    """Remove any existing data on this phone so seeding is repeatable (children before parents)."""
    with session_scope() as s:
        pids = [p.id for p in s.exec(select(Profile).where(Profile.phone == phone)).all()]
        for sess in s.exec(select(Session).where(Session.phone == phone)).all():
            s.delete(sess)
        for msg in s.exec(select(Message).where(Message.phone == phone)).all():
            s.delete(msg)
        s.commit()
        for model in (Message, Reminder, Activity, Event, Task, Document, ProfileFact, Profile):
            column = model.id if model is Profile else model.profile_id
            for row in s.exec(select(model).where(column.in_(pids))).all():
                s.delete(row)
            s.commit()


def _profile(phone: str, name: dict, language: str, pin: str) -> int:
    p = identity.create_profile(phone, language=language, display_name=name["first"])
    identity.set_pin(p.id, pin)
    identity.update_profile(p.id, consent_at=_ago(30))
    return p.id


def _seed_maria(phone: str) -> int:
    pid = _profile(phone, {"first": "Maria"}, "es", DEMO_PINS["maria"])
    f = lambda key, value, days, src="form", ref="seed": memory.set_fact(  # noqa: E731
        pid, key, value, source_type=src, source_ref=ref, confirmed_at=_ago(days))
    f("name", {"first": "Maria", "middle": "Elena", "last": "Garcia"}, 45)
    f("date_of_birth", "1988-03-14", 45)
    f("phone", phone, 45)
    f("preferred_language", "es", 45)
    f("address", {"street": "412 Elm St", "apt": "2B", "city": "Springfield", "state": "IL", "zip": "62704"}, 20)
    f("household_size", 3, 45)
    f("household_members", [
        {"first_name": "Luis", "last_name": "Garcia", "relationship": "son", "date_of_birth": "2015-09-02",
         "is_student": True, "school": "Lincoln Elementary", "has_income": False},
        {"first_name": "Sofia", "last_name": "Garcia", "relationship": "daughter", "date_of_birth": "2019-06-21",
         "is_student": False, "school": "", "has_income": False},
    ], 45)
    # Stale on purpose (30-day policy): the demo re-asks these with the old value as a hint.
    f("employment", {"status": "employed", "employer": "Sunrise Diner", "gross_pay": 300,
                     "pay_frequency": "weekly", "hours_per_week": 28, "varies": True}, 45)
    f("monthly_income", 1300, 45)
    f("housing_cost", 850, 45)
    f("utilities", {"pays_heating_cooling": True, "pays_electric": True, "pays_water": False,
                    "pays_phone": True, "monthly_amount": 160}, 45)
    f("disability_in_household", False, 45)
    f("case_numbers", {"snap": "IL-SNAP-448120"}, 40, src="document", ref="seed-letter")

    # One completed form 45 days ago: the "first form" baseline for the memory metric.
    started, finished = _ago(45), _ago(45) + timedelta(minutes=11)
    with session_scope() as s:
        task = Task(profile_id=pid, kind="fill_form", form_id="sample_benefits", status="completed",
                    turn_count=24, started_channel="sms", started_at=started, completed_at=finished,
                    verification={"ok": True, "fields_checked": 8}, answers={})
        s.add(task)
        s.commit()
        s.refresh(task)
        tid = task.id
        s.add(Event(type="form_started", phone=phone, profile_id=pid, task_id=tid, channel="sms",
                    data={"form_id": "sample_benefits"}, created_at=started))
        s.add(Event(type="form_completed", phone=phone, profile_id=pid, task_id=tid, channel="sms",
                    data={"form_id": "sample_benefits", "duration_s": 660, "turns": 24, "fields_total": 8,
                          "fields_from_memory": 0}, created_at=finished))
        s.add(Event(type="verification", phone=phone, profile_id=pid, task_id=tid,
                    data={"ok": True, "mismatches": 0}, created_at=finished))
        s.add(Activity(profile_id=pid, task_id=tid, kind="form_completed",
                       description="Filled out your Sample Benefits Application with 8 answers",
                       created_at=finished))
        s.commit()
    create_reminder(pid, datetime.now(timezone.utc) + timedelta(days=2),
                    "Formline reminder: your SNAP interview is in 2 days. Reply HELP if you need anything.",
                    task_id=tid)
    return pid


def _seed_shared(phone: str) -> tuple[int, int]:
    james = _profile(phone, {"first": "James"}, "en", DEMO_PINS["james"])
    denise = _profile(phone, {"first": "Denise"}, "en", DEMO_PINS["denise"])
    for pid, name, dob in ((james, {"first": "James", "middle": "", "last": "Walker"}, "1961-11-30"),
                           (denise, {"first": "Denise", "middle": "A", "last": "Walker"}, "1964-02-08")):
        memory.set_fact(pid, "name", name, source_type="conversation", confirmed_at=_ago(10))
        memory.set_fact(pid, "date_of_birth", dob, source_type="conversation", confirmed_at=_ago(10))
        memory.set_fact(pid, "address", {"street": "88 Oak Ave", "apt": "", "city": "Springfield", "state": "IL",
                                         "zip": "62702"}, source_type="conversation", confirmed_at=_ago(10))
    memory.set_fact(james, "disability_in_household", True, source_type="conversation", confirmed_at=_ago(10))
    return james, denise


def seed_demo(phones: Optional[DemoPhones] = None) -> dict:
    """Create (or recreate) the demo personas. Safe to run repeatedly."""
    phones = phones or DemoPhones()
    init_db()
    sync_forms()
    for phone in (phones.returning, phones.new, phones.shared):
        _clear_phone(phone)
    maria = _seed_maria(phones.returning)
    james, denise = _seed_shared(phones.shared)
    return {
        "returning": {"phone": phones.returning, "profile_id": maria, "name": "Maria Garcia",
                      "pin": DEMO_PINS["maria"], "language": "es"},
        "new": {"phone": phones.new},
        "shared": {"phone": phones.shared, "profiles": {"James Walker": james, "Denise Walker": denise},
                   "pins": {"James": DEMO_PINS["james"], "Denise": DEMO_PINS["denise"]}},
    }


def reset_demo(phones: Optional[DemoPhones] = None, *, seed: bool = True) -> Optional[dict]:
    """Wipe all data (tables, downloaded media, filled PDFs) and optionally reseed.

    Drops and recreates tables instead of deleting the SQLite file, so it works while the server runs.
    """
    engine = get_engine()
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    data_dir = get_settings().data_dir.resolve()
    for sub in ("media", "filled"):
        target = (data_dir / sub).resolve()
        if target.is_dir() and target.parent == data_dir:
            shutil.rmtree(target)
    return seed_demo(phones) if seed else None
