"""Moving between tabs: following a link into a new tab and coming back, switching by handle, tabs that
close or can't be used, telling the agent's own switches from the person's, and confirmations after a switch."""

import asyncio
from typing import Optional

from sqlmodel import select

from app.agent.decision import Decision
from app.agent.policy import page_fingerprint, validate_step
from app.agent.render import tabs_text
from app.agent.runner import BrowserAgent, ConnectionPort
from app.browser.hub import BrowserConnection
from app.browser.protocol import ActionResult, PageElement, PageState, TabInfo
from app.db import session_scope
from app.models import BrowserAction
from fake_portal import S, act

LIBRARY, HOURS, CLINIC, SETTINGS, SEARCH = 1, 3, 2, 9, 4


class FakeWindow(BrowserConnection):
    """A hub connection whose 'extension' is a browser window with several tabs."""

    def __init__(self):
        super().__init__(None, "br_test", 1)
        self.tabs_open = {
            LIBRARY: {"title": "Catalog - Maple County Public Library", "url": "http://library.test/index.html"},
            CLINIC: {"title": "MyRiverbend", "url": "http://clinic.test/"},
            SETTINGS: {"title": "Extensions", "url": ""},
        }
        self.active = LIBRARY
        self.tab = self._tab(LIBRARY)
        self.clicks: list[tuple[int, str]] = []

    def _tab(self, tab_id: int) -> dict:
        return {"tab_id": tab_id, "url": self.tabs_open[tab_id]["url"], "title": self.tabs_open[tab_id]["title"]}

    def person_switches_to(self, tab_id: int) -> None:
        self.active = tab_id
        self.handle({"type": "tab", "tab": self._tab(tab_id)})

    def report_active(self) -> None:
        """The extension's (possibly late) report of which tab is in front."""
        self.handle({"type": "tab", "tab": self._tab(self.active)})

    async def page_state(self, tab_id=None, *, fresh=False) -> PageState:
        tab_id = tab_id or self.active
        tab = self.tabs_open[tab_id]
        elements = [PageElement(role="heading", label=tab["title"], level=1)]
        if tab_id == LIBRARY:
            elements += [PageElement(id="e1", role="link", label="Branch hours"),
                         PageElement(id="e2", role="button", label="Submit request")]
        if tab_id == HOURS:
            elements += [PageElement(role="text", label="Main Library 9 AM to 8 PM")]
        return PageState(doc_id=f"d{tab_id}", tab_id=tab_id, url=tab["url"], title=tab["title"], elements=elements)

    async def act(self, action, *, tab_id, doc_id=None, element_id=None, value=None) -> ActionResult:
        self.clicks.append((tab_id, element_id))
        if action == "new_tab":  # a web search for `value` in a new tab, which comes to the front
            self.tabs_open[SEARCH] = {"title": f"{value} - Google Search", "url": "https://www.google.com/search"}
            self.active = SEARCH
            return ActionResult(success=True, action=action, page_changed=True, new_tab_id=SEARCH,
                                url_after="https://www.google.com/search")
        if tab_id == LIBRARY and element_id == "e1":  # target=_blank: a new tab opens and comes to the front
            self.tabs_open[HOURS] = {"title": "Branch hours - Maple County Public Library",
                                     "url": "http://library.test/hours.html"}
            self.active = HOURS
            return ActionResult(success=True, action=action, page_changed=True, new_tab_id=HOURS)
        return ActionResult(success=True, action=action, page_changed=True)

    async def request(self, action, *, tab_id=None, timeout=20.0, **args) -> dict:
        if action == "list_tabs":
            return {"ok": True, "data": {"tabs": [
                {"tab_id": i, "title": t["title"], "url": t["url"], "index": n, "active": i == self.active,
                 "switchable": bool(t["url"])} for n, (i, t) in enumerate(self.tabs_open.items())]}}
        if action == "switch_tab":
            target = args["target_tab_id"]
            if target not in self.tabs_open:
                return {"ok": False, "error": "no_tab", "detail": "That tab was closed."}
            self.active = target
            return {"ok": True, "data": {"success": True, "page_changed": True, "new_tab_id": target,
                                         "url_after": self.tabs_open[target]["url"]}}
        return {"ok": True, "data": {}}


