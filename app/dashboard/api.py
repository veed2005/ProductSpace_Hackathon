"""JSON API behind the dashboard. Owner: Lane D.

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
from app.models import Activity, Message, Profile, Session, Task

router = APIRouter(prefix="/api")


# Short replies that are just 4 digits are almost always a PIN or SSN last-4: never project them.
_SECRET_REPLY = re.compile(r"^\D{0,3}(\d[\s.-]?){4}\D{0,3}$")


def mask_message(direction: str, text: str) -> str:
    return "•••• (hidden)" if direction == "in" and _SECRET_REPLY.match(text.strip()) else text


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
        duration = (_latest(task.completed_at) - _latest(task.started_at)).total_seconds()
    return {
        "id": task.id, "kind": task.kind, "status": task.status, "form_id": task.form_id,
        "form_name": schema.name if schema else task.form_id, "profile_id": task.profile_id,
        "current_field": task.current_field, "turn_count": task.turn_count,
        "started_at": _iso(task.started_at), "completed_at": _iso(task.completed_at), "duration_s": duration,
        "started_channel": task.started_channel,
        "progress": {"answered": sum(f["answered"] for f in applicable), "total": len(applicable)},
        "counts": counts, "fields": fields,
        "verification": task.verification or None,
        "has_pdf": bool(task.output_pdf_path and Path(task.output_pdf_path).exists()),
    }


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
    return [{"id": m.id, "direction": m.direction, "channel": m.channel, "text": mask_message(m.direction, m.text),
             "media": [Path(p).name for p in m.media], "task_id": m.task_id, "at": _iso(m.created_at)}
            for m in reversed(rows)]
