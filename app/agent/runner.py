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

from app.agent import fields
from app.agent import memory as agent_memory
from app.agent import store
from app.agent.decision import Decision, Step
from app.agent.policy import classify_reply, is_consequential, is_stop, page_fingerprint, validate_step
from app.agent.prompts import CONFIRM_NUDGE, system_prompt
from app.agent.render import looks_loading, page_text, site_name, tab_name, tabs_text
from app.browser.hub import BrowserConnection, BrowserGone, PageUnavailable
from app.browser.protocol import ActionResult, PageElement, PageState, TabInfo
from app.browser.sanitize import mask
from app.config import get_settings
from app.events import log_event
from app.formcall import language as lang_mod
from app.llm import client as llm

log = logging.getLogger(__name__)

MAX_DECISIONS = 16  # model calls per caller turn before checking in with the caller
MAX_ERRORS = 3
NARRATE_EVERY_S = 6.0
FILLER_AFTER_S = 2.2
FILL_ACTIONS = {"type", "clear", "select", "check", "uncheck"}  # steps a question may carry: they only fill in fields
MAX_AUTOFILL = 12  # fields one "fill it in from what you have saved?" may cover
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
        "trouble": "Sorry, I couldn't get that to work on this page. What would you like me to try?",
        "long": "This is taking a while. Should I keep going?",
        "confirm_q": "Should I go ahead?",
        "press": "I'm ready to press {label}. Should I go ahead?",
        "remembered": "Got it. I'll remember that for next time.",
        "not_remembered": "Okay, I won't save it.",
        "autofilled": "Okay, I filled that in.",
        "autofill_declined": "No problem. I'll ask you instead.",
        "tab": "I'm on your {name} tab now.",
    },
    "es": {
        "filler": "Un momento.",
        "stopped": "Listo, me detuve. No haré nada más hasta que me diga.",
        "declined": "De acuerdo, no lo haré. No se envió nada. ¿Qué le gustaría hacer?",
        "stale": "La página cambió antes de terminar, así que no presioné nada. Déjeme revisar de nuevo.",
        "gone": "Perdí la conexión con su navegador. Asegúrese de que la computadora esté encendida y Chrome abierto, y avíseme.",
        "no_page": "No veo ninguna página abierta. Abra el sitio que necesita en Chrome y avíseme.",
        "error": "Perdón, algo salió mal. ¿Puede repetirlo?",
        "trouble": "Perdón, no pude hacerlo en esta página. ¿Qué quiere que intente?",
        "long": "Esto está tardando. ¿Sigo intentando?",
        "confirm_q": "¿Lo hago?",
        "press": "Estoy listo para presionar {label}. ¿Lo hago?",
        "remembered": "Listo. Lo recordaré para la próxima vez.",
        "not_remembered": "De acuerdo, no lo guardaré.",
        "autofilled": "Listo, ya lo completé.",
        "autofill_declined": "No hay problema. Se lo preguntaré.",
        "tab": "Ahora estoy en la pestaña de {name}.",
    },
}


class BrowserPort(Protocol):
    async def page_state(self, *, fresh: bool = False) -> PageState: ...
    async def act(self, step: Step, doc_id: str) -> ActionResult: ...
    async def overlay(self, text: Optional[str]) -> None: ...


