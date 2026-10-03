"""Test doubles for the browser agent: a fake browser that behaves like the Riverbend scheduling flow,
and a scripted "model" that reads the rendered page text and decides like a sensible agent would.

They exercise the real orchestration (app/agent/runner.py, policy, store) without an LLM or Chrome.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

from app.agent.decision import Decision, Step
from app.browser.hub import BrowserConnection, BrowserGone
from app.browser.protocol import ActionResult, PageElement, PageState

BASE = "http://portal.test/"


def el(id_: Optional[str], role: str, label: str, **kw) -> PageElement:
    return PageElement(id=id_, role=role, label=label, **kw)


class FakePortal:
    """Pages: home -> visits -> provider -> reason -> time -> review -> success."""

    def __init__(self):
        self.page = "home"
        self.doc = 1
        self.provider = None
        self.reason = ""
        self.slot = None
        self.booked = 0
        self.actions: list[tuple[str, str, Optional[str]]] = []
        self.gone = False
        self.fail_final = False
        self.overlays: list[Optional[str]] = []

    def nav(self) -> list[PageElement]:
        return [el("n1", "link", "Home"), el("n2", "link", "Visits")]

    def state(self) -> PageState:
        p = self.page
        if p == "home":
            items = [el(None, "heading", "Welcome back, Margaret", level=1), el(None, "text", "You have 1 new message.")]
        elif p == "visits":
            items = [el(None, "heading", "Visits", level=1), el("b1", "button", "Schedule an appointment")]
        elif p == "provider":
            items = [el(None, "heading", "Who would you like to see?", level=2),
                     el("r1", "radio", "Dr. Alan Smith, MD Family Medicine", checked=self.provider == "smith"),
                     el("r2", "radio", "Dr. Priya Patel, MD Internal Medicine", checked=self.provider == "patel"),
                     el("next", "button", "Next", enabled=self.provider is not None)]
        elif p == "reason":
            items = [el(None, "heading", "What's the reason for your visit?", level=2),
                     el("t1", "textbox", "Tell us more about why you want to be seen", value=self.reason, required=True),
                     el("next", "button", "Next")]
        elif p == "time":
            items = [el(None, "heading", "Choose a time", level=2),
                     el("s1", "radio", "Tuesday, October 6 at 10:30 AM", checked=self.slot == "tue"),
                     el("s2", "radio", "Thursday, October 8 at 2:00 PM", checked=self.slot == "thu"),
                     el("next", "button", "Next", enabled=self.slot is not None)]
        elif p == "review":
            items = [el(None, "heading", "Review your appointment", level=1),
                     el(None, "text", f"Dr. Alan Smith, MD. Reason: {self.reason}. Thursday, October 8 at 2:00 PM"),
                     el("back", "button", "Back"), el("final", "button", "Schedule appointment")]
        else:  # success
            items = [el(None, "heading", "Your appointment is scheduled", level=1),
                     el(None, "text", "Confirmation number RB-41234")]
        return PageState(doc_id=f"d{self.doc}", tab_id=1, url=BASE + "#" + p, title="MyRiverbend",
                         site_name="Riverbend Health patient portal", elements=self.nav() + items)

    def go(self, page: str) -> None:
        self.page = page
        self.doc += 1  # new document: old ids are stale

    def apply(self, action: str, element_id: Optional[str], value: Optional[str]) -> bool:
        self.actions.append((action, element_id, value))
        p, e = self.page, element_id
        if e == "n1":
            self.go("home")
        elif e == "n2":
            self.go("visits")
        elif p == "visits" and e == "b1":
            self.go("provider")
        elif p == "provider" and e in ("r1", "r2") and action in ("check", "click"):
            self.provider = "smith" if e == "r1" else "patel"
        elif p == "provider" and e == "next":
            self.go("reason")
        elif p == "reason" and e == "t1" and action == "type":
            self.reason = value or ""
        elif p == "reason" and e == "next":
            if not self.reason:
                return True
            self.go("time")
        elif p == "time" and e in ("s1", "s2"):
            self.slot = "tue" if e == "s1" else "thu"
        elif p == "time" and e == "next":
            self.go("review")
        elif p == "review" and e == "final":
            if self.fail_final:
                return False
            self.booked += 1
            self.go("success")
        elif p == "review" and e == "back":
            self.go("time")
        else:
            return False
        return True


class FakeBrowser:
    """BrowserPort over a FakePortal, checking doc ids and element ids like the extension does."""

    def __init__(self, portal: Optional[FakePortal] = None):
        self.portal = portal or FakePortal()

    async def page_state(self, *, fresh: bool = False) -> PageState:
        if self.portal.gone:
            raise BrowserGone("fake")
        await asyncio.sleep(0)
        return self.portal.state()

    async def act(self, step: Step, doc_id: str) -> ActionResult:
        if self.portal.gone:
            raise BrowserGone("fake")
        state = self.portal.state()
        if doc_id != state.doc_id:
            return ActionResult(success=False, action=step.action, error="stale_element")
        if step.element_id and state.control(step.element_id) is None:
            return ActionResult(success=False, action=step.action, error="not_found")
        before = state.url
        doc_before = self.portal.doc
        ok = self.portal.apply(step.action, step.element_id, step.value)
        after = self.portal.state().url
        return ActionResult(success=ok, action=step.action, url_before=before, url_after=after,
                            page_changed=ok and (after != before or self.portal.doc != doc_before or step.action != "type"),
                            error=None if ok else "invalid_action")

    async def overlay(self, text: Optional[str]) -> None:
        self.portal.overlays.append(text)


class FakeConnection(BrowserConnection):
    """A hub connection whose 'extension' is a FakePortal (for call-controller and voice tests)."""

    def __init__(self, installation_id: str, profile_id: int, portal: Optional[FakePortal] = None):
        super().__init__(None, installation_id, profile_id)
        self.browser = FakeBrowser(portal)

    async def page_state(self, tab_id=None, *, fresh=False) -> PageState:
        if self.closed:
            raise BrowserGone(self.installation_id)
        return await self.browser.page_state(fresh=fresh)

    async def act(self, action, *, tab_id, doc_id=None, element_id=None, value=None) -> ActionResult:
        if self.closed:
            raise BrowserGone(self.installation_id)
        return await self.browser.act(Step(action=action, element_id=element_id, value=value), doc_id)

    async def request(self, action, *, tab_id=None, timeout=20.0, **args) -> dict:
        return {"ok": True, "data": {}}


# ---------------------------------------------------------------- a scripted, sensible "model"

def _ids(page_text: str) -> dict[str, str]:
    """label -> id from the rendered snapshot lines like: [r1] radio "Dr. Alan Smith, MD ..." ..."""
    return {m.group(2): m.group(1) for m in re.finditer(r'\[(\w+)\] \w+ "([^"]*)"', page_text)}


def find(ids: dict[str, str], text: str) -> str:
    return next(i for label, i in ids.items() if text.lower() in label.lower())


def act(*steps: Step, say: str = "", reason: str = "next step") -> Decision:
    return Decision(kind="act", steps=list(steps), say=say, reason=reason, evidence=None)


def S(action: str, element_id: Optional[str] = None, value: Optional[str] = None) -> Step:
    return Step(action=action, element_id=element_id, value=value)


class ScriptedModel:
    """Decides from the page text like a careful agent. Knobs let tests make it misbehave."""

    def __init__(self, *, skip_confirm: bool = False, premature_done: bool = False):
        self.skip_confirm = skip_confirm
        self.premature_done = premature_done
        self.calls = 0
        self.prompts: list[str] = []
        self.gate: Optional[asyncio.Event] = None  # tests can make a decision hang

    async def __call__(self, system: str, messages: list[dict]) -> Decision:
        self.calls += 1
        text = messages[-1]["content"]
        self.prompts.append(text)
        if self.gate is not None:
            await self.gate.wait()
        page = text.split("CURRENT PAGE:", 1)[1].split("END OF PAGE", 1)[0]
        convo = text.split("Conversation so far (most recent last):", 1)[1].split("Steps you have taken", 1)[0]
        ids = _ids(page)
        nudged = "must be confirmed with the caller" in text

        if "Your appointment is scheduled" in page:
            return Decision(kind="done", steps=[], say="You're all set for Thursday at 2 PM. Confirmation RB-41234.",
                            reason="Success page shown", evidence="Your appointment is scheduled")
        if self.premature_done:
            self.premature_done = False
            return Decision(kind="done", steps=[], say="Done!", reason="claiming early",
                            evidence="Your appointment is scheduled")
        if "Welcome back" in page:
            return act(S("click", find(ids, "Visits")), say="Okay, opening your visits.")
        if "# Visits" in page:
            return act(S("click", find(ids, "Schedule an appointment")))
        if "Who would you like to see" in page:
            return act(S("check", find(ids, "Alan Smith")), S("click", find(ids, "Next")))
        if "reason for your visit" in page:
            m = re.search(r"Caller: (.*knee.*)", convo, re.IGNORECASE)
            if not m:
                return Decision(kind="ask_user", steps=[], say="What would you like to see Dr. Smith about?",
                                reason="Reason is required", evidence=None)
            return act(S("type", find(ids, "why you want"), m.group(1).strip()), S("click", find(ids, "Next")))
        if "Choose a time" in page:
            if "thursday" not in convo.lower():
                return Decision(kind="ask_user", steps=[],
                                say="I found Tuesday at 10:30 AM and Thursday at 2 PM. Which would you prefer?",
                                reason="Caller must pick a time", evidence=None)
            return act(S("check", find(ids, "Thursday")), S("click", find(ids, "Next")))
        if "Review your appointment" in page:
            if self.skip_confirm and not nudged:
                return act(S("click", find(ids, "Schedule appointment")), reason="Book it")
            return Decision(kind="confirm", steps=[S("click", find(ids, "Schedule appointment"))],
                            say="I'm ready to schedule with Dr. Alan Smith on Thursday, October 8 at 2 PM for knee pain. "
                                "Would you like me to book it?",
                            reason="Final booking needs consent", evidence=None)
        return Decision(kind="blocked", steps=[], say="I'm not sure what to do here.", reason="unknown page",
                        evidence=None)
