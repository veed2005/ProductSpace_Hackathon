"""Browser tasks and their action log: saved to the database and broadcast to the dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.db import session_scope
from app.events import log_activity, log_event, publish
from app.models import BrowserAction, BrowserTask


def _now() -> datetime:
    return datetime.now(timezone.utc)


def task_dict(t: BrowserTask) -> dict:
    pending = dict(t.pending or {})
    if pending.get("type") == "confirm":  # the dashboard needs the summary, not the fingerprint
        pending = {"type": "confirm", "say": pending.get("say"), "label": pending.get("label")}
    return {"id": t.id, "profile_id": t.profile_id, "installation_id": t.installation_id, "channel": t.channel,
            "goal": t.goal, "status": t.status, "pending": pending, "steps": t.steps, "model_calls": t.model_calls,
            "site_name": t.site_name, "last_url": t.last_url, "result": t.result,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None}


def action_dict(a: BrowserAction) -> dict:
    return {"id": a.id, "task_id": a.task_id, "kind": a.kind, "element_label": a.element_label, "value": a.value,
            "reason": a.reason, "ok": a.ok, "error": a.error, "url_after": a.url_after,
            "page_changed": a.page_changed, "latency_ms": a.latency_ms,
            "created_at": a.created_at.isoformat() if a.created_at else None}


def create_task(*, profile_id: int, installation_id: str, phone: str, channel: str, goal: str,
                site_name: Optional[str]) -> int:
    with session_scope() as s:
        t = BrowserTask(profile_id=profile_id, installation_id=installation_id, phone=phone, channel=channel,
                        goal=goal[:500], site_name=site_name)
        s.add(t)
        s.commit()
        s.refresh(t)
        data = task_dict(t)
    publish("agent", kind="task", task=data)
    log_activity("browser_task_started", f"Started in the browser: {goal[:120]}", profile_id=profile_id)
    log_event("browser_task_started", profile_id=profile_id, channel=channel, task_id=None)
    return data["id"]


def update_task(task_id: int, **fields: Any) -> dict:
    with session_scope() as s:
        t = s.get(BrowserTask, task_id)
        for k, v in fields.items():
            setattr(t, k, v)
        if fields.get("status") in ("completed", "stopped", "failed", "interrupted") and not t.completed_at:
            t.completed_at = _now()
        s.add(t)
        s.commit()
        s.refresh(t)
        data = task_dict(t)
    publish("agent", kind="task", task=data)
    return data


def get_task(task_id: int) -> Optional[BrowserTask]:
    with session_scope() as s:
        return s.get(BrowserTask, task_id)


def add_action(task_id: int, kind: str, **fields: Any) -> None:
    with session_scope() as s:
        a = BrowserAction(task_id=task_id, kind=kind, **fields)
        s.add(a)
        s.commit()
        s.refresh(a)
        data = action_dict(a)
    publish("agent", kind="action", action=data)
