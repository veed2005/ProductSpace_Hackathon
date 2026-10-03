"""Dashboard pages and live event stream. Owner: Lane D.

Everything here is local-only: requests must come from this machine and not through a proxy
such as ngrok (which Twilio needs during the demo). See `local_only`.

GET /dashboard                 the page (static/index.html)
GET /dashboard/static/{file}   its CSS and JS
GET /dashboard/events          Server-Sent Events stream of app.events.publish() payloads
/api/...                       JSON API (app/dashboard/api.py, app/dashboard/forms_api.py)
"""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app import events
from app.config import get_settings
from app.dashboard import agent_api, api, control_api, forms_api

STATIC_DIR = Path(__file__).parent / "static"
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}  # testclient: Starlette's TestClient
_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")


def local_only(request: Request) -> None:
    """Refuse anything that didn't originate on this machine (ngrok forwards from localhost but adds
    X-Forwarded-For). The dashboard shows personal data and can wipe the demo."""
    if get_settings().dashboard_remote:
        return
    host = request.client.host if request.client else ""
    if host not in _LOCAL_HOSTS or any(h in request.headers for h in _PROXY_HEADERS):
        raise HTTPException(403, "The dashboard is only available on the computer running Formline.")


router = APIRouter(dependencies=[Depends(local_only)])
router.include_router(api.router)
router.include_router(forms_api.router)
router.include_router(control_api.router)
router.include_router(agent_api.router)

FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" '
           'fill="#1d4ed8"/><text x="32" y="45" font-family="system-ui,Arial" font-size="38" font-weight="800" '
           'text-anchor="middle" fill="#fff">F</text></svg>')


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


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
