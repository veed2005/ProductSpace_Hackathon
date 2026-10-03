"""The browser-agent loop: caller goal + live page -> model decision -> checked action -> new page.

    caller speaks -> handle() -> (background) _loop():
        page = browser.page_state()          # cached until the page changes
        decision = model(goal, conversation, steps so far, page)
        ask_user  -> say the question, wait for the caller
        confirm   -> store the exact step + page fingerprint server-side, ask, wait for "yes"
        done      -> only if the evidence text is on a fresh snapshot of the page
        act       -> validate each step, run it in the browser, record the browser's result

The model proposes; code validates, executes through the extension, and verifies. A consequential
step runs only from the stored pending confirmation after the caller says yes, never from a later
model decision. "Stop" cancels the loop immediately.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Awaitable, Callable, Optional, Protocol

from fastapi.concurrency import run_in_threadpool

from app.agent import store
from app.agent.decision import Decision, Step
from app.agent.policy import classify_reply, is_consequential, is_stop, page_fingerprint, validate_step
from app.agent.prompts import CONFIRM_NUDGE, system_prompt
from app.agent.render import looks_loading, page_text, site_name
from app.browser.hub import BrowserConnection, BrowserGone, PageUnavailable
from app.browser.protocol import ActionResult, PageState
from app.browser.sanitize import mask
from app.config import get_settings
from app.events import log_event
from app.llm import client as llm

log = logging.getLogger(__name__)

MAX_DECISIONS = 16  # model calls per caller turn before checking in with the caller
MAX_ERRORS = 3
NARRATE_EVERY_S = 6.0
FILLER_AFTER_S = 2.2
CONFIRM_TTL_S = 300
DECIDE_TIMEOUT_S = 30.0

SAY = {
    "en": {
        "filler": "One moment.",
        "stopped": "Okay, I stopped. Nothing else will happen unless you tell me.",
        "declined": "Okay, I won't do that. Nothing was submitted. What would you like to do instead?",
        "stale": "The page changed before I could finish, so I didn't press anything. Let me take another look.",
        "gone": "I lost the connection to your browser. Make sure your computer is on and Chrome is open, then tell me when you're ready.",
        "no_page": "I can't see a web page right now. Open the website you need in Chrome, then tell me when it's up.",
        "error": "Sorry, something went wrong on my end. Could you say that again?",
        "trouble": "I'm having trouble with this page. Could you tell me another way to do this, or try again in a moment?",
        "long": "This is taking a while. Should I keep going?",
        "confirm_q": "Should I go ahead?",
        "press": "I'm ready to press {label}. Should I go ahead?",
    },
    "es": {
        "filler": "Un momento.",
        "stopped": "Listo, me detuve. No haré nada más hasta que me diga.",
        "declined": "De acuerdo, no lo haré. No se envió nada. ¿Qué le gustaría hacer?",
        "stale": "La página cambió antes de terminar, así que no presioné nada. Déjeme revisar de nuevo.",
        "gone": "Perdí la conexión con su navegador. Asegúrese de que la computadora esté encendida y Chrome abierto, y avíseme.",
        "no_page": "No veo ninguna página abierta. Abra el sitio que necesita en Chrome y avíseme.",
        "error": "Perdón, algo salió mal. ¿Puede repetirlo?",
        "trouble": "Tengo problemas con esta página. ¿Me dice otra forma de hacerlo, o lo intento de nuevo en un momento?",
        "long": "Esto está tardando. ¿Sigo intentando?",
        "confirm_q": "¿Lo hago?",
        "press": "Estoy listo para presionar {label}. ¿Lo hago?",
    },
}


class BrowserPort(Protocol):
    async def page_state(self, *, fresh: bool = False) -> PageState: ...
    async def act(self, step: Step, doc_id: str) -> ActionResult: ...
    async def overlay(self, text: Optional[str]) -> None: ...


class ConnectionPort:
    """The real browser: a paired extension, pinned to the tab the task started in."""

    def __init__(self, conn: BrowserConnection):
        self.conn = conn
        self.tab_id: Optional[int] = None
        self._switched: Optional[str] = None

    async def page_state(self, *, fresh: bool = False) -> PageState:
        # Follow the person: if they switched to another tab, that's the page now.
        active = self.conn.tab.get("tab_id")
        if active is not None and self.tab_id is not None and active != self.tab_id:
            self.tab_id = active
            self._switched = self.conn.tab.get("title") or "another tab"
            fresh = True
        state = await self.conn.page_state(self.tab_id, fresh=fresh)
        if self.tab_id is None:
            self.tab_id = state.tab_id
        return state

    def take_tab_switch(self) -> Optional[str]:
        switched, self._switched = self._switched, None
        return switched

    async def act(self, step: Step, doc_id: str) -> ActionResult:
        result = await self.conn.act(step.action, tab_id=self.tab_id, doc_id=doc_id, element_id=step.element_id,
                                     value=step.value)
        if result.new_tab_id:
            self.tab_id = result.new_tab_id
        return result

    async def overlay(self, text: Optional[str]) -> None:
        try:
            await self.conn.request("overlay", tab_id=self.tab_id, timeout=3, text=text)
        except Exception:
            pass  # cosmetic

    async def screenshot(self) -> Optional[str]:
        try:
            return await self.conn.screenshot(self.tab_id)
        except Exception:
            return None


Decider = Callable[[str, list[dict]], Awaitable[Decision]]


async def llm_decide(system: str, messages: list[dict]) -> Decision:
    return await asyncio.wait_for(
        run_in_threadpool(llm.structured, Decision, system=system, messages=messages, model=llm.agent_model(),
                          max_tokens=900, temperature=0),
        DECIDE_TIMEOUT_S)


def _trace(prompt: str, decision: Decision, ms: int) -> None:
    """FORMLINE_AGENT_TRACE=path.jsonl records every prompt and decision, for tuning prompts and models.
    Prompts contain page text and what the caller said: local debugging only."""
    path = os.environ.get("FORMLINE_AGENT_TRACE")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ms": ms, "prompt": prompt, "decision": decision.model_dump()}) + "\n")


def _page_key(page: PageState) -> str:
    """Which page this is, for spotting a click that keeps not working: the address plus its headings. A wizard's
    "Next" on each new step is a different page; "More..." pressed again on an unchanged page is the same."""
    heads = "|".join(e.label for e in page.elements if e.role == "heading")[:300]
    return f"{page.url}#{heads}"


def _norm(text: str) -> str:
    """For matching quoted evidence against page text: case, punctuation and spacing don't matter, so a
    quote that spans a heading and the line under it still matches."""
    text = (text or "").replace("’", "'").casefold()
    text = re.sub(r"\[\w+\]", " ", text)  # snapshot ids the model may have copied, like [e12]
    return " ".join(re.sub(r"[^\w$']+", " ", text).split())


class BrowserAgent:
    def __init__(self, *, browser: BrowserPort, say: Callable[[str], Awaitable[None]], profile_id: int,
                 installation_id: str, phone: str, channel: str = "voice", language: str = "en",
                 decide: Optional[Decider] = None):
        self.browser = browser
        self._say_cb = say
        self.profile_id = profile_id
        self.installation_id = installation_id
        self.phone = phone
        self.channel = channel
        self.language = language if language in SAY else "en"
        self.decide = decide or llm_decide

        self.task_id: Optional[int] = None
        self.goal: Optional[str] = None
        self.status = "idle"
        self.pending: Optional[dict] = None
        self.transcript: list[tuple[str, str]] = []
        self.history: list[str] = []
        self._done_steps: list[tuple[str, str, str]] = []  # (page, action, element label) actually run, this task
        self._last_seen: Optional[PageState] = None  # the page as the model last saw it, to mark what changed
        self.notes: list[str] = []
        self.inbox: list[str] = []
        self.steps = 0
        self.model_calls = 0
        self.required_confirmation = False
        self.confirmed_executed = False
        self.last_spoke = 0.0
        self._loop_task: Optional[asyncio.Task] = None
        self._stopping = False

    # ------------------------------------------------------------ public

    @property
    def busy(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    async def handle(self, text: str) -> None:
        """One thing the caller said. Returns quickly; browser work continues in the background."""
        text = (text or "").strip()
        if not text:
            return
        if self.busy:
            if is_stop(text):
                await self.stop()
                return
            if not self.pending:
                self.inbox.append(text)  # the model sees it on its next look
                return
            # A question was just asked and the loop is wrapping up: this is the answer.
            try:
                await asyncio.wait_for(asyncio.shield(self._loop_task), 5)
            except Exception:
                pass
        self.transcript.append(("caller", text))
        if self.pending and self.pending.get("type") == "confirm":
            await self._resolve_confirmation(text)
            return
        if is_stop(text) and self.task_id and self.status in ("active", "waiting_input"):
            await self.stop(spoken=True)
            return
        if self.task_id is None or self.status in ("completed", "failed"):
            await self._new_task(text)
        else:
            self.pending = None
            self._persist(status="active", pending={})
        self._start(self._loop())

    async def stop(self, *, spoken: bool = True) -> None:
        """Halt immediately: cancel the loop, drop any pending confirmation, say so."""
        self._stopping = True
        task = self._loop_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._stopping = False
        self.pending = None
        self.required_confirmation = False
        if self.task_id:
            self._persist(status="stopped", pending={})
            store.add_action(self.task_id, "stopped", reason="Caller said stop")
        await self.browser.overlay(None)
        if spoken:
            await self._say(self._t("stopped"))

    async def close(self) -> None:
        """The call ended. Nothing more runs; an unfinished task is marked interrupted."""
        task = self._loop_task
        if task and not task.done():
            task.cancel()
        if self.task_id and self.status not in ("completed", "stopped", "failed"):
            self._persist(status="interrupted", pending={})
        try:
            await self.browser.overlay(None)
        except Exception:
            pass

    # ------------------------------------------------------------ helpers

    def _t(self, key: str, **kw) -> str:
        return SAY[self.language][key].format(**kw)

    async def _say(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.last_spoke = time.monotonic()
        self.transcript.append(("you", text))
        await self._say_cb(text)

    def _persist(self, **fields) -> None:
        if "status" in fields:
            self.status = fields["status"]
        if self.task_id:
            store.update_task(self.task_id, **fields)

    def _start(self, coro) -> None:
        self._loop_task = asyncio.create_task(self._guarded(coro))

    async def _guarded(self, coro) -> None:
        filler = asyncio.create_task(self._filler(time.monotonic()))
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except BrowserGone:
            self._persist(status="waiting_input")
            await self._say(self._t("gone"))
        except PageUnavailable as e:
            log.info("page unavailable: %s", e)
            self._persist(status="waiting_input")
            await self._say(self._t("no_page"))
        except Exception:
            log.exception("browser agent failed")
            self._persist(status="waiting_input")
            await self._say(self._t("error"))
        finally:
            filler.cancel()

    async def _filler(self, started: float) -> None:
        await asyncio.sleep(FILLER_AFTER_S)
        if self.last_spoke < started:
            await self._say(self._t("filler"))

    async def _new_task(self, goal: str) -> None:
        page = None
        try:
            page = await self.browser.page_state()
        except (PageUnavailable, BrowserGone):
            pass
        earlier = bool(self.goal)
        self.goal = goal
        self.history, self.notes = [], []
        self._done_steps = []
        if earlier:
            self.notes.append("This is a NEW request. Earlier conversation is context only: don't reuse details "
                              "from earlier requests (like which movie or which date) unless the caller refers to "
                              "them. If the new request is missing something, ask.")
        self.steps = self.model_calls = 0
        self.required_confirmation = self.confirmed_executed = False
        self.task_id = store.create_task(profile_id=self.profile_id, installation_id=self.installation_id,
                                         phone=self.phone, channel=self.channel, goal=goal,
                                         site_name=site_name(page) if page else None)
        self.status = "active"
        await self.browser.overlay("Formline is helping on the phone. Say “stop” to pause.")

    # ------------------------------------------------------------ the loop

    async def _loop(self) -> None:
        errors = 0
        narrated = False
        for _ in range(MAX_DECISIONS):
            page = await self._page()
            decision = await self._decide(page)
            kind = decision.kind

            if kind == "ask_user":
                self.pending = {"type": "question", "say": decision.say}
                self._persist(status="waiting_input", pending=self.pending)
                store.add_action(self.task_id, "ask", element_label=decision.say, reason=decision.reason)
                await self._say(decision.say)
                return

            if kind == "answer":
                problem = self._unsupported(decision.evidence, page) if decision.evidence else None
                if problem:
                    store.add_action(self.task_id, "unverified", element_label=decision.evidence, ok=False, error=problem)
                    self.notes.append(problem)
                    errors += 1
                    if errors >= MAX_ERRORS:
                        break
                    continue
                self._persist(status="waiting_input", pending={})
                store.add_action(self.task_id, "answer", element_label=decision.say, value=decision.evidence,
                                 reason=decision.reason)
                await self._say(decision.say)
                return

            if kind == "blocked":
                self._persist(status="waiting_input", pending={})
                store.add_action(self.task_id, "blocked", element_label=decision.say, reason=decision.reason, ok=False)
                await self._say(decision.say)
                return

            if kind == "done":
                ok, why = await self._verify_done(decision)
                if ok:
                    store.add_action(self.task_id, "verified", element_label=decision.evidence, reason=decision.reason)
                    self._persist(status="completed", pending={}, result=f"{decision.say} [page: {decision.evidence}]")
                    log_event("browser_task_completed", profile_id=self.profile_id, channel=self.channel,
                              steps=self.steps, model_calls=self.model_calls)
                    await self.browser.overlay(None)
                    await self._say(decision.say)
                    return
                store.add_action(self.task_id, "unverified", element_label=decision.evidence, ok=False, error=why)
                self.notes.append(why)
                errors += 1
                if errors >= MAX_ERRORS:
                    break
                continue

            if not decision.steps:
                self.notes.append(f"kind={kind} needs at least one step")
                errors += 1
                continue

            if kind == "confirm":
                # Several steps: do the harmless leading ones now, and confirm only the final one.
                *lead, step = decision.steps[:3]
                failed = False
                for prior in lead:
                    err = validate_step(prior, page)
                    if err or is_consequential(prior, page):
                        self.notes.append(f"In a confirm, put only the one final step in steps ({err or 'two final steps'})")
                        failed = True
                        break
                    if not (await self._execute(prior, page, decision.reason)).success:
                        failed = True
                        break
                    page = await self._page()
                err = None if failed else validate_step(step, page)
                if failed or err:
                    if err:
                        self.notes.append(f"Your confirm step was not accepted: {err}")
                    errors += 1
                    continue
                await self._request_confirmation(step, page, decision.say, decision.reason)
                return

            # act
            if decision.say and (not narrated or time.monotonic() - self.last_spoke > NARRATE_EVERY_S):
                narrated = True
                await self._say(decision.say)
            for i, step in enumerate(decision.steps[:3]):
                if i:
                    page = await self._page()
                err = validate_step(step, page)
                if err:
                    self.notes.append(f"Step {step.action} [{step.element_id}] was not run: {err}")
                    store.add_action(self.task_id, "rejected", element_label=step.element_id, ok=False, error=err,
                                     reason=decision.reason)
                    errors += 1
                    break
                repeat = self._repeating(step, page)
                if repeat:
                    self.notes.append(repeat)
                    store.add_action(self.task_id, "rejected", element_label=step.element_id, ok=False, error=repeat,
                                     reason=decision.reason)
                    errors += 1
                    break
                if is_consequential(step, page):
                    await self._confirm_flagged(step, page, decision)
                    return
                result = await self._execute(step, page, decision.reason)
                if not result.success:
                    self.notes.append(f"{step.action} [{step.element_id}] failed: {result.error}"
                                      f"{' (' + result.detail + ')' if result.detail else ''}")
                    errors += 1
                    break
                errors = 0
                if result.page_changed and step.action in ("click", "press_enter", "go_back", "navigate", "search"):
                    break  # look at the new page before doing more
            if errors >= MAX_ERRORS:
                break
        else:
            self._persist(status="waiting_input")
            await self._say(self._t("long"))
            return
        self._persist(status="waiting_input")
        await self._say(self._t("trouble"))

    async def _page(self, *, fresh: bool = False) -> PageState:
        """The current page, after any loading indicator goes away (up to about 6 seconds)."""
        page = await self.browser.page_state(fresh=fresh)
        for _ in range(15):
            if not looks_loading(page):
                break
            await asyncio.sleep(0.4)
            page = await self.browser.page_state(fresh=True)
        return page

    @staticmethod
    def _tidy(decision: Decision, page: PageState) -> Decision:
        """Models sometimes mangle an id ("e27},{"). If the cleaned-up id is a real element, use it;
        otherwise leave it for validation to reject."""
        for step in decision.steps:
            if step.element_id and page.control(step.element_id) is None:
                m = re.match(r"\s*\[?([A-Za-z]{0,3}\d+)", step.element_id)
                if m and page.control(m.group(1)):
                    step.element_id = m.group(1)
        return decision

    async def _decide(self, page: PageState) -> Decision:
        take = getattr(self.browser, "take_tab_switch", None)
        switched = take() if take else None
        if switched:
            self.notes.append(f"The person switched to another tab ({switched!r}). Work with the page shown below now.")
        while self.inbox:
            said = self.inbox.pop(0)
            self.transcript.append(("caller", said))
            self.notes.append(f"While you were working the caller said: {said!r}")
        convo = "\n".join(f"{'Caller' if who == 'caller' else 'You'}: {text}" for who, text in self.transcript[-14:])
        steps = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(self.history[-15:])) or "(none yet)"
        notes = "\n".join(f"- {n}" for n in self.notes) or "(none)"
        self.notes = []
        # The page comes first: a long PDF stays an identical prefix across follow-up questions, which the
        # provider can cache.
        shown = page_text(page, previous=self._last_seen)
        self._last_seen = page
        user = (f"CURRENT PAGE:\n{shown}\nEND OF PAGE\n\n"
                f"Caller's goal: {self.goal}\n\nConversation so far (most recent last):\n{convo}\n\n"
                f"Steps you have taken in the browser:\n{steps}\n\nNotes for this turn:\n{notes}")
        content: object = user
        images: list[str] = list(page.document.images) if page.document else []
        note = ""
        if images and page.document and page.document.on_screen_only:
            note = "\n\nThe picture attached is the part of the PDF visible on screen, nothing more."
        elif images:
            note = "\n\nThe pictures attached are the PDF's pages, in order. Read them to answer."
        else:
            shot = await self._vision_fallback(page)
            if shot:
                images = [shot]
                note = ("\n\nA screenshot of the visible page is attached because the snapshot has little text. "
                        "Use it to understand the page; you can still only act on ids in the snapshot.")
        if images:
            content = [*({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": i}}
                         for i in images), {"type": "text", "text": user + note}]
        started = time.perf_counter()
        try:
            decision = await self.decide(system_prompt(self.language), [{"role": "user", "content": content}])
        except Exception as e:  # timeout, API error, refusal, or output cut off mid-JSON: one retry
            log.warning("agent decision failed once (%s); retrying", str(e)[:200])
            decision = await self.decide(system_prompt(self.language), [{"role": "user", "content": content}])
        ms = round((time.perf_counter() - started) * 1000)
        decision = self._tidy(decision, page)
        _trace(user, decision, ms)
        self.model_calls += 1
        self._persist(model_calls=self.model_calls)
        log_event("agent_decision", profile_id=self.profile_id, channel=self.channel, ms=ms, kind=decision.kind)
        log.info("agent %s in %sms: %s | %s", decision.kind, ms,
                 [(s.action, s.element_id, s.value) for s in decision.steps], decision.reason)
        return decision

    async def _vision_fallback(self, page: PageState) -> Optional[str]:
        """A screenshot, only if FORMLINE_VISION_FALLBACK is on and the snapshot is nearly empty."""
        if not get_settings().vision_fallback or not hasattr(self.browser, "screenshot"):
            return None
        meaningful = [e for e in page.elements if (e.label or "").strip()]
        if len(meaningful) >= 4:
            return None
        return await self.browser.screenshot()

    def _repeating(self, step: Step, page: PageState) -> Optional[str]:
        """The same click twice already in the last few steps means it isn't working: make the model change tack."""
        if step.action not in ("click", "press_enter", "search"):
            return None
        el = page.control(step.element_id)
        what = (step.value or "") if step.action == "search" else (el.label if el else step.element_id or "")
        key = (_page_key(page), step.action, what.strip().lower())
        if sum(1 for k in self._done_steps[-5:] if k == key) >= 2:
            return (f"You already did {step.action} on {key[2]!r} twice on this page and it didn't get you closer. Don't do it "
                    "again: try something different (the site's search box or icon, a menu, another link), or ask "
                    "the caller.")
        return None

    async def _execute(self, step: Step, page: PageState, reason: str) -> ActionResult:
        el = page.control(step.element_id)
        label = el.label if el else (step.value or step.action)
        started = time.perf_counter()
        result = await self.browser.act(step, page.doc_id)
        key_what = (step.value or "") if step.action == "search" else (label or "")
        self._done_steps.append((_page_key(page), step.action, key_what.strip().lower()))
        if step.action == "search":  # what the search routine did, in its own words
            label = result.detail or (f"search box {el.label!r}" if el else "the site search")
        ms = round((time.perf_counter() - started) * 1000)
        shown = None
        if step.action in ("type", "select", "navigate", "scroll", "search") and step.value:
            shown = mask(step.value)[:200]
        store.add_action(self.task_id, step.action, element_label=label, value=shown, reason=reason,
                         ok=result.success, error=result.error, url_before=result.url_before,
                         url_after=result.url_after, page_changed=result.page_changed, latency_ms=ms)
        self.steps += 1
        self._persist(steps=self.steps, last_url=result.url_after or page.url)
        outcome = "ok" if result.success else f"FAILED ({result.error})"
        changed = ", page changed" if result.page_changed else ""
        typed = f" = {shown!r}" if shown else ""
        if step.action == "search":
            self.history.append(f"search for {shown!r} -> {outcome}: {result.detail or 'no details'}")
        else:
            self.history.append(f"{step.action} [{step.element_id}] {label!r}{typed} -> {outcome}{changed}")
        return result

    @staticmethod
    def _unsupported(evidence: Optional[str], page: PageState) -> Optional[str]:
        """None if the quote is on the page (or in its document) as the model saw it, else the problem.
        A summary may stitch several quotes with "..." or line breaks: every piece of 3+ words must be there."""
        hay = _norm(page_text(page))
        cleaned = re.sub(r"\[Page \d+ of \d+\]", "\n", evidence or "")
        # Split at "...", line breaks, and sentence ends: each piece must be real, wherever it is.
        pieces = [_norm(p) for p in re.split(r"\.\.\.|…|\n|(?<=[.!?])[\"”)]?\s+(?=[\"“(]?[A-Z0-9])", cleaned)]
        pieces = [p for p in pieces if len(p.split()) >= 3] or [_norm(cleaned)]
        if pieces[0] and all(p in hay for p in pieces):
            return None
        return (f"Your evidence {evidence!r} is not on the current page or in its document. Quote the page "
                "exactly, or answer without evidence only if the page doesn't say.")

    async def _verify_done(self, decision: Decision) -> tuple[bool, str]:
        evidence = _norm(decision.evidence or "")
        if len(evidence) < 6:
            return False, "done needs evidence: copy the exact text on the current page that shows success"
        page = await self._page(fresh=True)
        if self._unsupported(decision.evidence, page):
            return False, (f"Your evidence {decision.evidence!r} is not on the current page. Only use done when "
                           "the page itself shows the goal was achieved, and quote it exactly.")
        if self.required_confirmation and not self.confirmed_executed:
            return False, ("The final step was never confirmed and run, so the goal can't be complete yet. "
                           "Ask the caller to confirm the final step.")
        return True, ""

    # ------------------------------------------------------------ confirmation

    async def _confirm_flagged(self, step: Step, page: PageState, decision: Decision) -> None:
        """The model tried to act on a final button without asking. Get a proper summary, then ask."""
        el = page.control(step.element_id)
        self.notes.append(CONFIRM_NUDGE.format(label=el.label if el else step.element_id))
        retry = await self._decide(page)
        if (retry.kind == "confirm" and retry.steps and retry.steps[0].element_id == step.element_id
                and retry.steps[0].action == step.action):
            await self._request_confirmation(retry.steps[0], page, retry.say, retry.reason)
        else:
            await self._request_confirmation(step, page, self._t("press", label=el.label if el else "that"),
                                             decision.reason)

    async def _request_confirmation(self, step: Step, page: PageState, say: str, reason: str) -> None:
        el = page.control(step.element_id)
        say = (say or "").strip() or self._t("press", label=el.label if el else "that")
        if not say.endswith("?"):
            say = f"{say} {self._t('confirm_q')}"
        self.pending = {"type": "confirm", "step": step.model_dump(), "label": el.label if el else None,
                        "role": el.role if el else None, "doc_id": page.doc_id, "url": page.url,
                        "fingerprint": page_fingerprint(page, step.element_id), "say": say,
                        "created_at": time.time()}
        self.required_confirmation = True
        self._persist(status="waiting_confirmation", pending=self.pending)
        store.add_action(self.task_id, "confirm_request", element_label=el.label if el else None, value=say,
                         reason=reason)
        await self.browser.overlay("Waiting for your OK on the phone…")
        await self._say(say)

    async def _resolve_confirmation(self, text: str) -> None:
        pending = self.pending or {}
        verdict = classify_reply(text)
        if time.time() - pending.get("created_at", 0) > CONFIRM_TTL_S and verdict == "yes":
            verdict = "no"  # too old to trust; ask again
        if verdict == "yes":
            self._start(self._run_confirmed(pending))
            return
        self.pending = None
        self.required_confirmation = False
        store.add_action(self.task_id, "declined", element_label=pending.get("label"),
                         reason="Caller said no" if verdict == "no" else "Caller asked for something else")
        await self.browser.overlay("Formline is helping on the phone. Say “stop” to pause.")
        if verdict == "no":
            self._persist(status="waiting_input", pending={})
            await self._say(self._t("declined"))
            return
        self.notes.append("You asked the caller to confirm, but they said something else instead (see the "
                          "conversation). Nothing was submitted. Do what they asked now.")
        self._persist(status="active", pending={})
        self._start(self._loop())

    async def _run_confirmed(self, pending: dict) -> None:
        """Run exactly the stored step, and only if the page is still what the caller agreed to."""
        step = Step.model_validate(pending["step"])
        page = await self.browser.page_state(fresh=True)
        self.pending = None
        if page_fingerprint(page, step.element_id) != pending.get("fingerprint"):
            store.add_action(self.task_id, "confirm_stale", element_label=pending.get("label"), ok=False,
                             error="The page changed after the caller agreed; nothing was pressed")
            self.notes.append("The page changed after the caller said yes, so the final step was NOT run. "
                              "Look again; confirm again before any final step.")
            self.required_confirmation = False
            self._persist(status="active", pending={})
            await self._say(self._t("stale"))
            await self._loop()
            return
        store.add_action(self.task_id, "confirmed", element_label=pending.get("label"), reason="Caller said yes")
        self._persist(status="active", pending={})
        await self.browser.overlay("Formline is helping on the phone. Say “stop” to pause.")
        result = await self._execute(step, page, "Caller confirmed")
        if result.success:
            self.confirmed_executed = True
            self.notes.append("The caller said yes and the final step ran. Check the page for the result; use done "
                              "only if the page shows it worked.")
        else:
            self.notes.append(f"The confirmed final step failed: {result.error} {result.detail or ''}")
        await self._loop()
