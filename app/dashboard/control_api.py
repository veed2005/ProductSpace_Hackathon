"""Metrics and demo controls. Owner: Lane D.

GET  /api/metrics
GET  /api/demo/state                       personas' phones/PINs, pending reminders, profiles
POST /api/demo/seed                        {"returning_phone": "+1..."} (optional)
POST /api/demo/reset                       wipe everything and reseed
POST /api/demo/reminders/{id}/send         send one reminder now
POST /api/profiles/{id}/reset-pin          partner-initiated PIN reset
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlmodel import select

from app import reminders
from app.core import identity
from app.dashboard.api import _iso, mask_phone
from app.dashboard.demo import DEMO_PINS, DemoPhones, reset_demo, seed_demo
from app.db import session_scope
from app.events import log_activity
from app.metrics import compute_metrics
from app.models import Profile

router = APIRouter(prefix="/api")


@router.get("/metrics")
def metrics() -> dict:
    return compute_metrics()


class SeedBody(BaseModel):
    returning_phone: Optional[str] = None


def _phones(body: Optional[SeedBody]) -> DemoPhones:
    phones = DemoPhones()
    if body and body.returning_phone:
        phone = body.returning_phone.strip()
        if not (phone.startswith("+") and phone[1:].isdigit() and 8 <= len(phone) <= 16):
            raise HTTPException(400, "Phone must look like +12175550123")
        phones.returning = phone
    return phones


@router.post("/demo/seed")
async def demo_seed(body: Optional[SeedBody] = None) -> dict:
    return await run_in_threadpool(seed_demo, _phones(body))


@router.post("/demo/reset")
async def demo_reset(body: Optional[SeedBody] = None) -> dict:
    return await run_in_threadpool(reset_demo, _phones(body))


@router.get("/demo/state")
def demo_state() -> dict:
    with session_scope() as s:
        profiles = s.exec(select(Profile).order_by(Profile.phone, Profile.id)).all()
    pending = reminders.pending_reminders()
    names = {p.id: p.display_name or "Unnamed" for p in profiles}
    return {
        "pins": DEMO_PINS,
        "profiles": [{"id": p.id, "name": p.display_name or "Unnamed", "phone_masked": mask_phone(p.phone),
                      "pin_set": bool(p.pin_hash)} for p in profiles],
        "reminders": [{"id": r.id, "name": names.get(r.profile_id, "?"), "message": r.message,
                       "due_at": _iso(r.due_at)} for r in pending],
    }


@router.post("/demo/reminders/{reminder_id}/send")
async def demo_send_reminder(reminder_id: int) -> dict:
    sent = await run_in_threadpool(reminders.send_now, reminder_id)
    if not sent:
        raise HTTPException(404, "No pending reminder with that id")
    return {"sent": True}


@router.post("/profiles/{profile_id}/reset-pin")
def reset_pin(profile_id: int) -> dict:
    if identity.get_profile(profile_id) is None:
        raise HTTPException(404, "profile not found")
    identity.reset_pin(profile_id)
    log_activity("pin_reset", "A partner reset the PIN; a new one will be set on next contact",
                 profile_id=profile_id)
    return {"reset": True}
