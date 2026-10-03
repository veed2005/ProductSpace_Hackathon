"""Dashboard API for the browser agent ("call the internet"). Local-only like the rest of /api.

GET /api/agent/browsers                      paired browsers, connected or not, with their current tab
GET /api/agent/tasks                         recent browser tasks, newest first
GET /api/agent/tasks/{id}                    one task with its action feed and the call transcript
GET /api/agent/browsers/{id}/page            what the agent sees: the sanitized page snapshot as text
"""

from datetime import timedelta

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from app.agent.render import page_text, site_name
from app.agent.store import action_dict, task_dict
from app.browser.hub import BrowserGone, PageUnavailable, hub
from app.dashboard.api import mask_phone
from app.db import session_scope
from app.models import BrowserAction, BrowserInstallation, BrowserTask, Message, Profile

router = APIRouter(prefix="/api/agent")


@router.get("/browsers")
def browsers() -> list[dict]:
    with session_scope() as s:
        rows = s.exec(select(BrowserInstallation).where(BrowserInstallation.revoked_at.is_(None))).all()
        names = {p.id: (p.display_name or "Unnamed", p.phone) for p in s.exec(select(Profile)).all()}
    out = []
    for inst in rows:
        conn = hub.get(inst.id)
        name, phone = names.get(inst.profile_id, ("Unknown", ""))
        out.append({
            "installation_id": inst.id, "profile_id": inst.profile_id, "profile_name": name,
            "phone_masked": mask_phone(phone), "label": inst.label, "connected": bool(conn and not conn.closed),
            "tab": conn.public_tab() if conn else None,
            "last_seen": inst.last_seen_at.isoformat() if inst.last_seen_at else None,
        })
    return sorted(out, key=lambda b: (not b["connected"], b["profile_name"]))


@router.get("/tasks")
def tasks(limit: int = 10) -> list[dict]:
    with session_scope() as s:
        rows = s.exec(select(BrowserTask).order_by(BrowserTask.id.desc()).limit(min(limit, 50))).all()
        return [task_dict(t) for t in rows]


@router.get("/tasks/{task_id}")
def task(task_id: int) -> dict:
    with session_scope() as s:
        t = s.get(BrowserTask, task_id)
        if t is None:
            raise HTTPException(404, "No such task")
        actions = s.exec(select(BrowserAction).where(BrowserAction.task_id == task_id)
                         .order_by(BrowserAction.id)).all()
        since = t.started_at - timedelta(minutes=2)
        until = (t.completed_at or t.started_at + timedelta(hours=2)) + timedelta(seconds=30)
        messages = s.exec(select(Message).where(Message.phone == t.phone, Message.created_at >= since,
                                                Message.created_at <= until).order_by(Message.id)).all()
        profile = s.get(Profile, t.profile_id)
        return {
            "task": task_dict(t),
            "caller": {"name": profile.display_name if profile else None, "phone_masked": mask_phone(t.phone)},
            "actions": [action_dict(a) for a in actions],
            "messages": [{"direction": m.direction, "channel": m.channel, "text": m.text,
                          "at": m.created_at.isoformat()} for m in messages],
        }


@router.get("/browsers/{installation_id}/page")
async def page(installation_id: str) -> dict:
    conn = hub.get(installation_id)
    if conn is None or conn.closed:
        raise HTTPException(409, "That browser isn't connected")
    try:
        state = await conn.page_state()
    except (PageUnavailable, BrowserGone) as e:
        raise HTTPException(409, f"No readable page: {e}")
    controls = sum(1 for e in state.elements if e.id)
    return {"site": site_name(state), "title": state.title, "url": state.url, "controls": controls,
            "items": len(state.elements), "text": page_text(state)}
