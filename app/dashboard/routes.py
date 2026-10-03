"""Dashboard pages and live event stream. Owner: Lane D.

GET /dashboard         the page (projector-friendly, Phase 8)
GET /dashboard/events  Server-Sent Events stream of app.events.publish() payloads
"""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from app import events

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    # TODO(Lane D, Phase 8): real dashboard. This page just prints live events.
    return """<!doctype html><meta charset="utf-8"><title>Formline</title>
<h1>Formline dashboard</h1><p>Live events:</p><pre id="log"></pre>
<script>
const es = new EventSource('/dashboard/events');
es.onmessage = e => { document.getElementById('log').textContent = e.data + '\\n' + document.getElementById('log').textContent; };
</script>"""


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
