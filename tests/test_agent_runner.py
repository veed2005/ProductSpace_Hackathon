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
            text = messages[-1]["content"]
            page = text.split("CURRENT PAGE:", 1)[1].split("END OF PAGE", 1)[0]
            if "Choose a time" in page and "thursday" in text.split("END OF PAGE", 1)[1].lower():
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


def test_vision_fallback_is_opt_in_and_only_for_sparse_pages(monkeypatch):
    from app.browser.protocol import PageElement, PageState
    from app.config import get_settings

    sparse = PageState(doc_id="d1", url="https://app.test/", title="Canvas app",
                       elements=[PageElement(id="e1", role="button", label="")])

    class Shooter(FakeBrowser):
        async def page_state(self, *, fresh=False):
            return sparse

        async def screenshot(self):
            return "aGVsbG8="

    def model_seeing(messages_box):
        async def model(system, messages):
            messages_box.append(messages[-1]["content"])
            return Decision(kind="blocked", steps=[], say="I can't tell what's on this page.", reason="sparse",
                            evidence=None)
        return model

    async def scenario():
        seen = []
        agent = BrowserAgent(browser=Shooter(), say=lambda t: asyncio.sleep(0), profile_id=0, installation_id="x",
                             phone="+15550001111", decide=model_seeing(seen))
        await agent.handle("Click the blue thing")
        await settle(agent)
        return seen

    assert all(isinstance(c, str) for c in run(scenario()))  # off by default: text only
    monkeypatch.setenv("FORMLINE_VISION_FALLBACK", "true")
    get_settings.cache_clear()
    content = run(scenario())[0]
    assert content[0]["type"] == "image" and content[0]["source"]["data"] == "aGVsbG8="


# ---------------------------------------------------------------- answering questions from the page

def _lease_page():
    from pathlib import Path

    from app.browser import pdf
    from app.browser.protocol import PageState

    data = (Path(__file__).resolve().parent.parent / "demo_sites" / "testbench" / "lease.pdf").read_bytes()
    doc = pdf.read(data, "https://files.example/lease.pdf")
    return PageState(doc_id="d1", url="https://files.example/lease.pdf", title=doc.title, document=doc,
                     content_type="application/pdf", site_name=pdf.friendly_name(doc))


def _qa_agent(decisions, page):
    said, seen = [], []

    class Static(FakeBrowser):
        async def page_state(self, *, fresh=False):
            return page

    async def model(system, messages):
        seen.append(messages[-1]["content"])
        return decisions.pop(0)

    async def say(text):
        said.append(text)

    from app.core import identity

    profile = identity.create_profile("+15550003333", display_name="Margaret")
    agent = BrowserAgent(browser=Static(), say=say, profile_id=profile.id, installation_id="br",
                         phone="+15550003333", decide=model)
    return agent, said, seen


def test_answers_are_grounded_in_the_document_and_the_conversation_continues():
    decisions = [Decision(kind="answer", steps=[], say="Yes, a dog under 40 pounds, with a $300 deposit.",
                          reason="pets", evidence="Tenant may keep up to two cats or one dog weighing under 40 pounds")]
    agent, said, seen = _qa_agent(decisions, _lease_page())

    async def scenario():
        await agent.handle("Can I have a dog?")
        await settle(agent)

    run(scenario())
    assert said[-1].startswith("Yes, a dog")
    assert agent.status == "waiting_input"  # not completed: follow-up questions continue the conversation
    assert "[Page 4 of 6]" in seen[0] and "Dana Whitfield" in seen[0]  # the whole document was in the prompt
    assert actions("answer")[0].value.startswith("Tenant may keep")


def test_an_answer_quoting_text_that_is_not_there_is_rejected():
    decisions = [
        Decision(kind="answer", steps=[], say="Yes, any pets are fine.", reason="x", evidence="All pets are welcome"),
        Decision(kind="answer", steps=[], say="Up to two cats or one small dog.", reason="x",
                 evidence="up to two cats or one dog weighing under 40 pounds"),
    ]
    agent, said, seen = _qa_agent(decisions, _lease_page())

    async def scenario():
        await agent.handle("Can I have pets?")
        await settle(agent)

    run(scenario())
    assert "Yes, any pets are fine." not in said and said[-1].startswith("Up to two cats")
    assert "not on the current page or in its document" in seen[1]


