"""Canonical profile facts with provenance and freshness. Owner: Lane D.

Used by the form engine (prefill and write-back) and the document engine (case numbers, dates).

A form field's `profile_key` is a *path* into the canonical profile:

    "date_of_birth"                     a whole fact
    "address.city"                      a field inside an object fact
    "household_members[1].first_name"   a field inside one item of a list fact
    "full_name"                         virtual: reads/writes the parts of "name"

Use `get_value(profile_id, path)` / `set_value(profile_id, path, value, ...)` with paths. Freshness
is tracked per top-level fact: updating `address.city` re-confirms the whole address.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlmodel import select

from app.db import session_scope
from app.events import log_activity, publish
from app.models import Profile, ProfileFact, Session

# ---------------------------------------------------------------- canonical keys

NAME_FIELDS = ("first", "middle", "last")
ADDRESS_FIELDS = ("street", "apt", "city", "state", "zip")


@dataclass(frozen=True)
class KeySpec:
    description: str
    freshness_days: Optional[int]  # None = never goes stale
    fields: Optional[tuple[str, ...]] = None  # object (or list item) fields; None = scalar
    is_list: bool = False
    sensitive: bool = False
    value_hint: str = ""  # how values are stored, for normalization and schema mapping


CANONICAL_KEYS: dict[str, KeySpec] = {
    "name": KeySpec("Legal name of the person", None, NAME_FIELDS,
                    value_hint='{"first": "Ana", "middle": "Maria", "last": "Lopez"}; use "full_name" for one string'),
    "date_of_birth": KeySpec("Date of birth", None, value_hint="ISO date YYYY-MM-DD"),
    "phone": KeySpec("Contact phone number", 180, value_hint="E.164, e.g. +12175550123"),
    "email": KeySpec("Email address, if any", 180),
    "preferred_language": KeySpec("Language for conversations", None, value_hint='ISO code: "en", "es"'),
    "address": KeySpec("Home address", 180, ADDRESS_FIELDS,
                       value_hint='{"street": "412 Elm St", "apt": "2B", "city": "...", "state": "IL", "zip": "62701"}'),
    "mailing_address": KeySpec("Mailing address, if different from home", 180, ADDRESS_FIELDS),
    "household_size": KeySpec("Number of people in the household, including the person", 90,
                              value_hint="integer"),
    "household_members": KeySpec(
        "Other people in the household (not the person themself)", 90,
        ("first_name", "last_name", "relationship", "date_of_birth", "is_student", "school", "has_income"),
        is_list=True,
        value_hint='[{"first_name": "Leo", "last_name": "Lopez", "relationship": "son", '
                   '"date_of_birth": "2016-04-02", "is_student": true, "school": "Lincoln Elementary", '
                   '"has_income": false}]',
    ),
    "employment": KeySpec(
        "The person's job", 30,
        ("status", "employer", "gross_pay", "pay_frequency", "hours_per_week", "varies"),
        value_hint='status: employed|unemployed|self_employed|retired|unable_to_work; gross_pay: dollars per '
                   'pay period; pay_frequency: weekly|biweekly|semimonthly|monthly; varies: true if pay changes',
    ),
    "monthly_income": KeySpec("Total household gross income per month, all sources", 30,
                              value_hint="number, US dollars per month (weekly x 4.33)"),
    "other_income": KeySpec("Income other than wages (SSI, child support, unemployment, ...)", 30,
                            ("type", "monthly_amount"), is_list=True),
    "housing_cost": KeySpec("Rent or mortgage per month", 90, value_hint="number, US dollars per month"),
    "utilities": KeySpec("Utility costs", 90, ("pays_heating_cooling", "pays_electric", "pays_water",
                                               "pays_phone", "monthly_amount")),
    "disability_in_household": KeySpec("Anyone in the household has a disability", 180, value_hint="boolean"),
    "case_numbers": KeySpec("Existing case or account numbers by program", None,
                            ("snap", "medicaid", "tanf", "school_meals", "other")),
    "ssn_last4": KeySpec("Last 4 digits of SSN (never the full number)", None, sensitive=True,
                         value_hint="4-digit string"),
}

# Short labels for read-backs and the dashboard.
FACT_LABELS: dict[str, str] = {
    "name": "Name", "date_of_birth": "Date of birth", "phone": "Phone", "email": "Email",
    "preferred_language": "Language", "address": "Home address", "mailing_address": "Mailing address",
    "household_size": "Household size", "household_members": "Household members", "employment": "Job",
    "monthly_income": "Monthly income", "other_income": "Other income", "housing_cost": "Rent / mortgage",
    "utilities": "Utilities", "disability_in_household": "Disability in household",
    "case_numbers": "Case numbers", "ssn_last4": "SSN (last 4)",
}

# Read/write aliases that map onto real keys.
VIRTUAL_KEYS = {"full_name": "name"}

# Days until each top-level fact should be re-confirmed (kept for existing callers).
FRESHNESS_POLICY: dict[str, Optional[int]] = {k: s.freshness_days for k, s in CANONICAL_KEYS.items()}
DEFAULT_FRESHNESS_DAYS = 90

# ---------------------------------------------------------------- paths

_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")
_BASE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)")


def parse_path(path: str) -> tuple[str, list[str | int]]:
    """'household_members[1].first_name' -> ('household_members', [1, 'first_name'])."""
    m = _BASE.match(path)
    if not m:
        raise ValueError(f"invalid profile path: {path!r}")
    base, rest, steps = m.group(1), path[m.end():], []
    pos = 0
    while pos < len(rest):
        t = _TOKEN.match(rest, pos)
        if not t:
            raise ValueError(f"invalid profile path: {path!r}")
        steps.append(t.group(1) if t.group(1) is not None else int(t.group(2)))
        pos = t.end()
    return base, steps


def validate_profile_key(path: str) -> Optional[str]:
    """None if `path` is a valid canonical path, else a human-readable problem."""
    try:
        base, steps = parse_path(path)
    except ValueError as e:
        return str(e)
    if base in VIRTUAL_KEYS:
        return None if not steps else f"{base} takes no sub-fields"
    spec = CANONICAL_KEYS.get(base)
    if spec is None:
        return f"unknown profile key {base!r} (add it to CANONICAL_KEYS in app/memory/profile.py)"
    if spec.is_list:
        if not steps:
            return None
        if not isinstance(steps[0], int):
            return f"{base} is a list; use {base}[0].<field>"
        steps = steps[1:]
    if not steps:
        return None
    if spec.fields is None:
        return f"{base} has no sub-fields"
    if len(steps) != 1 or steps[0] not in spec.fields:
        return f"{path!r}: field must be one of {', '.join(spec.fields)}"
    return None


# ---------------------------------------------------------------- facts

@dataclass
class FactView:
    key: str
    value: Any
    source_type: str
    source_ref: Optional[str]
    confirmed_at: datetime
    fresh: bool
    sensitive: bool


@dataclass
class ValueView:
    """A value at a profile path, with its top-level fact's provenance."""
    path: str
    value: Any
    fresh: bool
    source_type: str
    confirmed_at: datetime
    sensitive: bool
    fact: FactView = field(repr=False)


