"""Endpoints the Chrome extension uses.

POST /browser/pair/start     {phone, delivery: sms|call}            -> {pairing_id, phone, delivery, dev_code?}
POST /browser/pair/confirm   {pairing_id, code, name?, pin?, label?} -> {status: paired, installation_id, token, ...}
POST /browser/unpair         Authorization: Bearer <token>, {installation_id}
WS   /browser/ws             first message: {"type": "hello", installation_id, token, version}
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.browser import pairing
from app.browser.hub import BrowserConnection, PageUnavailable, hub
from app.config import get_settings
from app.core import identity

log = logging.getLogger(__name__)
router = APIRouter(prefix="/browser")

HELLO_TIMEOUT_S = 10.0


class PairStart(BaseModel):
    phone: str
    delivery: str = "sms"


class PairConfirm(BaseModel):
    pairing_id: str
    code: str
    name: Optional[str] = None
    pin: Optional[str] = None
    label: Optional[str] = None


class Unpair(BaseModel):
    installation_id: str


class DevCommand(BaseModel):
    action: str
    installation_id: Optional[str] = None
    element_id: Optional[str] = None
    value: Optional[str] = None
    doc_id: Optional[str] = None
    tab_id: Optional[int] = None


def _error(e: pairing.PairingError) -> HTTPException:
    status = 429 if e.code in ("rate_limited", "too_many_attempts") else 502 if e.code == "delivery_failed" else 400
    return HTTPException(status, {"code": e.code, "message": e.message})


@router.post("/pair/start")
async def pair_start(body: PairStart) -> dict:
    try:
        started = await run_in_threadpool(pairing.start_pairing, body.phone, body.delivery)
    except pairing.PairingError as e:
        raise _error(e)
    out = {"pairing_id": started.pairing_id, "phone": started.phone, "delivery": started.delivery,
           "expires_in": int(pairing.CODE_TTL.total_seconds())}
    if started.dev_code:
        out["dev_code"] = started.dev_code
    return out


@router.post("/pair/confirm")
async def pair_confirm(body: PairConfirm) -> dict:
    try:
        return await run_in_threadpool(pairing.confirm_pairing, body.pairing_id, body.code, name=body.name,
                                       pin=body.pin, label=body.label)
    except pairing.PairingError as e:
        raise _error(e)


@router.post("/unpair")
async def unpair(body: Unpair, authorization: str = Header("")) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    if await run_in_threadpool(pairing.authenticate, body.installation_id, token) is None:
        raise HTTPException(401, "Not paired")
    await run_in_threadpool(pairing.revoke, body.installation_id)
    conn = hub.get(body.installation_id)
    if conn:
        hub.unregister(conn)
    return {"ok": True}


@router.websocket("/ws")
async def browser_ws(ws: WebSocket) -> None:
    origin = ws.headers.get("origin", "")
    if origin.startswith(("http://", "https://")):  # a web page, not the extension
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        hello = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT_S)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
        await _close(ws, 1008)
        return
    inst = None
    if isinstance(hello, dict) and hello.get("type") == "hello":
        inst = await run_in_threadpool(pairing.authenticate, str(hello.get("installation_id", "")),
                                       str(hello.get("token", "")))
    if inst is None:
        await ws.send_json({"type": "error", "error": "unauthorized"})
        await _close(ws, 4401)
        return

    profile = await run_in_threadpool(identity.get_profile, inst.profile_id)
    conn = BrowserConnection(ws, inst.id, inst.profile_id, inst.label or "")
    if isinstance(hello.get("tab"), dict):
        conn.tab = hello["tab"]
    hub.register(conn)
    log.info("browser %s connected (profile %s)", inst.id, inst.profile_id)
    await ws.send_json({"type": "welcome", "profile_name": profile.display_name if profile else ""})
    pinger = asyncio.create_task(_ping(conn))
    try:
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict):
                conn.handle(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        log.exception("browser websocket failed")
    finally:
        pinger.cancel()
        hub.unregister(conn)
        log.info("browser %s disconnected", inst.id)


async def _ping(conn: BrowserConnection) -> None:
    """Keeps the extension's service worker awake and the connection's liveness visible."""
    while not conn.closed:
        await asyncio.sleep(20)
        try:
            await conn.send({"type": "ping"})
        except Exception:
            return


async def _close(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code=code)
    except Exception:
        pass


@router.post("/dev/command")
async def dev_command(body: DevCommand, request: Request) -> dict:
    """Drive a connected browser by hand (FORMLINE_DEV_ENDPOINTS=true, this machine only). For debugging
    and the end-to-end tests; the agent never uses it."""
    from app.dashboard.routes import local_only

    if not get_settings().dev_endpoints:
        raise HTTPException(404)
    local_only(request)
    conn = hub.get(body.installation_id) if body.installation_id else next(iter(hub.all()), None)
    if conn is None:
        raise HTTPException(409, "No browser connected")
    if body.action == "get_page_state":
        try:
            return (await conn.page_state(body.tab_id, fresh=True)).model_dump()
        except PageUnavailable as e:
            raise HTTPException(409, {"code": e.code, "detail": e.detail})
    if body.action == "screenshot":
        image = await conn.screenshot(body.tab_id)
        return {"ok": bool(image), "jpeg_base64_chars": len(image or "")}
    if body.action == "list_tabs":
        return {"tabs": [t.model_dump() for t in await conn.tabs(body.tab_id)]}
    if body.action == "switch_tab":  # value: the target tab's id, from list_tabs
        if not (body.value or "").isdigit():
            raise HTTPException(400, "switch_tab needs the target tab id in value")
        return (await conn.switch_tab(body.tab_id, int(body.value))).model_dump()
    result = await conn.act(body.action, tab_id=body.tab_id, doc_id=body.doc_id, element_id=body.element_id,
                            value=body.value)
    return result.model_dump()