def test_title_quoted_as_shown_counts_as_evidence():
    from app.agent.runner import BrowserAgent as A
    from app.browser.protocol import PageState

    page = PageState(doc_id="d", url="https://letterboxd.com/linky/", title="\u200eLinky’s profile • Letterboxd")
    assert A._unsupported("Title: \u200eLinky’s profile • Letterboxd", page) is None
    assert A._unsupported("Linky’s watchlist", page)


def test_a_new_request_does_not_inherit_details_from_the_last_one():
    agent, portal, said = make()

    async def scenario():
        await agent.handle("Book with Dr. Smith")
        await settle(agent)
        agent.status = "completed"
        await agent.handle("Look up a movie")
        await settle(agent)
        return agent.decide

    model = run(scenario())
    assert "This is a NEW request" in model.prompts[-1]


def test_stitched_quotes_count_only_if_every_piece_is_real():
    from app.agent.runner import BrowserAgent as A

    page = _lease_page()
    real = ('This Residential Lease Agreement ... between Riverside Property Management LLC ("Landlord") ... '
            "The Lease begins on October 1, 2026 and ends on September 30, 2027. ... Monthly rent is $1,450.00")
    assert A._unsupported(real, page) is None
    assert A._unsupported("[Page 4 of 6]\n11. PETS. Tenant may keep up to two cats", page) is None
    fake = "Monthly rent is $1,450.00 ... Tenant may keep any number of pets"
    assert A._unsupported(fake, page)
    sentences = ('Margaret Ellis ("Tenant"). The Lease begins on October 1, 2026 and ends on September 30, 2027. '
                 "Monthly rent is $1,450.00. This Lease, with Addendum A (Parking Rules) and Addendum B (Pet Policy), "
                 "is the entire agreement between the parties.")
    assert A._unsupported(sentences, page) is None
    assert A._unsupported("Monthly rent is $1,450.00. The landlord allows any pet you like.", page)


def test_the_same_click_is_not_repeated_a_third_time():
    from app.browser.protocol import ActionResult, PageElement, PageState

    page = PageState(doc_id="d", url="https://films.test/", title="Films",
                     elements=[PageElement(id="m", role="link", label="More..."),
                               PageElement(id="s", role="searchbox", label="Search")])

    class Static(FakeBrowser):
        async def page_state(self, *, fresh=False):
            return page

        async def act(self, step, doc_id):
            return ActionResult(success=True, action=step.action, page_changed=True)

    seen = []

    async def model(system, messages):
        seen.append(messages[-1]["content"])
        if any("twice" in p for p in seen):
            return Decision(kind="ask_user", steps=[], say="Which movie?", reason="x", evidence=None)
        return Decision(kind="act", steps=[S("click", "m")], say="", reason="more", evidence=None)

    from app.core import identity

    profile = identity.create_profile("+15550004444", display_name="Evan")

    async def say(text):
        pass

    agent = BrowserAgent(browser=Static(), say=say, profile_id=profile.id, installation_id="b",
                         phone="+15550004444", decide=model)

    async def scenario():
        await agent.handle("Look up a movie")
        await settle(agent)

    run(scenario())
    assert [a.kind for a in actions()].count("click") == 2
    assert any("already did click on 'more...' twice" in (a.error or "") for a in actions("rejected"))


def test_next_on_each_new_wizard_step_is_not_a_repeat():
    # The golden path clicks "Next" on four different steps; none of them may be refused.
    agent, portal, said = make()

    async def scenario():
        for line in ("Book with Dr. Smith", "My knee hurts", "Thursday", "yes"):
            await agent.handle(line)
            await settle(agent)

    run(scenario())
    assert portal.booked == 1 and not actions("rejected")


def test_agent_follows_the_person_to_another_tab():
    from app.agent.runner import ConnectionPort
    from app.browser.hub import BrowserConnection
    from app.browser.protocol import PageState

    class Conn(BrowserConnection):
        async def page_state(self, tab_id=None, *, fresh=False):
            return PageState(doc_id=f"d{tab_id}", tab_id=tab_id or 1, url=f"https://site.test/{tab_id}", title=str(tab_id))

    async def scenario():
        conn = Conn(None, "b", 1)
        conn.tab = {"tab_id": 1, "title": "Lease.pdf"}
        port = ConnectionPort(conn)
        first = await port.page_state()
        conn.tab = {"tab_id": 2, "title": "Letterboxd"}  # the person clicked another tab
        second = await port.page_state()
        return first, second, port.take_tab_switch(), port.take_tab_switch()

    first, second, switched, again = asyncio.run(scenario())
    assert first.tab_id == 1 and second.tab_id == 2
    assert switched == "Letterboxd" and again is None