def _policy_for(key: str) -> Optional[int]:
    spec = CANONICAL_KEYS.get(key)
    return spec.freshness_days if spec else DEFAULT_FRESHNESS_DAYS


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt  # SQLite drops tzinfo


def _is_fresh(fact: ProfileFact, now: datetime) -> bool:
    if fact.freshness_days is None:
        return True
    return now - _aware(fact.confirmed_at) < timedelta(days=fact.freshness_days)


def get_facts(profile_id: int) -> dict[str, FactView]:
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        rows = s.exec(select(ProfileFact).where(ProfileFact.profile_id == profile_id)).all()
    return {
        f.key: FactView(f.key, f.value, f.source_type, f.source_ref, _aware(f.confirmed_at),
                        _is_fresh(f, now), f.sensitive)
        for f in rows
    }


def get_fact(profile_id: int, key: str) -> Optional[FactView]:
    return get_facts(profile_id).get(key)


def set_fact(profile_id: int, key: str, value: Any, *, source_type: str, source_ref: Optional[str] = None,
             sensitive: Optional[bool] = None, confirmed_at: Optional[datetime] = None) -> None:
    """Insert or overwrite a whole top-level fact and mark it confirmed (now, unless given)."""
    now = datetime.now(timezone.utc)
    spec = CANONICAL_KEYS.get(key)
    if sensitive is None:
        sensitive = spec.sensitive if spec else False
    with session_scope() as s:
        fact = s.exec(select(ProfileFact).where(ProfileFact.profile_id == profile_id,
                                                ProfileFact.key == key)).first()
        if fact is None:
            fact = ProfileFact(profile_id=profile_id, key=key, source_type=source_type,
                               freshness_days=_policy_for(key))
        fact.value = value
        fact.source_type = source_type
        fact.source_ref = source_ref
        fact.sensitive = sensitive
        fact.confirmed_at = confirmed_at or now
        fact.updated_at = now
        s.add(fact)
        s.commit()
    publish("profile_updated", profile_id=profile_id, key=key)


