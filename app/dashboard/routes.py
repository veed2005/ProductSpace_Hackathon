"""Dashboard pages and live event stream. Owner: Lane D.

GET /dashboard                 the page (static/index.html)
GET /dashboard/static/{file}   its CSS and JS
GET /dashboard/events          Server-Sent Events stream of app.events.publish() payloads
/api/...                       JSON API (app/dashboard/api.py)
"""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from app import events
from app.dashboard import api

STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter()
router.include_router(api.router)


@router.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@router.get("/dashboard/static/{name}", include_in_schema=False)
def static(name: str) -> FileResponse:
    path = (STATIC_DIR / name).resolve()
    if path.parent != STATIC_DIR.resolve() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@router.get("/dashboard/events")
async def stream(request: Request) -> StreamingResponse:
    q = events.subscribe()

    async def gen():
        try:
            yield ": connected\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")
