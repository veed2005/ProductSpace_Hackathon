"""The browser-agent loop: asking the caller, confirmation gate, verification, stop, failures."""

import asyncio

import pytest
from sqlmodel import select

from app.agent.decision import Decision
from app.agent.runner import BrowserAgent
from app.db import session_scope
from app.models import BrowserAction, BrowserTask
from fake_portal import FakeBrowser, FakePortal, S, ScriptedModel


def make(model=None, portal=None):
    said: list[str] = []

    async def say(text):
        said.append(text)

    from app.core import identity

    profile = identity.create_profile("+15550001111", display_name="Margaret")
    browser = FakeBrowser(portal or FakePortal())
    agent = BrowserAgent(browser=browser, say=say, profile_id=profile.id, installation_id="br_test", phone="+15550001111",
                         decide=model or ScriptedModel())
    return agent, browser.portal, said


async def settle(agent):
    for _ in range(200):
        await asyncio.sleep(0)
        if not agent.busy:
            return
    task = agent._loop_task
    if task:
        await asyncio.wait_for(asyncio.shield(task), 5)


def actions(kind=None):
    with session_scope() as s:
        rows = s.exec(select(BrowserAction).order_by(BrowserAction.id)).all()
    return [a for a in rows if kind is None or a.kind == kind]


def task_row():
    with session_scope() as s:
        return s.exec(select(BrowserTask)).one()


def run(coro):
    return asyncio.run(coro)


def test_golden_path_asks_confirms_and_verifies():
    async def scenario():
        agent, portal, said = make()
        await agent.handle("I need to make an appointment with Dr. Smith.")
        await settle(agent)
        assert said[-1] == "What would you like to see Dr. Smith about?"
        assert agent.status == "waiting_input"

        await agent.handle("My knee has been hurting.")
        await settle(agent)
        assert portal.reason == "My knee has been hurting."
        assert "Which would you prefer?" in said[-1]

        await agent.handle("Thursday please")
        await settle(agent)
        assert agent.status == "waiting_confirmation"
        assert "Would you like me to book it?" in said[-1]
        assert portal.booked == 0  # nothing final happened yet

        await agent.handle("Yes, please.")
        await settle(agent)
        return agent, portal, said

    agent, portal, said = run(scenario())
    assert portal.booked == 1
    assert agent.status == "completed"
    assert "RB-41234" in said[-1]
    t = task_row()
    assert t.status == "completed" and "Your appointment is scheduled" in t.result
    kinds = [a.kind for a in actions()]
    assert kinds.index("confirm_request") < kinds.index("confirmed") < kinds.index("verified")
    assert ("click", "final", None) in portal.actions


def test_consequential_click_is_gated_even_if_model_skips_confirmation():
    async def scenario():
        agent, portal, said = make(ScriptedModel(skip_confirm=True))
        for line in ("Book with Dr. Smith", "My knee hurts", "Thursday"):
            await agent.handle(line)
            await settle(agent)
        return agent, portal, said

    agent, portal, said = run(scenario())
    assert portal.booked == 0
    assert agent.status == "waiting_confirmation"
    assert said[-1].endswith("?")
    assert agent.pending["step"]["element_id"] == "final"


def test_denial_cancels_and_nothing_is_submitted():
    async def scenario():
        agent, portal, said = make()
        for line in ("Book with Dr. Smith", "My knee hurts", "Thursday", "No, don't."):
            await agent.handle(line)
            await settle(agent)
        return agent, portal, said

    agent, portal, said = run(scenario())
    assert portal.booked == 0
    assert agent.pending is None and agent.status == "waiting_input"
    assert "Nothing was submitted" in said[-1]
    assert actions("declined")


def test_confirmation_runs_exactly_the_stored_action_and_only_if_page_unchanged():
    async def scenario():
        agent, portal, said = make()
        for line in ("Book with Dr. Smith", "My knee hurts", "Thursday"):
            await agent.handle(line)
            await settle(agent)
        portal.reason = "something else"  # the page changed after the caller was asked
        await agent.handle("yes")
        await settle(agent)
        return agent, portal, said

    agent, portal, said = run(scenario())
    assert actions("confirm_stale")
    assert "didn't press anything" in " ".join(said)
    assert ("click", "final", None) not in portal.actions[: portal.actions.index(("click", "final", None))] \
        if ("click", "final", None) in portal.actions else True
    # The model asked again rather than the stale yes being used.
    assert agent.status == "waiting_confirmation"
    assert portal.booked == 0


def test_other_reply_to_confirmation_is_treated_as_new_instruction():
    async def scenario():
        agent, portal, said = make()
        for line in ("Book with Dr. Smith", "My knee hurts", "Thursday"):
            await agent.handle(line)
            await settle(agent)
        await agent.handle("Actually, what time was the other one?")
        await settle(agent)
        return agent, portal

    agent, portal = run(scenario())
    assert portal.booked == 0
    assert any(a.kind == "declined" for a in actions())


def test_cannot_claim_success_without_page_evidence():
    async def scenario():
        agent, portal, said = make(ScriptedModel(premature_done=True))
        await agent.handle("Book with Dr. Smith")
        await settle(agent)
        return agent, said

    agent, said = run(scenario())
    assert "Done!" not in said
    bad = actions("unverified")
    assert bad and "not on the current page" in bad[0].error
    assert agent.status != "completed"