def confirm_fact(profile_id: int, key: str) -> None:
    """Person said the stored value is still right. Accepts a path; confirms its top-level fact."""
    base, _ = parse_path(key)
    base = VIRTUAL_KEYS.get(base, base)
    with session_scope() as s:
        fact = s.exec(select(ProfileFact).where(ProfileFact.profile_id == profile_id,
                                                ProfileFact.key == base)).first()
        if fact:
            fact.confirmed_at = datetime.now(timezone.utc)
            s.add(fact)
            s.commit()


# ---------------------------------------------------------------- path-level read/write

def _split_name(full: str) -> dict:
    parts = full.split()
    if not parts:
        return {"first": "", "middle": "", "last": ""}
    if len(parts) == 1:
        return {"first": parts[0], "middle": "", "last": ""}
    return {"first": parts[0], "middle": " ".join(parts[1:-1]), "last": parts[-1]}


def _join_name(name: Any) -> Optional[str]:
    if isinstance(name, str):
        return name
    if isinstance(name, dict):
        return " ".join(p for p in (name.get(k) for k in NAME_FIELDS) if p) or None
    return None


def _dig(value: Any, steps: list[str | int]) -> Any:
    for step in steps:
        if isinstance(step, int):
            if not isinstance(value, list) or step >= len(value):
                return None
            value = value[step]
        else:
            if not isinstance(value, dict):
                return None
            value = value.get(step)
        if value is None:
            return None
    return value


def get_value(profile_id: int, path: str, facts: Optional[dict[str, FactView]] = None) -> Optional[ValueView]:
    """Value at a canonical path, or None if not stored. Pass `facts` to avoid re-querying."""
    base, steps = parse_path(path)
    facts = facts if facts is not None else get_facts(profile_id)
    real = VIRTUAL_KEYS.get(base, base)
    fact = facts.get(real)
    if fact is None:
        return None
    value = _join_name(fact.value) if base == "full_name" else _dig(fact.value, steps)
    if value is None or value == "":
        return None
    return ValueView(path, value, fact.fresh, fact.source_type, fact.confirmed_at, fact.sensitive, fact)


