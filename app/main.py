"""FastAPI app. Every lane's router is registered here once, so nobody needs to edit this file."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool

from app import reminders
from app.channels import messaging, voice
from app.config import get_settings
from app.contracts import TurnRequest, TurnResult
from app.core.turn import handle_turn
from app.dashboard import routes as dashboard
from app.dashboard.demo import sync_forms
from app.db import init_db

logging.basicConfig(level=get_settings().log_level)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    sync_forms()  # Task.form_id references the Form table; mirror forms/ into it
    reminders.start_scheduler()
    yield
    reminders.stop_scheduler()


app = FastAPI(title="Formline", lifespan=lifespan)
app.include_router(messaging.router)
app.include_router(voice.router)
app.include_router(dashboard.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/dev/turn")
async def dev_turn(req: TurnRequest) -> TurnResult:
    """Simulator entry point (FORMLINE_DEV_ENDPOINTS=true only). Same brain, no Twilio."""
    if not get_settings().dev_endpoints:
        raise HTTPException(status_code=404)
    return await run_in_threadpool(handle_turn, req)
