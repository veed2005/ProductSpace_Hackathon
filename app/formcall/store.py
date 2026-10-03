"""Reading and writing a phone form's state: the Task (answers, status), its FormRun (workflow), and the audit
trail kept on each answer.

Each answer in Task.answers keeps the keys the rest of the app already reads (value, source, updated_at) plus:
    provenance    memory | caller | corrected | derived
    heard         what the recognizer heard (masked for sensitive fields)
    normalized    what Formline understood, e.g. "300 USD per week"
    form_value    what goes on the form (masked for sensitive fields)
    translated    the form value is a translation of what was said
    verification  unverified | read_back | inferred | confirmed
    language      the language it was given in
    history       earlier values (masked) and why they changed
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.contracts import FormField
from app.db import session_scope
from app.events import log_event, publish
from app.formcall import privacy
from app.models import FormRun, Task

_SOURCE = {"memory": "memory", "caller": "asked", "corrected": "corrected", "derived": "asked"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load(task_id: int) -> tuple[Optional[Task], Optional[FormRun]]:
    with session_scope() as s:
        return s.get(Task, task_id), s.get(FormRun, task_id)


def save(task: Task, run: FormRun) -> None:
    run.updated_at = datetime.now(timezone.utc)
    with session_scope() as s:
        s.merge(task)
        s.merge(run)
        s.commit()
    publish("task_updated", task_id=task.id, profile_id=task.profile_id, form_id=task.form_id, status=task.status)


def entry(field: FormField, *, value: Any, provenance: str, heard: Optional[str], normalized: Optional[str],
          form_value: Optional[str], translated: bool, verification: str, language: str,
          transformed: bool = False) -> dict:
    hide = field.sensitive or field.type == "ssn_last4" or privacy.is_secret(field)
    return {
        "value": value, "source": _SOURCE.get(provenance, "asked"), "updated_at": now_iso(),
        "provenance": provenance,
        "heard": (privacy.mask(field, heard or "") if hide and heard else heard),
        "normalized": None if hide else normalized,
        "form_value": (privacy.mask(field, form_value or "") if hide and form_value else form_value),
        "translated": translated, "transformed": transformed or translated, "verification": verification,
        "language": language,
    }


def put_answer(task: Task, field: FormField, new: dict, *, channel: str, reason: Optional[str] = None) -> None:
    """Store an answer, keeping the previous value (masked) in its history. Publishes the dashboard event."""
    answers = dict(task.answers or {})
    old = answers.get(field.id)
    history = list((old or {}).get("history", []))
    if old and old.get("value") is not None and old.get("value") != new.get("value"):
        history.append({"at": now_iso(), "value": privacy.display(field, old.get("value")),
                        "provenance": old.get("provenance"), "reason": reason or "changed"})
    new = {**new, "history": history}
    answers[field.id] = new
    task.answers = answers
    hide = field.sensitive or field.type == "ssn_last4"
    publish("field_filled", task_id=task.id, field_id=field.id, label=field.label,
            value="[redacted]" if hide else privacy.display(field, new.get("value")), source=new.get("source"))
    log_event("field_answered", task_id=task.id, profile_id=task.profile_id, channel=channel, field_id=field.id,
              source=new.get("source"), provenance=new.get("provenance"), verification=new.get("verification"))
    if reason == "corrected":
        log_event("readback_correction", task_id=task.id, profile_id=task.profile_id, field_id=field.id)


def set_verification(task: Task, field_id: str, status: str) -> None:
    answers = dict(task.answers or {})
    if field_id in answers:
        answers[field_id] = {**answers[field_id], "verification": status}
        task.answers = answers