def set_value(profile_id: int, path: str, value: Any, *, source_type: str,
              source_ref: Optional[str] = None) -> None:
    """Write a value at a canonical path, creating containers as needed. Raises ValueError on bad paths."""
    problem = validate_profile_key(path)
    if problem:
        raise ValueError(problem)
    base, steps = parse_path(path)
    if base == "full_name":
        base, steps, value = "name", [], _split_name(str(value))
    if not steps:
        set_fact(profile_id, base, value, source_type=source_type, source_ref=source_ref)
        return

    existing = get_fact(profile_id, base)
    spec = CANONICAL_KEYS[base]
    root: Any = existing.value if existing else ([] if spec.is_list else {})
    if spec.is_list and not isinstance(root, list):
        root = []
    if not spec.is_list and not isinstance(root, dict):
        root = {}

    container = root
    for i, step in enumerate(steps):
        last = i == len(steps) - 1
        if isinstance(step, int):
            while len(container) <= step:
                container.append({})
            if last:
                container[step] = value
            else:
                if not isinstance(container[step], dict):
                    container[step] = {}
                container = container[step]
        else:
            if last:
                container[step] = value
            else:
                container = container.setdefault(step, {})
    set_fact(profile_id, base, root, source_type=source_type, source_ref=source_ref)


# ---------------------------------------------------------------- formatting

def format_value(path: str, value: Any) -> str:
    """Human-readable form for read-backs, receipts, and PDFs."""
    base, steps = parse_path(path)
    if value is None:
        return ""
    if base in ("name", "full_name") and not steps:
        return _join_name(value) or ""
    if base in ("address", "mailing_address") and not steps and isinstance(value, dict):
        apt = value.get("apt") and f"Apt {value['apt']}"
        tail = " ".join(p for p in (value.get("state"), value.get("zip")) if p)
        return ", ".join(p for p in (value.get("street"), apt, value.get("city"), tail) if p)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def mask(value: Any) -> str:
    """For sensitive values in texts and on the dashboard: '••••' plus at most the last 2 chars."""
    s = str(value)
    return "••••" + (s[-2:] if len(s) > 4 else "")


# ---------------------------------------------------------------- forget

@dataclass
class ForgetResult:
    facts: int = 0
    tasks: int = 0
    documents: int = 0
    messages: int = 0
    reminders: int = 0
    files: int = 0

    def summary(self) -> str:
        parts = [(self.tasks, "form"), (self.documents, "document"), (self.facts, "saved detail")]
        said = [f"{n} {word}{'' if n == 1 else 's'}" for n, word in parts if n]
        return "Deleted " + (", ".join(said) if said else "your profile") + "."


def _remove_file(path: Optional[str]) -> bool:
    """Delete a file only if it lives under the data directory."""
    if not path:
        return False
    from app.config import get_settings

    data_dir = get_settings().data_dir.resolve()
    target = Path(path).resolve()
    if data_dir in target.parents and target.is_file():
        target.unlink()
        return True
    return False