def test_done_requires_the_confirmed_step_to_have_run():
    """The caller said yes but the final click failed; the model then claims success anyway."""
    class Liar(ScriptedModel):
        async def __call__(self, system, messages):
            if "confirmed final step failed" in messages[-1]["content"]:
                self.calls += 1
                return Decision(kind="done", steps=[], say="Booked!", reason="x", evidence="Review your appointment")
            return await super().__call__(system, messages)

    async def scenario():
        portal = FakePortal()
        portal.fail_final = True
        agent, portal, said = make(Liar(), portal)
        for line in ("Book with Dr. Smith", "My knee hurts", "Thursday", "yes"):
            await agent.handle(line)
            await settle(agent)
        return agent, portal, said

    agent, portal, said = run(scenario())
    assert portal.booked == 0
    assert "Booked!" not in said
    assert any("never confirmed and run" in (a.error or "") for a in actions("unverified"))
    assert agent.status != "completed"


def test_stop_halts_immediately():
    async def scenario():
        model = ScriptedModel()
        agent, portal, said = make(model)
        model.gate = asyncio.Event()  # the next decision hangs, like a slow model call
        await agent.handle("Book with Dr. Smith")
        await asyncio.sleep(0.01)
        assert agent.busy
        await agent.handle("stop")
        model.gate.set()
        await asyncio.sleep(0.05)
        return agent, portal, said

    agent, portal, said = run(scenario())
    assert not agent.busy
    assert agent.status == "stopped"
    assert portal.actions == []  # the hung decision never ran
    assert said[-1].startswith("Okay, I stopped")


def test_stale_element_is_rejected_and_fed_back():
    class Stale(ScriptedModel):
        async def __call__(self, system, messages):
            if self.calls == 0:
                self.calls += 1
                self.prompts.append(messages[-1]["content"])
                return Decision(kind="act", steps=[S("click", "e999")], say="", reason="old id", evidence=None)
            return await super().__call__(system, messages)

    async def scenario():
        model = Stale()
        agent, portal, said = make(model)
        await agent.handle("Book with Dr. Smith")
        await settle(agent)
        return model, portal

    model, portal = run(scenario())
    assert actions("rejected")[0].error.startswith("'e999' is not an element on the current page")
    assert "is not an element on the current page" in model.prompts[1]
    assert portal.page == "reason"  # recovered and kept going


def test_browser_disconnect_is_reported_to_caller():
    async def scenario():
        agent, portal, said = make()
        portal.gone = True
        await agent.handle("Book with Dr. Smith")
        await settle(agent)
        return said

    said = run(scenario())
    assert "lost the connection to your browser" in said[-1]


def test_caller_speech_while_working_reaches_the_model():
    async def scenario():
        model = ScriptedModel()
        agent, portal, said = make(model)
        model.gate = asyncio.Event()
        await agent.handle("Book with Dr. Smith")
        await asyncio.sleep(0.01)
        await agent.handle("it's for my knee")
        model.gate.set()
        await settle(agent)
        return model

    model = run(scenario())
    assert any("While you were working the caller said: \"it's for my knee\"" in p for p in model.prompts)


@pytest.mark.parametrize("reply", ["yes", "Yes, book it.", "sí", "go ahead"])
def test_affirmative_replies_book(reply):
    async def scenario():
        agent, portal, said = make()
        for line in ("Book with Dr. Smith", "My knee hurts", "Thursday", reply):
            await agent.handle(line)
            await settle(agent)
        return portal

    assert run(scenario()).booked == 1


def test_evidence_may_span_page_elements_but_must_be_real():
    from app.agent.runner import _norm

    page = _norm("Your appointment is scheduled Confirmation number RB-41234")
    assert _norm("Your appointment is scheduled. Confirmation number RB-41234.") in page
    assert _norm("“Your appointment is scheduled”") in page
    assert _norm("Your appointment is booked") not in page


def test_evidence_copied_with_snapshot_markup_still_matches():
    from app.agent.runner import _norm

    page = _norm("Renewal complete The Overstory is now due Mon, Oct 26.")
    assert _norm("# Renewal complete\nThe Overstory is now due Mon, Oct 26.") in page
    assert _norm("[e5] Renewal complete") in page


def test_confirm_with_several_steps_runs_the_lead_and_confirms_the_last():
    class Bundler(ScriptedModel):
        async def __call__(self, system, messages):
            page = messages[-1]["content"].split("CURRENT PAGE:", 1)[1]
            if "Choose a time" in page and "thursday" in messages[-1]["content"].lower().split("current page:")[0]:
                self.calls += 1
                return Decision(kind="confirm", steps=[S("check", "s2"), S("click", "next")],
                                say="Shall I continue?", reason="bundled", evidence=None)
            return await super().__call__(system, messages)

    async def scenario():
        agent, portal, said = make(Bundler())
        for line in ("Book with Dr. Smith", "My knee hurts", "Thursday"):
            await agent.handle(line)
            await settle(agent)
        return agent, portal

    agent, portal = run(scenario())
    assert portal.slot == "thu"  # the harmless lead step ran
    assert agent.pending["step"] == {"action": "click", "element_id": "next", "value": None}  # only the last waits