class ConnectionPort:
    """The real browser: a paired extension. The agent works in one tab at a time: the one the task started
    in, a tab its click opened, a tab the person switched to, or one it chose with switch_tab."""

    def __init__(self, conn: BrowserConnection):
        self.conn = conn
        self.tab_id: Optional[int] = None
        self._switched: Optional[str] = None
        self._seen_active: Optional[int] = None  # the browser's active tab when we last looked
        self._trail: list[int] = []  # tabs the agent was on before this one, oldest first ("previous" pops it)
        self._handles: dict[int, str] = {}  # tab id -> "T1", "T2"...; a tab keeps its handle while it's open

    def _move(self, tab_id: int) -> None:
        if self.tab_id is not None and tab_id != self.tab_id:
            self._trail.append(self.tab_id)
        self.tab_id = tab_id

    async def page_state(self, *, fresh: bool = False) -> PageState:
        # Follow the person: if they switched to another tab, that's the page now. Only a *change* in the
        # browser's active tab counts, so the report of a switch the agent made itself (which can arrive late)
        # isn't mistaken for the person's.
        active = self.conn.tab.get("tab_id")
        if active is not None and active != self._seen_active:
            self._seen_active = active
            if self.tab_id is not None and active != self.tab_id:
                self._move(active)
                self._switched = self.conn.tab.get("title") or "another tab"
                fresh = True
        state = await self.conn.page_state(self.tab_id, fresh=fresh)
        if self.tab_id is None:
            self.tab_id = state.tab_id
        return state

    def take_tab_switch(self) -> Optional[str]:
        switched, self._switched = self._switched, None
        return switched

    async def tabs(self) -> list[TabInfo]:
        """The tabs in the agent's window, each with a handle the model can name in switch_tab."""
        tabs = await self.conn.tabs(self.tab_id)
        for t in tabs:
            t.handle = self._handles.setdefault(t.tab_id, f"T{len(self._handles) + 1}")
            t.current = t.tab_id == self.tab_id
        return tabs

    async def act(self, step: Step, doc_id: str) -> ActionResult:
        if step.action == "switch_tab":
            return await self._switch_tab((step.value or "").strip())
        result = await self.conn.act(step.action, tab_id=self.tab_id, doc_id=doc_id, element_id=step.element_id,
                                     value=step.value)
        if result.new_tab_id:
            self._move(result.new_tab_id)
        return result

    async def _switch_tab(self, target: str) -> ActionResult:
        def refuse(error: str, detail: str) -> ActionResult:
            return ActionResult(success=False, action="switch_tab", error=error, detail=detail)

        tabs = await self.tabs()
        by_id = {t.tab_id: t for t in tabs}
        here = by_id.get(self.tab_id)
        if target.lower() == "previous":
            while self._trail and not (self._trail[-1] in by_id and by_id[self._trail[-1]].switchable
                                       and self._trail[-1] != self.tab_id):
                self._trail.pop()  # closed since, or not a page Formline can use
            if not self._trail:
                return refuse("no_tab", "There is no earlier tab to go back to. Pick a tab from the tab list instead.")
            tab = by_id[self._trail.pop()]
        else:
            tab = next((t for t in tabs if t.handle.lower() == target.lower()), None)
            if tab is None:
                closed = target.upper() in self._handles.values()
                return refuse("no_tab", "That tab was closed." if closed else
                              f"{target!r} is not in the tab list; use a handle from the current list.")
            if tab.tab_id == self.tab_id:
                return refuse("invalid_action", "You are already on that tab.")
            if not tab.switchable:
                return refuse("unsupported_page", "Formline can't work on that tab (it isn't a regular web page).")
        result = await self.conn.switch_tab(self.tab_id, tab.tab_id)
        if result.success:
            self._move(tab.tab_id)
            result.new_tab_id = tab.tab_id
            result.page_changed = True
            was, now = (tab_name(here) if here else "the last tab"), tab_name(tab)
            if here and was == now:  # two tabs of one site: their titles tell them apart
                was, now = repr(here.title), repr(tab.title)
            result.detail = f"moved from {was} to {now}"
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

NO_PAGE_CODES = {"unsupported_page", "no_tab"}  # the tab isn't a web page the extension can read
NO_PAGE_DOC = "no-page"


def no_page() -> PageState:
    """What the model is shown when the tab can't be read (a blank new tab, a browser settings page), so a
    caller who asks to look something up still gets a search in a new tab. Anything else it decides here is
    dropped and the caller is asked to open a website, as before."""
    return PageState(doc_id=NO_PAGE_DOC, url="", title="No readable page", site_name="a blank tab", elements=[
        PageElement(role="text", label=(
            "This tab is not a web page you can read or act on (a blank tab or a browser page). You can only "
            "use new_tab (to look something up or reach a website by searching for it) or switch_tab to a tab "
            "in the tab list. If the caller's request needs neither, return blocked."))])


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


# A question about what's on screen ("who's Peter New?", "how much is my rent", "explain this"): the agent gets a
# screenshot with its first look, so it can answer from what the person actually sees. Commands don't need one.
_QUESTION = re.compile(r"\?\s*$|^(what|who|whom|whose|where|when|why|how|which|is|are|was|were|am|can|could|does|do|did|"
                       r"will|would|should|explain|tell me|read|describe|summari[sz]e|whats|whos|wheres|hows|"
                       r"qué|que|quién|quien|dónde|donde|cuándo|cuando|por qué|cómo|como|cuál|cual|cuánto|cuanto|"
                       r"explica|explíca|lee|léeme|dime)\b")

# "Keep going", "try again", "OK I logged in": the caller wants the same task to continue.
_CONTINUE = re.compile(r"^(ok|okay|yes|yeah|sure|continue|keep going|go on|go ahead|try again|resume|done|ready|"
                       r"i did( it)?|i ve done it|i (ve |have )?(logged|signed) in|it s (done|open|loaded|ready)|"
                       r"sigue|continúa|continua|listo|ya)\b")