def run(coro):
    return asyncio.run(coro)


async def opened_hours() -> tuple[FakeWindow, ConnectionPort]:
    """The agent started on the library tab and clicked a link that opened Branch hours in a new tab."""
    window = FakeWindow()
    port = ConnectionPort(window)
    page = await port.page_state()
    result = await port.act(S("click", "e1"), page.doc_id)
    assert result.new_tab_id == HOURS and port.tab_id == HOURS
    return window, port


# ---------------------------------------------------------------- the port


def test_a_link_opens_a_new_tab_and_previous_returns_to_the_first():
    async def scenario():
        window, port = await opened_hours()
        back = await port.act(S("switch_tab", None, "previous"), "d3")
        again = await port.act(S("switch_tab", None, "previous"), "d1")
        return window, port, back, again, await port.page_state()

    window, port, back, again, page = run(scenario())
    assert back.success and back.page_changed
    assert back.detail == ("moved from 'Branch hours - Maple County Public Library' to "
                           "'Catalog - Maple County Public Library'")
    assert again.success and port.tab_id == HOURS  # "previous" again goes back to where it just was
    assert page.tab_id == HOURS and window.active == HOURS


def test_previous_without_an_earlier_tab_is_refused():
    async def scenario():
        port = ConnectionPort(FakeWindow())
        await port.page_state()
        return port, await port.act(S("switch_tab", None, "previous"), "d1")

    port, result = run(scenario())
    assert not result.success and result.error == "no_tab" and "no earlier tab" in result.detail
    assert port.tab_id == LIBRARY


def test_switching_by_handle_reaches_a_tab_the_task_never_touched():
    async def scenario():
        window = FakeWindow()
        port = ConnectionPort(window)
        await port.page_state()
        tabs = await port.tabs()
        clinic = next(t for t in tabs if t.tab_id == CLINIC)
        result = await port.act(S("switch_tab", None, clinic.handle.lower()), "d1")
        return window, port, tabs, result, await port.page_state()

    window, port, tabs, result, page = run(scenario())
    assert [t.handle for t in tabs] == ["T1", "T2", "T3"] and [t.current for t in tabs] == [True, False, False]
    assert result.success and result.detail == "moved from Maple County Public Library to Clinic"
    assert port.tab_id == CLINIC and page.title == "MyRiverbend" and window.active == CLINIC


def test_closed_unusable_unknown_and_current_tabs_are_refused():
    async def scenario():
        window, port = await opened_hours()
        handles = {t.tab_id: t.handle for t in await port.tabs()}
        unusable = await port.act(S("switch_tab", None, handles[SETTINGS]), "d3")
        current = await port.act(S("switch_tab", None, handles[HOURS]), "d3")
        unknown = await port.act(S("switch_tab", None, "T42"), "d3")
        del window.tabs_open[CLINIC]
        closed = await port.act(S("switch_tab", None, handles[CLINIC]), "d3")
        return port, unusable, current, unknown, closed

    port, unusable, current, unknown, closed = run(scenario())
    assert unusable.error == "unsupported_page"
    assert current.error == "invalid_action" and "already" in current.detail
    assert unknown.error == "no_tab" and "not in the tab list" in unknown.detail
    assert closed.error == "no_tab" and closed.detail == "That tab was closed."
    assert port.tab_id == HOURS  # none of them moved the agent


def test_previous_skips_a_tab_that_was_closed():
    async def scenario():
        window, port = await opened_hours()  # trail: library
        clinic = next(t.handle for t in await port.tabs() if t.tab_id == CLINIC)
        await port.act(S("switch_tab", None, clinic), "d3")  # trail: library, hours
        del window.tabs_open[HOURS]
        return port, await port.act(S("switch_tab", None, "previous"), "d2")

    port, result = run(scenario())
    assert result.success and port.tab_id == LIBRARY