def forget_profile(profile_id: int) -> ForgetResult:
    """'Forget me': permanently delete everything about this person. Caller confirms first.

    Deletes facts, tasks (and filled PDFs), documents (and photos), reminders, activity, and their
    transcript. Metrics events are kept for aggregate numbers but stripped of identity. Other
    people sharing the phone are untouched; transcript lines not yet tied to anyone on a shared
    phone are kept unless this was the phone's last profile.
    """
    from app.models import Activity, Document, Event, Message, Reminder, Task

    result = ForgetResult()
    with session_scope() as s:
        profile = s.get(Profile, profile_id)
        if profile is None:
            return result
        phone = profile.phone
        others = s.exec(select(Profile).where(Profile.phone == phone, Profile.id != profile_id)).all()
        task_ids = [t.id for t in s.exec(select(Task).where(Task.profile_id == profile_id)).all()]

        # 1. Detach the session (it may point at this person and their active task).
        for sess in s.exec(select(Session).where(Session.phone == phone)).all():
            if sess.profile_id == profile_id or sess.active_task_id in task_ids:
                if others:
                    sess.profile_id, sess.active_task_id = None, None
                    sess.state, sess.pending, sess.pin_verified_at = "new", {}, None
                    s.add(sess)
                else:
                    s.delete(sess)
        s.commit()

        # 2. Rows that reference tasks, documents, or the profile.
        mine = (Message.profile_id == profile_id) | (Message.task_id.in_(task_ids))
        if not others:  # last profile on this phone: also its not-yet-identified lines
            mine = mine | ((Message.phone == phone) & (Message.profile_id.is_(None)))
        msgs = s.exec(select(Message).where(mine)).all()
        result.messages = len(msgs)
        for m in msgs:
            s.delete(m)
        for r in s.exec(select(Reminder).where(Reminder.profile_id == profile_id)).all():
            result.reminders += 1
            s.delete(r)
        for a in s.exec(select(Activity).where(
                (Activity.profile_id == profile_id) | (Activity.task_id.in_(task_ids)))).all():
            s.delete(a)
        for e in s.exec(select(Event).where(
                (Event.profile_id == profile_id) | (Event.task_id.in_(task_ids)))).all():
            e.profile_id, e.task_id, e.phone = None, None, None
            s.add(e)
        # Paired browsers and browser-agent history.
        from app.models import BrowserAction, BrowserInstallation, BrowserTask

        browser_tasks = [t.id for t in s.exec(select(BrowserTask).where(BrowserTask.profile_id == profile_id)).all()]
        for row in s.exec(select(BrowserAction).where(BrowserAction.task_id.in_(browser_tasks))).all():
            s.delete(row)
        for model in (BrowserTask, BrowserInstallation):
            for row in s.exec(select(model).where(model.profile_id == profile_id)).all():
                s.delete(row)
        # Phone form runs, the notices shown during them, and emailed receipts (with their PDFs).
        from app.models import FormNotice, FormRun, Receipt

        for row in s.exec(select(FormNotice).where(FormNotice.task_id.in_(task_ids))).all():
            s.delete(row)
        for row in s.exec(select(FormRun).where(FormRun.profile_id == profile_id)).all():
            s.delete(row)
        for row in s.exec(select(Receipt).where(Receipt.profile_id == profile_id)).all():
            result.files += _remove_file(row.pdf_path)
            s.delete(row)
        s.commit()

        # 3. Tasks, then documents (tasks reference documents), with their files.
        for t in s.exec(select(Task).where(Task.profile_id == profile_id)).all():
            result.tasks += 1
            result.files += _remove_file(t.output_pdf_path)
            s.delete(t)
        s.commit()
        for d in s.exec(select(Document).where(Document.profile_id == profile_id)).all():
            result.documents += 1
            result.files += sum(_remove_file(p) for p in d.media_paths)
            s.delete(d)
        s.commit()

        # 4. Facts and the profile itself.
        facts = s.exec(select(ProfileFact).where(ProfileFact.profile_id == profile_id)).all()
        result.facts = len(facts)
        for f in facts:
            s.delete(f)
        s.commit()  # separately: no ORM relationships, so one flush could delete the profile first
        s.delete(profile)
        s.commit()

    log_activity("profile_deleted", "Deleted a profile and all its data at the person's request")
    publish("profile_deleted", profile_id=profile_id)
    return result


def describe_keys() -> str:
    """Canonical keys as plain text, e.g. for Lane C's schema-generation prompt."""
    lines = []
    for key, spec in CANONICAL_KEYS.items():
        shape = (f"list of {{{', '.join(spec.fields)}}}" if spec.is_list and spec.fields
                 else f"{{{', '.join(spec.fields)}}}" if spec.fields else "scalar")
        hint = f"; {spec.value_hint}" if spec.value_hint else ""
        lines.append(f"- {key}: {spec.description}. Shape: {shape}{hint}")
    lines.append('- full_name: the person\'s whole name as one string (maps onto "name")')
    lines.append('Paths: "address.city", "household_members[0].first_name", "case_numbers.snap".')
    return "\n".join(lines)
