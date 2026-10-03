"""Live websocket connections to paired Chrome extensions, and request/response over them.

Everything here runs on the server's event loop. The agent awaits `BrowserConnection.command(...)`;
the extension executes the action in the page and answers with a `result` message.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any, Callable, Optional

from fastapi import WebSocket

from fastapi.concurrency import run_in_threadpool

from app.browser import pdf
from app.browser.protocol import ActionResult, PageState, PdfDocument, TabInfo
from app.browser.sanitize import sanitize_page, sanitize_tab
from app.events import publish

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 20.0


class BrowserGone(Exception):
    """The extension disconnected (browser closed, laptop asleep, network)."""


class BrowserConnection:
    def __init__(self, ws: Optional[WebSocket], installation_id: str, profile_id: int, label: str = ""):
        self.ws = ws
        self.installation_id = installation_id
        self.profile_id = profile_id
        self.label = label
        self.tab: dict[str, Any] = {}  # {tab_id, url, title} of the active tab
        self.connected_at = time.time()
        self.last_seen = time.time()
        self.closed = False
        self._pending: dict[str, asyncio.Future] = {}
        self._cache: Optional[PageState] = None
        self._pdfs: dict[str, PdfDocument] = {}
        self._on_close: list[Callable[[BrowserConnection], None]] = []
        self._send_lock = asyncio.Lock()

    # ------------------------------------------------------------ transport

    async def send(self, message: dict) -> None:
        if self.closed or self.ws is None:
            raise BrowserGone(self.installation_id)
        async with self._send_lock:
            await self.ws.send_json(message)

    def handle(self, msg: dict) -> None:
        """A message from the extension (after hello)."""
        self.last_seen = time.time()
        kind = msg.get("type")
        if kind == "result":
            fut = self._pending.pop(str(msg.get("id")), None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif kind == "tab":
            tab = msg.get("tab") or {}
            if tab != self.tab:
                self.tab = tab
                self._cache = None
                publish("browser", kind="tab", installation_id=self.installation_id,
                        profile_id=self.profile_id, tab=self.public_tab())
        elif kind == "page_changed":
            self._cache = None
            if msg.get("url") and self.tab.get("tab_id") == msg.get("tab_id"):
                self.tab = {**self.tab, "url": msg["url"], "title": msg.get("title", self.tab.get("title"))}

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(BrowserGone(self.installation_id))
        self._pending.clear()
        for callback in self._on_close:
            try:
                callback(self)
            except Exception:
                log.exception("browser close callback failed")

    def on_close(self, callback: Callable[[BrowserConnection], None]) -> None:
        self._on_close.append(callback)

    async def request(self, action: str, *, tab_id: Optional[int] = None, timeout: float = COMMAND_TIMEOUT_S,
                      **args: Any) -> dict:
        """Send one command and wait for its result message."""
        cid = secrets.token_hex(6)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        try:
            await self.send({"type": "command", "id": cid, "action": action, "args": args, "tab_id": tab_id})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "timeout", "detail": f"{action} took over {timeout:.0f}s"}
        finally:
            self._pending.pop(cid, None)

    # ------------------------------------------------------------ page state and actions

    async def page_state(self, tab_id: Optional[int] = None, *, fresh: bool = False) -> PageState:
        """The current page as a sanitized snapshot. Reuses the last snapshot while nothing changed."""
        if not fresh and self._cache is not None and (tab_id is None or self._cache.tab_id == tab_id):
            return self._cache
        msg = await self.request("get_page_state", tab_id=tab_id)
        if not msg.get("ok"):
            raise PageUnavailable(msg.get("error") or "unknown", msg.get("detail"))
        state = sanitize_page(PageState.model_validate(msg["data"]))
        if state.content_type == "application/pdf":
            state.document = await self._pdf(state)
            state.site_name = pdf.friendly_name(state.document)
            state.title = state.title or state.document.title
        self._cache = state
        return state

    async def _pdf(self, state: PageState) -> PdfDocument:
        """The whole PDF this tab shows, read once per address and kept while the connection lasts."""
        cached = self._pdfs.get(state.url)
        if cached is not None:
            return cached
        msg = await self.request("read_pdf", tab_id=state.tab_id, timeout=45)
        title = pdf.title_from_url(state.url)
        if not msg.get("ok"):
            log.info("PDF unreadable: %s", msg.get("detail"))
            return PdfDocument(title=title, error=msg.get("detail") or "It couldn't be downloaded.")
        if msg["data"].get("screenshot_base64"):  # only what's on screen; not cached, the file may come back
            log.info("PDF on screen only (%s): %s", msg["data"].get("reason"), title or "untitled")
            return pdf.on_screen(msg["data"]["screenshot_base64"], title, msg["data"].get("reason") or "")
        data = pdf.decode(msg["data"]["pdf_base64"])
        document = await run_in_threadpool(pdf.read, data, state.url)
        log.info("PDF read: %s pages, %s chars%s%s", document.pages, len(document.text),
                 " (scanned)" if document.scanned else "", f" error: {document.error}" if document.error else "")
        if not document.error:
            self._pdfs[state.url] = document
            while len(self._pdfs) > 4:
                self._pdfs.pop(next(iter(self._pdfs)))
        return document

    async def act(self, action: str, *, tab_id: Optional[int], doc_id: Optional[str] = None,
                  element_id: Optional[str] = None, value: Optional[str] = None) -> ActionResult:
        self._cache = None  # whatever happens, the page may have changed
        msg = await self.request(action, tab_id=tab_id, doc_id=doc_id, element_id=element_id, value=value)
        self._cache = None
        if msg.get("ok"):
            return ActionResult.model_validate({"action": action, **(msg.get("data") or {})})
        return ActionResult(success=False, action=action, error=msg.get("error") or "failed",
                            detail=msg.get("detail"))

    async def tabs(self, tab_id: Optional[int] = None) -> list[TabInfo]:
        """Every tab in the window `tab_id` is in, in tab-strip order: titles only, scrubbed like page text."""
        msg = await self.request("list_tabs", tab_id=tab_id, timeout=5)
        if not msg.get("ok"):
            return []
        return [sanitize_tab(TabInfo.model_validate(t)) for t in (msg.get("data") or {}).get("tabs", [])]

    async def switch_tab(self, tab_id: Optional[int], target_tab_id: int) -> ActionResult:
        """Bring another tab of the same window to the front. `new_tab_id` in the result is the page now."""
        self._cache = None
        msg = await self.request("switch_tab", tab_id=tab_id, target_tab_id=target_tab_id)
        self._cache = None
        if msg.get("ok"):
            return ActionResult.model_validate({"action": "switch_tab", **(msg.get("data") or {})})
        return ActionResult(success=False, action="switch_tab", error=msg.get("error") or "failed",
                            detail=msg.get("detail"))

    async def screenshot(self, tab_id: Optional[int] = None) -> Optional[str]:
        """Base64 JPEG of the visible tab, or None. Used only by the opt-in vision fallback."""
        msg = await self.request("screenshot", tab_id=tab_id, timeout=8)
        image = (msg.get("data") or {}).get("image") if msg.get("ok") else None
        return image.split(",", 1)[1] if image and "," in image else None

    def public_tab(self) -> dict:
        return {"tab_id": self.tab.get("tab_id"), "url": self.tab.get("url"), "title": self.tab.get("title")}


class PageUnavailable(Exception):
    def __init__(self, code: str, detail: Optional[str] = None):
        super().__init__(f"{code}: {detail or ''}")
        self.code = code
        self.detail = detail


class BrowserHub:
    def __init__(self) -> None:
        self._by_id: dict[str, BrowserConnection] = {}
        self._listeners: list[Callable[[BrowserConnection], None]] = []

    def on_connect(self, callback: Callable[[BrowserConnection], None]) -> Callable[[], None]:
        """Call `callback(conn)` when a browser connects; returns a function that unsubscribes."""
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def register(self, conn: BrowserConnection) -> None:
        old = self._by_id.get(conn.installation_id)
        if old is not None and old is not conn:
            old.close()  # the same installation reconnected; the old socket is dead or about to be
        self._by_id[conn.installation_id] = conn
        for callback in list(self._listeners):
            try:
                callback(conn)
            except Exception:
                log.exception("browser connect listener failed")
        publish("browser", kind="connected", installation_id=conn.installation_id, profile_id=conn.profile_id,
                tab=conn.public_tab())

    def unregister(self, conn: BrowserConnection) -> None:
        if self._by_id.get(conn.installation_id) is conn:
            del self._by_id[conn.installation_id]
            publish("browser", kind="disconnected", installation_id=conn.installation_id,
                    profile_id=conn.profile_id)
        conn.close()

    def get(self, installation_id: str) -> Optional[BrowserConnection]:
        return self._by_id.get(installation_id)

    def for_profile(self, profile_id: int) -> list[BrowserConnection]:
        """Connected browsers for this person, most recently active first."""
        conns = [c for c in self._by_id.values() if c.profile_id == profile_id and not c.closed]
        return sorted(conns, key=lambda c: c.last_seen, reverse=True)

    def all(self) -> list[BrowserConnection]:
        return list(self._by_id.values())


hub = BrowserHub()