def test_the_agents_own_switch_is_not_mistaken_for_the_persons():
    async def scenario():
        window, port = await opened_hours()
        await port.page_state()  # the extension hasn't reported the new active tab yet
        early = (port.tab_id, port.take_tab_switch())
        window.report_active()  # the late report of the tab the agent itself opened
        await port.page_state()
        late = (port.tab_id, port.take_tab_switch())
        window.person_switches_to(CLINIC)
        await port.page_state()
        return early, late, (port.tab_id, port.take_tab_switch())

    early, late, person = run(scenario())
    assert early == (HOURS, None) and late == (HOURS, None)
    assert person == (CLINIC, "MyRiverbend")


def test_tab_list_shows_titles_only_and_scrubs_them():
    async def scenario():
        window = FakeWindow()
        window.tabs_open[CLINIC]["title"] = "Statement for 123-45-6789"
        window.tabs_open[CLINIC]["url"] = "http://clinic.test/statement?token=SECRET"
        port = ConnectionPort(window)
        await port.page_state()
        return await port.tabs()

    text = tabs_text(run(scenario()))
    assert text.splitlines() == [
        'T1 Maple County Public Library: "Catalog - Maple County Public Library" (you are here)',
        'T2 Clinic: "Statement for [redacted SSN]"',
        'T3 "Extensions" (can\'t be used)',
    ]
    assert "SECRET" not in text and "clinic.test" not in text
    assert tabs_text([TabInfo(tab_id=1, title="Only tab", url="http://a.test/", handle="T1", current=True)]) == ""


# ---------------------------------------------------------------- policy


def test_switch_tab_takes_previous_or_a_handle_only():
    page = PageState(doc_id="d1", url="http://library.test/", elements=[])
    assert validate_step(S("switch_tab", None, "previous"), page) is None
    assert validate_step(S("switch_tab", None, "T2"), page) is None
    for bad in (None, "", "http://evil.example/", "17", "the library tab"):
        assert "switch_tab value" in validate_step(S("switch_tab", None, bad), page)


def test_a_confirmation_belongs_to_the_tab_it_was_asked_on():
    button = PageElement(id="b", role="button", label="Submit request")
    here = PageState(doc_id="d1", tab_id=1, url="http://library.test/", elements=[button])
    assert page_fingerprint(here, "b") != page_fingerprint(here.model_copy(update={"tab_id": 2}), "b")


# ---------------------------------------------------------------- the loop


class Scripted:
    def __init__(self, *decisions: Decision):
        self.decisions = list(decisions)
        self.prompts: list[str] = []

    async def __call__(self, system: str, messages: list[dict]) -> Decision:
        content = messages[-1]["content"]
        self.prompts.append(content if isinstance(content, str) else content[-1]["text"])
        return self.decisions.pop(0)


def answer(say: str, evidence: Optional[str] = None) -> Decision:
    return Decision(kind="answer", steps=[], say=say, reason="answering", evidence=evidence)


def make(window: FakeWindow, model: Scripted):
    from app.core import identity

    said: list[str] = []

    async def say(text):
        said.append(text)

    profile = identity.create_profile("+15550001111", display_name="Margaret")
    agent = BrowserAgent(browser=ConnectionPort(window), say=say, profile_id=profile.id, installation_id="br_test",
                         phone="+15550001111", decide=model)
    return agent, said


async def settle(agent):
    for _ in range(200):
        await asyncio.sleep(0)
        if not agent.busy:
            return
    await asyncio.wait_for(asyncio.shield(agent._loop_task), 5)


def actions():
    with session_scope() as s:
        return s.exec(select(BrowserAction).order_by(BrowserAction.id)).all()


