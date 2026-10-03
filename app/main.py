"""FastAPI app. Every lane's router is registered here once, so nobody needs to edit this file."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.channels import messaging, voice
from app.config import get_settings
from app.dashboard import routes as dashboard
from app.db import init_db

logging.basicConfig(level=get_settings().log_level)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Formline", lifespan=lifespan)
app.include_router(messaging.router)
app.include_router(voice.router)
app.include_router(dashboard.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
