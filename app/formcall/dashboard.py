"""What the partner dashboard shows about a phone form session: state, language, what Formline is waiting for,
each answer's provenance and verification, notices and how the caller responded, and the receipt.

Everything here is masked by the same policy as receipts (privacy.py); PINs and secrets never appear.
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import select

from app.db import session_scope
from app.engines import form_library
from app.formcall import notices as notices_mod
from app.formcall import privacy, validate, values
from app.models import FormNotice, FormRun, Receipt, Task

WAITING = {
    None: "an answer", "pin": "the PIN (to use saved info)", "memory_batch": "OK to use saved info",
    "stale": "a re-check of an old answer", "readback": "a yes to a read-back", "spell": "a spelling",
    "double": "the digits again", "notice": "a reply about a notice", "review": "a reply to the summary",
    "final": "the final yes", "resume": "OK to continue after a question", "start_over": "OK to start over",
    "receipt_offer": "whether to email a receipt", "receipt_known": "OK to use the saved email",
    "receipt_email": "an email address", "receipt_confirm": "a yes to the spelled-back email", "paused": "continue",
}


def view(task: Task) -> Optional[dict]:
    with session_scope() as s:
        run = s.get(FormRun, task.id)
        if run is None:
            return None
        notice_rows = list(s.exec(select(FormNotice).where(FormNotice.task_id == task.id)).all())
        rec = s.exec(select(Receipt).where(Receipt.task_id == task.id).order_by(Receipt.id.desc())).first()
    try:
        schema = form_library.load_schema(task.form_id)
    except (OSError, ValueError):
        return None
    fields = {f.id: f for f in schema.fields}
    answers = task.answers or {}
    audit = {}
    for fid, entry in answers.items():
        f = fields.get(fid)
        if f is None or privacy.is_secret(f):
            continue
        audit[fid] = {
            "provenance": entry.get("provenance") or {"memory": "memory", "corrected": "corrected"}.get(
                entry.get("source"), "caller"),
            "verification": entry.get("verification") or "inferred",
            "heard": entry.get("heard"), "normalized": entry.get("normalized"), "form_value": entry.get("form_value"),
            "translated": bool(entry.get("translated")), "language": entry.get("language"),
            "history": entry.get("history", []),
        }
    problems = validate.problems(schema, answers, notice_rows)
    unresolved = []
    for p in problems:
        if p.kind == "notice":
            unresolved.append("notice not yet presented")
        elif p.field_id in fields:
            unresolved.append(f"{fields[p.field_id].label}: {p.kind.replace('_', ' ')}")
        else:
            unresolved.append(p.detail or p.kind)
    pending = run.pending or {}
    current = fields.get(task.current_field or "")
    return {
        "state": run.state, "language": run.language,
        "waiting_for": WAITING.get(pending.get("type"), pending.get("type")),
        "current_question": current.label if current and run.state in ("in_progress", "paused") else None,
        "unresolved": unresolved,
        "approved": bool((run.review or {}).get("approved_at")),
        "final_action": (run.review or {}).get("final_action"),
        "submission": run.submission or None,
        "fields": audit,
        "notices": notices_mod.as_dicts(sorted(notice_rows, key=lambda r: r.id)),
        "receipt": None if rec is None else {
            "status": rec.status, "to": values.mask_email(rec.to_address) if rec.to_address else None,
            "provider": rec.provider, "error": rec.error,
            "sent_at": rec.sent_at.isoformat() if rec.sent_at else None},
    }