def test_agent_opens_a_tab_goes_back_and_says_where_it_is():
    model = Scripted(act(S("click", "e1")), answer("The main library is open 9 to 8.", "Main Library 9 AM to 8 PM"),
                     act(S("switch_tab", None, "previous")), answer("You're back on the catalog."))

    async def scenario():
        window = FakeWindow()
        agent, said = make(window, model)
        await agent.handle("When is the main library open?")
        await settle(agent)
        await agent.handle("Go back to the page I was on.")
        await settle(agent)
        return window, agent, said

    window, agent, said = run(scenario())
    assert window.active == LIBRARY and agent.browser.tab_id == LIBRARY
    assert said == ["The main library is open 9 to 8.", "I'm on your Maple County Public Library tab now.",
                    "You're back on the catalog."]
    assert "Tabs open in this browser window" in model.prompts[0]
    assert 'T4 Maple County Public Library: "Branch hours - Maple County Public Library" (you are here)' in model.prompts[1]
    assert "The person switched" not in "".join(model.prompts)  # its own moves aren't blamed on the person
    switch = next(a for a in actions() if a.kind == "switch_tab")
    assert switch.ok and switch.element_label.startswith("moved from 'Branch hours - Maple County Public Library' to")


def test_a_failed_switch_is_fed_back_to_the_model():
    model = Scripted(act(S("switch_tab", None, "previous")), answer("There's no other page to go back to."))

    async def scenario():
        agent, said = make(FakeWindow(), model)
        await agent.handle("Go back to my other tab")
        await settle(agent)
        return agent, said

    agent, said = run(scenario())
    assert agent.browser.tab_id == LIBRARY and said == ["There's no other page to go back to."]
    assert "switch_tab [None] failed: no_tab (There is no earlier tab" in model.prompts[1]


def test_yes_after_a_tab_change_does_not_press_the_button():
    confirm = Decision(kind="confirm", steps=[S("click", "e2")], say="I'm ready to submit your request. Should I?",
                       reason="final step", evidence=None)
    model = Scripted(confirm, answer("You're on the clinic page now, so I didn't submit anything."))

    async def scenario():
        window = FakeWindow()
        agent, said = make(window, model)
        await agent.handle("Submit my request")
        await settle(agent)
        assert agent.status == "waiting_confirmation"
        window.person_switches_to(CLINIC)
        await agent.handle("Yes")
        await settle(agent)
        return window, said

    window, said = run(scenario())
    assert window.clicks == []  # the stored step never ran, on either tab
    assert any(a.kind == "confirm_stale" for a in actions())
    assert "The person switched to another tab" in model.prompts[1]


# ---------------------------------------------------------------- opening a new tab


def test_new_tab_needs_words_to_search_for():
    page = PageState(doc_id="d1", url="http://library.test/", elements=[])
    assert validate_step(S("new_tab", None, "weather in Springfield"), page) is None
    for bad in (None, "", "   ", "x" * 201):
        assert "new_tab" in validate_step(S("new_tab", None, bad), page)


def test_agent_opens_a_new_tab_on_a_web_search_and_can_go_back():
    model = Scripted(act(S("new_tab", None, "Springfield weather"), say="Okay, opening a new tab."),
                     answer("I've opened a search for Springfield weather."),
                     act(S("switch_tab", None, "previous")), answer("You're back on the library page."))

    async def scenario():
        window = FakeWindow()
        agent, said = make(window, model)
        await agent.handle("Open a new tab and look up the weather in Springfield.")
        await settle(agent)
        opened = (window.active, agent.browser.tab_id)
        await agent.handle("Go back to the library.")
        await settle(agent)
        return window, agent, said, opened

    window, agent, said, opened = run(scenario())
    assert opened == (SEARCH, SEARCH)
    assert "(you are here)" in next(line for line in model.prompts[1].splitlines()
                                    if line[:1] == "T" and line[1:2].isdigit() and "Google Search" in line)
    assert window.active == LIBRARY and agent.browser.tab_id == LIBRARY  # "previous" is the tab it came from
    assert SEARCH in window.tabs_open  # going back closed nothing
    opened_row = next(a for a in actions() if a.kind == "new_tab")
    assert opened_row.ok and opened_row.value == "Springfield weather"
    assert "The person switched" not in "".join(model.prompts)