def _page_key(page: PageState) -> str:
    """Which page this is, for spotting a click that keeps not working: the address plus its headings. A wizard's
    "Next" on each new step is a different page; "More..." pressed again on an unchanged page is the same."""
    heads = "|".join(e.label for e in page.elements if e.role == "heading")[:300]
    return f"{page.url}#{heads}"


def _number(match: re.Match) -> str:
    """'$1,767.00' -> '1767', '2,080.50' -> '2080_5': one token per amount, so '$1,950' can't pass for '$1,767'
    by sharing a '$1' and a '00', and '$1,767' still matches '$1,767.00'."""
    raw = match.group().replace("$", "").replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return raw
    return (str(int(value)) if value == int(value) else repr(value)).replace(".", "_")


def _norm(text: str) -> str:
    """For matching quoted evidence against page text: case, punctuation and spacing don't matter, so a
    quote that spans a heading and the line under it still matches; amounts compare as whole numbers."""
    text = (text or "").replace("’", "'").casefold()
    text = re.sub(r"\[\w+\]", " ", text)  # snapshot ids the model may have copied, like [e12]
    text = re.sub(r"\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?|\$?\d+\.\d+|\$\d+", _number, text)
    return " ".join(re.sub(r"[^\w']+", " ", text).split())


class BrowserAgent:
    def __init__(self, *, browser: BrowserPort, say: Callable[[str], Awaitable[None]], profile_id: int,
                 installation_id: str, phone: str, channel: str = "voice", language: str = "en",
                 decide: Optional[Decider] = None,
                 on_language: Optional[Callable[[str, str], Awaitable[None]]] = None):
        self.browser = browser
        self._say_cb = say
        self.profile_id = profile_id
        self.installation_id = installation_id
        self.phone = phone
        self.channel = channel
        self.language = language if language in lang_mod.CALL_LANGUAGES else "en"
        self.decide = decide or llm_decide
        self.on_language = on_language  # (language code, the caller's words): the call decides whether to switch

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
        self.last_outcome: Optional[str] = None  # question | answer | blocked | trouble | stopped | completed
        self._fresh_turn = False  # the next decision is the first since the caller spoke
        self._loop_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._offers: list[agent_memory.Proposal] = []  # details the caller gave, waiting to be offered for saving
        # May saved details be typed into forms for this request? None: not asked yet. True/False: the caller's answer.
        self._autofill: Optional[bool] = None
        # (page doc id, element id) of the form field the caller just answered a question about
        self._answered_field: Optional[tuple[str, str]] = None

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
        if self.pending and self.pending.get("type") == "remember" and await self._resolve_memory_offer(text):
            return
        if self.pending and self.pending.get("type") == "confirm":
            await self._resolve_confirmation(text)
            return
        if is_stop(text) and self.task_id and self.status in ("active", "waiting_input"):
            await self.stop(spoken=True)
            return
        if self.pending and self.pending.get("type") == "autofill":
            await self._resolve_autofill(text)
            return
        answering = bool(self.pending and self.pending.get("type") == "question")
        carry_on = self.last_outcome in ("blocked", "trouble", "stopped", "long") and _CONTINUE.match(_norm(text))
        self._answered_field = ((self.pending.get("doc"), self.pending["field"])
                                if answering and self.pending.get("field") else None)
        if self.task_id is None or not (answering or carry_on):
            await self._new_task(text)  # a new request ("who's Peter New?", "check my profile") starts fresh
        else:
            self.pending = None
            self._persist(status="active", pending={})
        self._fresh_turn = bool(_QUESTION.search(text) or _QUESTION.match(_norm(text)))  # show it the screen
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
        self.last_outcome = "stopped"
        if self.task_id:
            self._persist(status="stopped", pending={})
            store.add_action(self.task_id, "stopped", reason="Caller said stop")
        await self.browser.overlay(None)
        if spoken:
            await self._say(await self._t("stopped"))

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

    async def _t(self, key: str, **kw) -> str:
        """One of Formline's own lines in the call's language (translated once and cached beyond English/Spanish)."""
        if self.language in SAY:
            return SAY[self.language][key].format(**kw)
        template = await run_in_threadpool(lang_mod.phrase, self.language, f"agent.{key}", SAY["en"][key])
        return template.format(**kw)

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
            await self._say(await self._t("gone"))
        except PageUnavailable as e:
            log.info("page unavailable: %s", e)
            self._persist(status="waiting_input")
            await self._say(await self._t("no_page"))
        except Exception:
            log.exception("browser agent failed")
            self._persist(status="waiting_input")
            await self._say(await self._t("error"))
        finally:
            filler.cancel()

    async def _filler(self, started: float) -> None:
        await asyncio.sleep(FILLER_AFTER_S)
        if self.last_spoke < started:
            await self._say(await self._t("filler"))

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
        self._autofill = None
        if earlier:
            self.notes.append("This is the caller's latest request. Use the earlier conversation for context when "
                              "they refer back to something ('that one', 'what about dogs?'), but don't fill in "
                              "details they haven't given for this request (like which movie); ask instead.")
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
        doubted = False  # an answer's quote already failed once this turn
        reworded = False  # a question already went back once for asking several things or reading a format hint
        unfilled = False  # a question already went back once because the caller's last answer wasn't typed in
        narrated = False
        for _ in range(MAX_DECISIONS):
            try:
                page = await self._page()
            except PageUnavailable as e:
                if e.code not in NO_PAGE_CODES:
                    raise
                page = no_page()  # a blank tab or a browser page: the model can still open a search in a new tab
            decision = await self._decide(page)
            if page.doc_id == NO_PAGE_DOC and not (
                    decision.kind == "act" and decision.steps and decision.steps[0].action in ("new_tab", "switch_tab")):
                raise PageUnavailable("unsupported_page", "nothing to do without a readable page")
            kind = decision.kind

            if kind == "ask_user":
                page, offered = await self._fill_before_asking(decision, page)
                if offered:
                    return
                answered = None
                if self._answered_field and self._answered_field[0] == page.doc_id:  # still the same page
                    answered = page.control(self._answered_field[1])
                if (answered and not unfilled and not (answered.value or "").strip()
                        and decision.field_id != answered.id):
                    # The caller answered a question about this field, but the model moved on without filling it.
                    unfilled = True
                    store.add_action(self.task_id, "rejected", element_label=decision.say, ok=False,
                                     error="Asked the next question before typing the last answer",
                                     reason=decision.reason)
                    reply = next((t for who, t in reversed(self.transcript) if who == "caller"), "")
                    self.notes.append(f"You asked the caller for [{answered.id}] {answered.label!r}, they replied "
                                      f"{reply!r}, and the field is still empty. Type their answer into it now "
                                      "(act) before you ask anything else. Carry on without it only if that reply "
                                      "clearly isn't an answer (they skipped it or asked you something).")
                    continue
                problem = fields.question_problem(decision.say, page)
                if problem and not reworded:
                    # One chance to ask for a single field in plain words; after that it's asked anyway, with any
                    # format hint taken out, so the caller is never left waiting.
                    reworded = True
                    store.add_action(self.task_id, "rejected", element_label=decision.say, ok=False, error=problem,
                                     reason=decision.reason)
                    self.notes.append(problem)
                    continue
                decision.say = fields.strip_hints(decision.say)
                self.last_outcome = "question"
                self.pending = {"type": "question", "say": decision.say}
                target = page.control(decision.field_id)
                if target is not None and target.role in fields.FIELD_ROLES:
                    self.pending.update(field=target.id, doc=page.doc_id)
                self._persist(status="waiting_input", pending=self.pending)
                store.add_action(self.task_id, "ask", element_label=decision.say, reason=decision.reason)
                await self._say(decision.say)
                return

            if kind == "offer_fill":
                if await self._offer_autofill(decision.steps, page, decision.reason):
                    return
                errors += 1  # nothing in it could be offered (the notes say why); don't let it loop
                if errors >= MAX_ERRORS:
                    break
                continue

            if kind == "answer":
                problem = self._unsupported(decision.evidence, page) if decision.evidence else None
                if problem and not doubted:
                    # One chance to quote it better; after that the answer is given anyway and flagged on the
                    # dashboard. A person asking about their own screen must never get a dead end.
                    doubted = True
                    store.add_action(self.task_id, "unverified", element_label=decision.evidence, ok=False, error=problem)
                    self.notes.append(problem)
                    continue
                self._persist(status="waiting_input", pending={})
                store.add_action(self.task_id, "answer", element_label=decision.say, value=decision.evidence,
                                 reason=decision.reason, ok=not problem,
                                 error="Its quote couldn't be confirmed on the page" if problem else None)
                self.last_outcome = "answer"
                await self._say(decision.say)
                await self._offer_memory()
                return

            if kind == "blocked":
                self.last_outcome = "blocked"
                self._persist(status="waiting_input", pending={})
                store.add_action(self.task_id, "blocked", element_label=decision.say, reason=decision.reason, ok=False)
                await self._say(decision.say)
                return

            if kind == "done":
                ok, why = await self._verify_done(decision)
                if ok:
                    store.add_action(self.task_id, "verified", element_label=decision.evidence, reason=decision.reason)
                    self.last_outcome = "completed"
                    self._persist(status="completed", pending={}, result=f"{decision.say} [page: {decision.evidence}]")
                    log_event("browser_task_completed", profile_id=self.profile_id, channel=self.channel,
                              steps=self.steps, model_calls=self.model_calls)
                    await self.browser.overlay(None)
                    await self._say(decision.say)
                    await self._offer_memory()
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
                await self._request_confirmation(step, page, fields.strip_hints(decision.say), decision.reason)
                return

            # act. Its say is a short status; a question is only ever asked with ask_user, so it isn't said twice.
            if decision.say and not re.search(r"[?？]", decision.say) and (
                    not narrated or time.monotonic() - self.last_spoke > NARRATE_EVERY_S):
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
                if await self._needs_offer(step) and await self._offer_autofill(
                        decision.steps[i:3], page, decision.reason, quiet=True):
                    return  # it was about to type a saved detail without asking: ask first
                result = await self._execute(step, page, decision.reason)
                if not result.success:
                    self.notes.append(f"{step.action} [{step.element_id}] failed: {result.error}"
                                      f"{' (' + result.detail + ')' if result.detail else ''}")
                    errors += 1
                    break
                errors = 0
                if step.action == "switch_tab":
                    # The agent changed what the person sees, so it says where it is now.
                    await self._say(await self._t("tab", name=site_name(await self._page())))
                    break
                if result.page_changed and step.action in ("click", "press_enter", "go_back", "navigate", "search",
                                                           "new_tab"):
                    break  # look at the new page before doing more
            if errors >= MAX_ERRORS:
                break
        else:
            self.last_outcome = "long"
            self._persist(status="waiting_input")
            await self._say(await self._t("long"))
            return
        self.last_outcome = "trouble"
        self._persist(status="waiting_input")
        await self._say(await self._t("trouble"))

    async def _fill_before_asking(self, decision: Decision, page: PageState) -> tuple[PageState, bool]:
        """A question that also carries steps ("type Ana, then ask for the last name"): run the ones that only
        fill in fields, checked like any other step, so the caller's last answer isn't lost. Nothing is clicked.
        Returns the page now, and True if the caller was instead asked whether to use their saved details."""
        for i, step in enumerate(decision.steps[:3]):
            if step.action not in FILL_ACTIONS:
                break
            err = validate_step(step, page)
            if err or is_consequential(step, page):
                self.notes.append(f"Step {step.action} [{step.element_id}] was not run: {err or 'needs a yes first'}")
                break
            if await self._needs_offer(step) and await self._offer_autofill(
                    decision.steps[i:3], page, decision.reason, quiet=True):
                return page, True
            result = await self._execute(step, page, decision.reason)
            page = await self._page()
            if not result.success:
                self.notes.append(f"{step.action} [{step.element_id}] failed: {result.error}")
                break
        return page, False

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
        known = await run_in_threadpool(agent_memory.recall, self.profile_id)
        if known and self._autofill is False:
            known = ("(hidden: the caller asked you not to use their saved details for this request. Ask them for "
                     "each field instead.)")
        elif known and self._autofill:
            known += "\nThe caller has agreed to use these for this request: type them wherever the form asks."
        elif known:
            known += ("\nThe caller has NOT been asked about using these yet: before typing any of them into a "
                      "form, use offer_fill.")
        tabs = tabs_text(await self._tabs())
        tab_list = (f"Tabs open in this browser window (titles only; you see a tab's page after switching to it):\n"
                    f"{tabs}\n\n") if tabs else ""
        user = (f"CURRENT PAGE:\n{shown}\nEND OF PAGE\n\n{tab_list}"
                f"Caller's goal: {self.goal}\n\n"
                f"What you remember about the caller (from earlier conversations):\n{known or '(nothing saved yet)'}\n\n"
                f"Conversation so far (most recent last):\n{convo}\n\n"
                f"Steps you have taken in the browser:\n{steps}\n\nNotes for this turn:\n{notes}")
        content: object = user
        images: list[str] = list(page.document.images) if page.document else []
        if images:
            self._fresh_turn = False
        note = ""
        if images and page.document and page.document.on_screen_only:
            note = "\n\nThe picture attached is the part of the PDF visible on screen, nothing more."
        elif images:
            note = "\n\nThe pictures attached are the PDF's pages, in order. Read them to answer."
        else:
            shot = await self._screen(page)
            if shot:
                images = [shot]
                note = ("\n\nA screenshot of what the person sees right now is attached. Use it to understand and "
                        "explain what's on their screen (including things the text snapshot misses); act only "
                        "through the ids in the snapshot.")
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
        await self._follow_language(decision)
        await self._collect_memory(decision)
        _trace(user, decision, ms)
        self.model_calls += 1
        self._persist(model_calls=self.model_calls)
        log_event("agent_decision", profile_id=self.profile_id, channel=self.channel, ms=ms, kind=decision.kind)
        log.info("agent %s in %sms: %s | %s", decision.kind, ms,
                 [(s.action, s.element_id, s.value) for s in decision.steps], decision.reason)
        return decision

    async def _follow_language(self, decision: Decision) -> None:
        """Tell the call which language the model heard, before anything from this decision is spoken."""
        code = (decision.language or "").split("-")[0].strip().lower()
        if not self.on_language or code not in lang_mod.CALL_LANGUAGES or code == self.language:
            return
        latest = next((text for who, text in reversed(self.transcript) if who == "caller"), "")
        await self.on_language(code, latest)

    async def _tabs(self) -> list[TabInfo]:
        """The window's tabs, if this browser can list them. Never fails a decision."""
        if not hasattr(self.browser, "tabs"):
            return []
        try:
            return await self.browser.tabs()
        except BrowserGone:
            raise
        except Exception:
            log.warning("couldn't list tabs", exc_info=True)
            return []

    # ------------------------------------------------------------ memory

    async def _collect_memory(self, decision: Decision) -> None:
        """Keep the details the model proposes remembering, once code has checked them. Repeating what's already
        saved just re-confirms it; anything new or different waits to be offered to the caller."""
        for item in decision.remember or []:
            proposal = agent_memory.check(item.key, item.value, language=self.language)
            if isinstance(proposal, str):
                self.notes.append(f"Not remembered: {proposal}")
                continue
            if await run_in_threadpool(agent_memory.already_known, self.profile_id, proposal):
                await run_in_threadpool(agent_memory.refresh, self.profile_id, proposal)
                continue
            self._offers = [p for p in self._offers if p.path != proposal.path] + [proposal]

    async def _offer_memory(self) -> None:
        """After a result or an answer, ask before saving anything new the caller told us."""
        if not self._offers:
            return
        offers, self._offers = self._offers, []
        self.pending = {"type": "remember", "proposals": offers}
        if self.task_id:
            store.add_action(self.task_id, "remember_offer", element_label=", ".join(p.path for p in offers),
                             reason="Caller shared details worth reusing")
        offer = agent_memory.offer_text(offers, self.language)
        if self.language not in SAY:
            offer = await run_in_threadpool(lang_mod.translate, offer, self.language)
        await self._say(offer)

    # ------------------------------------------------------------ filling a form from saved details

    async def _needs_offer(self, step: Step) -> bool:
        """True if this step would type a saved detail the caller hasn't agreed to reuse and didn't just say:
        the backstop for a model that skips offer_fill."""
        if self._autofill is not None or step.action != "type":
            return False  # already agreed, or declined (then the details aren't shown to the model at all)
        if self._answered_field and step.element_id == self._answered_field[1]:
            return False  # the caller's answer to the question just asked
        said = {w for who, text in self.transcript if who == "caller" for w in agent_memory.words(text)}
        if all(w in said for w in agent_memory.words(step.value or "")):
            return False  # the caller said it on this call
        facts = await run_in_threadpool(agent_memory.saved, self.profile_id)
        source = agent_memory.source_of(step.value, facts, strict=True)
        return bool(source and source.fresh)

    async def _offer_autofill(self, steps: list[Step], page: PageState, reason: str, *, quiet: bool = False) -> bool:
        """Ask the caller whether to fill these fields from their saved details. Only steps that type or choose
        a value traced to a saved, up-to-date fact are kept; the question names what's saved, never the values.
        True if the caller was asked. False (with notes for the model, unless `quiet`) if nothing could be
        offered, or the caller already agreed, in which case the fields are simply filled in."""
        facts = await run_in_threadpool(agent_memory.saved, self.profile_id)
        keep, keys, notes = [], [], []
        for step in steps[:MAX_AUTOFILL]:
            if step.action not in ("type", "select"):
                continue
            el = page.control(step.element_id)
            err = validate_step(step, page)
            if err:
                notes.append(f"offer_fill step [{step.element_id}] was left out: {err}")
                continue
            if step.action == "type" and (el.value or "").strip():
                continue  # already filled in
            source = agent_memory.source_of(step.value, facts)
            if source is None:
                notes.append(f"[{el.id}] {el.label!r} was left out of offer_fill: its value isn't one of the "
                             "caller's saved details. Ask the caller for it.")
            elif not source.fresh:
                notes.append(f"[{el.id}] {el.label!r} was left out of offer_fill: that saved detail may be out "
                             "of date. Ask the caller whether it's still right.")
            else:
                keep.append(step)
                if source.key not in keys:
                    keys.append(source.key)
        if not quiet:
            self.notes.extend(notes)
        if not keep:
            if not quiet:
                self.notes.append("offer_fill had no field that the caller's saved details can fill. Ask the "
                                  "caller for what the form needs, one field at a time.")
            return False
        if self._autofill:  # they already said yes for this request (say, on the form's previous page)
            await self._fill(keep, page, reason)
            return False
        if self._autofill is False:
            return False
        offer = agent_memory.fill_offer_text(keys, self.language)
        if self.language not in SAY:
            offer = await run_in_threadpool(lang_mod.translate, agent_memory.fill_offer_text(keys, "en"), self.language)
        self.last_outcome = "question"
        self.pending = {"type": "autofill", "steps": [s.model_dump() for s in keep], "doc_id": page.doc_id,
                        "keys": keys, "say": offer}
        self._persist(status="waiting_input", pending=self.pending)
        store.add_action(self.task_id, "autofill_offer", element_label=", ".join(keys), reason=reason)
        await self._say(offer)
        return True

    async def _fill(self, steps: list[Step], page: PageState, reason: str) -> int:
        """Type saved details into the fields the caller agreed to. Each step is checked again on the page as
        it is now; a field that has a value by now is left alone. Returns how many were filled."""
        filled = 0
        for step in steps:
            el = page.control(step.element_id)
            if validate_step(step, page) or is_consequential(step, page):
                continue
            if step.action == "type" and (el.value or "").strip():
                continue
            if (await self._execute(step, page, reason)).success:
                filled += 1
            page = await self._page()
        return filled

    async def _resolve_autofill(self, text: str) -> None:
        """The caller's reply to "Would you like me to fill that in for you?"."""
        pending, self.pending = self.pending or {}, None
        verdict = classify_reply(text, self.language)
        what = ", ".join(pending.get("keys", []))
        self._persist(status="active", pending={})
        if verdict == "yes":
            self._autofill = True
            store.add_action(self.task_id, "autofill_accepted", element_label=what, reason="Caller said yes")
            self._start(self._run_autofill(pending))
            return
        if verdict == "no":
            self._autofill = False
            store.add_action(self.task_id, "autofill_declined", element_label=what, reason="Caller said no")
            self.notes.append("The caller doesn't want their saved details used for this request. Ask them for "
                              "each field, one at a time.")
            await self._say(await self._t("autofill_declined"))
        else:
            store.add_action(self.task_id, "autofill_declined", element_label=what,
                             reason="Caller said something else")
            self.notes.append("You offered to fill in the caller's saved details and they said something else "
                              "(see the conversation). Nothing was filled in. Do what they said.")
        self._start(self._loop())

    async def _run_autofill(self, pending: dict) -> None:
        page = await self._page()
        if page.doc_id != pending.get("doc_id"):
            self.notes.append("The caller agreed to use their saved details, but the page changed before they "
                              "were typed. Look again and type them where this page asks.")
        else:
            steps = [Step.model_validate(s) for s in pending.get("steps", [])]
            filled = await self._fill(steps, page, "Caller agreed to use saved details")
            if filled:
                await self._say(await self._t("autofilled"))
            self.notes.append(f"The caller agreed to use their saved details and {filled} field(s) were filled "
                              "in. Carry on with whatever is still empty, asking for one field at a time.")
        await self._loop()

    async def _resolve_memory_offer(self, text: str) -> bool:
        """The caller's reply to "Would you like me to remember...?". False if they moved on to something else:
        nothing is saved and their words are handled as a new request."""
        offers = (self.pending or {}).get("proposals", [])
        self.pending = None
        verdict = classify_reply(text, self.language)
        if verdict == "other":
            return False
        saved = 0
        if verdict == "yes":
            saved = await run_in_threadpool(agent_memory.save, self.profile_id, offers, task_id=self.task_id)
        if self.task_id:
            store.add_action(self.task_id, "remembered" if saved else "not_remembered",
                             element_label=", ".join(p.path for p in offers),
                             reason="Caller said yes" if verdict == "yes" else "Caller said no")
        await self._say(await self._t("remembered" if saved else "not_remembered"))
        return True

    async def _screen(self, page: PageState) -> Optional[str]:
        """A screenshot of what the person sees: for the first look after they ask something (so questions about
        their screen are answered from what they actually see), after a failed step, or when the page has almost
        no text. FORMLINE_SCREENSHOTS=false turns it off."""
        fresh, self._fresh_turn = self._fresh_turn, False
        if not hasattr(self.browser, "screenshot"):
            return None
        sparse = len([e for e in page.elements if (e.label or "").strip()]) < 4
        failed = any("failed" in n or "was not run" in n for n in self.notes)
        s = get_settings()
        if not (s.screenshots and (fresh or failed or sparse)) and not (s.vision_fallback and sparse):
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
        if step.action == "type":  # "March 14, 1988" goes into a "Birthdate (MM-DD-YYYY)" box as 03-14-1988
            fitted = fields.fit_value(step.value, el)
            if fitted != step.value:
                step = step.model_copy(update={"value": fitted})
        if self._answered_field and step.action in ("type", "select") and step.element_id == self._answered_field[1]:
            self._answered_field = None
        label = el.label if el else (step.value or step.action)
        started = time.perf_counter()
        result = await self.browser.act(step, page.doc_id)
        key_what = (step.value or "") if step.action == "search" else (label or "")
        self._done_steps.append((_page_key(page), step.action, key_what.strip().lower()))
        if step.action == "search":  # what the search routine did, in its own words
            label = result.detail or (f"search box {el.label!r}" if el else "the site search")
        if step.action == "new_tab":
            label = "a new tab with a web search"
        if step.action == "switch_tab":  # "moved from Riverbend to Maple County Public Library"
            label = result.detail if result.success else f"tab {step.value}"
        ms = round((time.perf_counter() - started) * 1000)
        shown = None
        if step.action in ("type", "select", "navigate", "scroll", "search", "new_tab") and step.value:
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
        elif step.action == "new_tab":
            self.history.append(f"new_tab searching the web for {shown!r} -> {outcome}{changed}")
        elif step.action == "switch_tab":
            self.history.append(f"switch_tab {step.value} -> {outcome}: {result.detail or 'no details'}")
        else:
            self.history.append(f"{step.action} [{step.element_id}] {label!r}{typed} -> {outcome}{changed}")
        return result

    @staticmethod
    def _unsupported(evidence: Optional[str], page: PageState) -> Optional[str]:
        """None if the quote is on the page (or in its document) as the model saw it, else the problem.

        A quote may stitch several pieces with "...", line breaks or sentence ends. A piece counts if it appears
        word for word, or if its words are there even though the layout separates them: a short piece (up to 6
        words, like a name next to "Cast", or a label and its amount in a table) needs 80% of its words, a
        longer one 90%. Made-up quotes still fail; real ones from tables and lists no longer do."""
        hay = _norm(page_text(page))
        words_on_page = set(hay.split())
        cleaned = re.sub(r"\[Page \d+ of \d+\]", "\n", evidence or "")
        pieces = [_norm(p) for p in re.split(r"\.\.\.|…|\n|(?<=[.!?])[\"”)]?\s+(?=[\"“(]?[A-Z0-9])", cleaned)]
        pieces = [p for p in pieces if p]

        def there(piece: str) -> bool:
            if piece in hay:
                return True
            words = [w for w in piece.split() if len(w) >= 3 or any(c.isdigit() for c in w)]
            if not words:
                return True  # nothing meaningful to check ("a", "of")
            share = sum(1 for w in words if w in words_on_page) / len(words)
            return share >= (0.8 if len(words) <= 6 else 0.9)

        if pieces and all(there(p) for p in pieces):
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
            await self._request_confirmation(step, page, await self._t("press", label=el.label if el else "that"),
                                             decision.reason)

    async def _request_confirmation(self, step: Step, page: PageState, say: str, reason: str) -> None:
        el = page.control(step.element_id)
        say = (say or "").strip() or await self._t("press", label=el.label if el else "that")
        if not say.endswith("?"):
            say = f"{say} {await self._t('confirm_q')}"
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
        verdict = classify_reply(text, self.language)
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
            await self._say(await self._t("declined"))
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
            await self._say(await self._t("stale"))
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
