"""JSON API behind the dashboard.

GET /api/people                    one entry per phone number, most recent first
GET /api/phones/{phone}/messages   interleaved voice + SMS transcript for a phone
GET /api/phones/{phone}/task       the phone's active (or most recent) task, same shape as /api/tasks/{id}
GET /api/tasks/{id}                form fields joined with answers, color-coded by source
GET /api/tasks/{id}/pdf            the filled PDF
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import select

from app.contracts import FormField, FormSchema
from app.db import session_scope
from app.engines import form_library
from app.memory import profile as memory
from app.models import Activity, Document, Message, Profile, ProfileFact, Reminder, Session, Task

router = APIRouter(prefix="/api")


# A bare 4-digit reply right after Formline asked for a PIN / SSN digits is a secret: never project it.
# (Without that context, "1450" is just as likely an income answer, so it stays visible.)
_SECRET_REPLY = re.compile(r"^\D{0,3}(\d[\s.-]?){4}\D{0,3}$")
_ASKED_SECRET = re.compile(r"\b(pin|ssn|social|seguro|digits?|d[ií]gitos|last (4|four)|[uú]ltimos)\b", re.I)


def mask_message(direction: str, text: str, *, after_secret_question: bool) -> str:
    if direction == "in" and after_secret_question and _SECRET_REPLY.match(text.strip()):
        return "•••• (hidden)"
    return text


def mask_phone(phone: str) -> str:
    """Projector-safe: only the last 4 digits."""
    digits = "".join(c for c in phone if c.isdigit())
    return f"•••-{digits[-4:]}" if len(digits) >= 4 else phone


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).isoformat()


def _latest(*dts: Optional[datetime]) -> Optional[datetime]:
    aware = [d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d for d in dts if d]
    return max(aware) if aware else None


# ---------------------------------------------------------------- tasks

def _load_schema(form_id: Optional[str]) -> Optional[FormSchema]:
    if not form_id:
        return None
    try:
        return form_library.load_schema(form_id)
    except (FileNotFoundError, ValueError):
        return None


def _applies(field: FormField, answers: dict) -> bool:
    if field.condition is None:
        return True
    other = answers.get(field.condition.field) or {}
    value, want = other.get("value"), field.condition.equals
    if isinstance(value, str) and isinstance(want, str):
        return value.strip().lower() == want.strip().lower()
    return value == want


def _display(field: FormField, value: Any) -> str:
    if value is None:
        return ""
    if field.sensitive:
        return memory.mask(value)
    if field.type == "yes_no" and isinstance(value, (str, bool)):
        return {"yes": "Yes", "no": "No", "true": "Yes", "false": "No"}.get(str(value).strip().lower(), str(value))
    if field.type == "date" and isinstance(value, str):
        try:
            d = datetime.strptime(value, "%Y-%m-%d")
            return f"{d:%b} {d.day}, {d.year}"
        except ValueError:
            return value
    if field.type == "money" and isinstance(value, (int, float)):
        return f"${value:,.0f}"
    if field.profile_key and memory.validate_profile_key(field.profile_key) is None:
        return memory.format_value(field.profile_key, value)
    return memory.format_value(field.id, value)


def task_view(task: Task) -> dict:
    schema = _load_schema(task.form_id)
    answers = task.answers or {}
    fields, counts = [], {"memory": 0, "asked": 0, "corrected": 0, "document": 0, "unknown": 0, "skipped": 0}
    for f in schema.fields if schema else []:
        ans = answers.get(f.id) or {}
        source = ans.get("source")
        if source in counts:
            counts[source] += 1
        fields.append({
            "id": f.id, "label": f.label, "group": f.group, "required": f.required, "sensitive": f.sensitive,
            "applies": _applies(f, answers), "answered": bool(ans), "source": source,
            "value": _display(f, ans.get("value")), "updated_at": ans.get("updated_at"),
            "current": f.id == task.current_field,
        })
    applicable = [f for f in fields if f["applies"]]
    duration = None
    if task.completed_at:
        duration = round((_latest(task.completed_at) - _latest(task.started_at)).total_seconds())
    comparison = None
    if task.completed_at and duration is not None:
        with session_scope() as s:
            prior = s.exec(select(Task).where(Task.profile_id == task.profile_id, Task.id != task.id,
                                              Task.status == "completed", Task.completed_at.is_not(None),
                                              Task.completed_at < task.completed_at)
                           .order_by(Task.completed_at)).first()
        if prior:
            first_s = round((_latest(prior.completed_at) - _latest(prior.started_at)).total_seconds())
            if first_s > 0:
                comparison = {"first_form_id": prior.form_id, "first_s": first_s, "this_s": duration,
                              "faster_pct": round(100 * (1 - duration / first_s))}
    return {
        "comparison": comparison,
        "id": task.id, "kind": task.kind, "status": task.status, "form_id": task.form_id,
        "form_name": schema.name if schema else task.form_id, "profile_id": task.profile_id,
        "current_field": task.current_field, "turn_count": task.turn_count,
        "started_at": _iso(task.started_at), "completed_at": _iso(task.completed_at), "duration_s": duration,
        "started_channel": task.started_channel,
        "progress": {"answered": sum(f["answered"] for f in applicable), "total": len(applicable)},
        "counts": counts, "fields": fields,
        "verification": task.verification or None,
        "has_pdf": bool(task.output_pdf_path and Path(task.output_pdf_path).exists()),
        "formcall": _formcall_view(task),  # phone form session (app/formcall); None for other tasks
    }


def _formcall_view(task: Task) -> Optional[dict]:
    from app.formcall.dashboard import view

    return view(task)


@router.get("/tasks/{task_id}")
def get_task(task_id: int) -> dict:
    with session_scope() as s:
        task = s.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task_view(task)


@router.get("/tasks/{task_id}/pdf")
def get_task_pdf(task_id: int) -> FileResponse:
    with session_scope() as s:
        task = s.get(Task, task_id)
    if task is None or not task.output_pdf_path or not Path(task.output_pdf_path).exists():
        raise HTTPException(404, "no filled PDF for this task")
    return FileResponse(task.output_pdf_path, media_type="application/pdf",
                        filename=f"{task.form_id or 'form'}_task{task.id}.pdf")


def _phone_task(s, phone: str) -> Optional[Task]:
    sess = s.exec(select(Session).where(Session.phone == phone)).first()
    if sess and sess.active_task_id:
        task = s.get(Task, sess.active_task_id)
        if task:
            return task
    pids = [p.id for p in s.exec(select(Profile).where(Profile.phone == phone)).all()]
    if not pids:
        return None
    return s.exec(select(Task).where(Task.profile_id.in_(pids)).order_by(Task.started_at.desc())).first()


@router.get("/phones/{phone}/task")
def get_phone_task(phone: str) -> Optional[dict]:
    with session_scope() as s:
        task = _phone_task(s, phone)
    return task_view(task) if task else None


# ---------------------------------------------------------------- profiles (memory view)

SOURCE_LABELS = {"form": "From a form", "document": "From a letter", "conversation": "Said in conversation",
                 "seed": "Demo data"}
_FREQ = {"weekly": "a week", "biweekly": "every 2 weeks", "semimonthly": "twice a month", "monthly": "a month"}


def _money(v: Any) -> str:
    return f"${v:,.0f}" if isinstance(v, (int, float)) else str(v)


def fact_display(key: str, value: Any) -> list[str]:
    """One or more readable lines for a stored fact."""
    if value is None or value == "" or value == [] or value == {}:
        return ["—"]
    if key == "household_members" and isinstance(value, list):
        lines = []
        for m in value:
            if not isinstance(m, dict):
                continue
            who = " ".join(p for p in (m.get("first_name"), m.get("last_name")) if p) or "Unnamed"
            extra = [m.get("relationship")]
            if m.get("date_of_birth"):
                try:
                    born = datetime.strptime(m["date_of_birth"], "%Y-%m-%d")
                    today = datetime.now()
                    extra.append(f"age {today.year - born.year - ((today.month, today.day) < (born.month, born.day))}")
                except ValueError:
                    pass
            if m.get("is_student"):
                extra.append(f"student{' at ' + m['school'] if m.get('school') else ''}")
            details = ", ".join(e for e in extra if e)
            lines.append(f"{who} ({details})" if details else who)
        return lines or ["—"]
    if key == "employment" and isinstance(value, dict):
        if value.get("status") and value.get("status") != "employed":
            return [str(value["status"]).replace("_", " ").capitalize()]
        pay = value.get("gross_pay")
        line = " · ".join(p for p in (
            value.get("employer"),
            f"{_money(pay)} {_FREQ.get(value.get('pay_frequency'), '')}".strip() if pay is not None else None,
            f"{value['hours_per_week']} h/week" if value.get("hours_per_week") else None,
            "pay varies" if value.get("varies") else None,
        ) if p)
        return [line or "Employed"]
    if key == "other_income" and isinstance(value, list):
        return [f"{i.get('type', 'Income')}: {_money(i.get('monthly_amount'))}/month" for i in value if isinstance(i, dict)] or ["—"]
    if key == "utilities" and isinstance(value, dict):
        pays = [n for n, k in (("heating/cooling", "pays_heating_cooling"), ("electric", "pays_electric"),
                               ("water", "pays_water"), ("phone", "pays_phone")) if value.get(k)]
        line = "Pays " + ", ".join(pays) if pays else "No utilities"
        if value.get("monthly_amount") is not None:
            line += f" · {_money(value['monthly_amount'])}/month"
        return [line]
    if key == "case_numbers" and isinstance(value, dict):
        names = {"snap": "SNAP", "medicaid": "Medicaid", "tanf": "TANF", "school_meals": "School meals"}
        return [f"{names.get(k, k.title())}: {v}" for k, v in value.items() if v] or ["—"]
    if key in ("monthly_income", "housing_cost"):
        return [f"{_money(value)}/month"]
    if key == "date_of_birth" and isinstance(value, str):
        try:
            d = datetime.strptime(value, "%Y-%m-%d")
            return [f"{d:%b} {d.day}, {d.year}"]
        except ValueError:
            return [value]
    if key == "phone":
        return [mask_phone(str(value))]
    if key == "preferred_language":
        return [{"en": "English", "es": "Spanish"}.get(value, str(value))]
    return [memory.format_value(key, value)]


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: int) -> dict:
    with session_scope() as s:
        p = s.get(Profile, profile_id)
        if p is None:
            raise HTTPException(404, "profile not found")
        rows = {f.key: f for f in s.exec(select(ProfileFact).where(ProfileFact.profile_id == profile_id)).all()}
        acts = s.exec(select(Activity).where(Activity.profile_id == profile_id)
                      .order_by(Activity.created_at.desc()).limit(8)).all()
        n_docs = len(s.exec(select(Document.id).where(Document.profile_id == profile_id)).all())
        tasks = s.exec(select(Task).where(Task.profile_id == profile_id).order_by(Task.started_at.desc())).all()
        reminders = s.exec(select(Reminder).where(Reminder.profile_id == profile_id, Reminder.status == "pending")
                           .order_by(Reminder.due_at)).all()
    views = memory.get_facts(profile_id)
    now = datetime.now(timezone.utc)
    order = list(memory.CANONICAL_KEYS)
    facts = []
    for key in sorted(views, key=lambda k: order.index(k) if k in order else len(order)):
        v, row = views[key], rows[key]
        stale_in = None
        if row.freshness_days is not None:
            stale_in = row.freshness_days - (now - v.confirmed_at).days
        facts.append({
            "key": key, "label": memory.FACT_LABELS.get(key, key.replace("_", " ").capitalize()),
            "lines": [memory.mask(v.value)] if v.sensitive else fact_display(key, v.value),
            "sensitive": v.sensitive, "source": v.source_type,
            "source_label": SOURCE_LABELS.get(v.source_type, v.source_type),
            "confirmed_at": _iso(v.confirmed_at), "fresh": v.fresh, "stale_in_days": stale_in,
        })
    return {
        "id": p.id, "name": p.display_name or "Unnamed", "phone_masked": mask_phone(p.phone),
        "language": p.preferred_language, "pin_set": bool(p.pin_hash), "consent_at": _iso(p.consent_at),
        "created_at": _iso(p.created_at),
        "facts": facts,
        "counts": {"facts": len(facts), "stale": sum(not f["fresh"] for f in facts),
                   "forms_completed": sum(t.status == "completed" for t in tasks), "documents": n_docs},
        "tasks": [{"id": t.id, "form_id": t.form_id, "status": t.status, "started_at": _iso(t.started_at)}
                  for t in tasks[:5]],
        "reminders": [{"id": r.id, "due_at": _iso(r.due_at), "message": r.message} for r in reminders],
        "activity": [{"description": a.description, "at": _iso(a.created_at)} for a in acts],
    }


# ---------------------------------------------------------------- people + transcript

@router.get("/people")
def people() -> list[dict]:
    with session_scope() as s:
        profiles = s.exec(select(Profile)).all()
        sessions = {x.phone: x for x in s.exec(select(Session)).all()}
        phones = set(sessions) | {p.phone for p in profiles}
        out = []
        for phone in phones:
            sess = sessions.get(phone)
            mine = [p for p in profiles if p.phone == phone]
            last_msg = s.exec(select(Message).where(Message.phone == phone)
                              .order_by(Message.created_at.desc())).first()
            task = _phone_task(s, phone)
            active = sess.profile_id if sess else None
            names = [p.display_name or "Unnamed" for p in mine]
            if active:
                speaking = next((p for p in mine if p.id == active), None)
                name = speaking.display_name if speaking and speaking.display_name else " / ".join(names)
            else:
                name = " / ".join(names) if names else "New caller"
            if task and task.status in ("active", "readback"):
                tv = task_view(task)
                doing = f"Filling {tv['form_name']} ({tv['progress']['answered']}/{tv['progress']['total']})"
            else:
                act = None
                if mine:
                    act = s.exec(select(Activity).where(Activity.profile_id.in_([p.id for p in mine]))
                                 .order_by(Activity.created_at.desc())).first()
                doing = act.description if act else ("Getting started" if sess else "No activity yet")
            language = next((p.preferred_language for p in mine if p.id == active), None) or \
                (mine[0].preferred_language if mine else None)
            out.append({
                "phone": phone, "phone_masked": mask_phone(phone), "name": name,
                "profile_count": len(mine), "language": language,
                "profiles": [{"id": p.id, "name": p.display_name or "Unnamed"} for p in mine],
                "active_profile_id": active,
                "channel": sess.last_channel if sess else (last_msg.channel if last_msg else None),
                "doing": doing, "task_id": task.id if task else None,
                "task_status": task.status if task else None,
                "last_seen": _iso(_latest(sess.updated_at if sess else None,
                                          last_msg.created_at if last_msg else None,
                                          *(p.updated_at for p in mine))),
                "live": bool(last_msg and (datetime.now(timezone.utc) - _latest(last_msg.created_at)).total_seconds() < 300),
            })
    return sorted(out, key=lambda x: x["last_seen"] or "", reverse=True)


@router.get("/phones/{phone}/messages")
def messages(phone: str, limit: int = 200) -> list[dict]:
    with session_scope() as s:
        rows = s.exec(select(Message).where(Message.phone == phone)
                      .order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)).all()
    out, asked_secret = [], False
    for m in reversed(rows):
        out.append({"id": m.id, "direction": m.direction, "channel": m.channel,
                    "text": mask_message(m.direction, m.text, after_secret_question=asked_secret),
                    "media": [Path(p).name for p in m.media], "task_id": m.task_id, "at": _iso(m.created_at)})
        if m.direction == "out":
            asked_secret = bool(_ASKED_SECRET.search(m.text))
    return out
