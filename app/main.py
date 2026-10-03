import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.db import init_db

settings = get_settings()
logging.basicConfig(level=settings.log_level)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Formline", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    # Placeholder until Phase 8.
    return "<!doctype html><title>Formline</title><h1>Formline dashboard</h1><p>Coming in Phase 8.</p>"
