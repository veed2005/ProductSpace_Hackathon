"""The browser agent's memory between calls: recall after the PIN, learn only with the caller's yes."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.agent import memory as agent_memory
from app.agent import runner
from app.agent.decision import Decision, Remember
from app.agent.runner import BrowserAgent
from app.browser import pairing
from app.browser.hub import hub
from app.browser.protocol import PageState
from app.core import identity
from app.db import session_scope
from app.main import app
from app.memory import profile as memory
from app.models import Activity, BrowserAction
from fake_portal import FakeBrowser, FakeConnection, ScriptedModel, el

PHONE = "+15550004242"


def run(coro):
    return asyncio.run(coro)


def converse(agent, *lines):
    """Say each line and let the agent finish, all in one event loop (its work runs in background tasks)."""
    async def scenario():
        for line in lines:
            await agent.handle(line)
            await settle(agent)

    run(scenario())


async def settle(agent):
    for _ in range(200):
        await asyncio.sleep(0)
        if not agent.busy:
            return
    if agent._loop_task:
        await asyncio.wait_for(asyncio.shield(agent._loop_task), 5)


def _page():
    return PageState(doc_id="d1", url="https://clinic.test/register", title="Register",
                     elements=[el(None, "heading", "New patient registration"),
                               el("e1", "textbox", "Date of birth"), el("e2", "textbox", "City")])


def _agent(decisions, profile_id=None):
    said, seen = [], []
    page = _page()

    class Static(FakeBrowser):
        async def page_state(self, *, fresh=False):
            return page

    async def model(system, messages):
        seen.append(messages[-1]["content"])
        return decisions.pop(0)

    async def say(text):
        said.append(text)

    pid = profile_id or identity.create_profile(PHONE, display_name="Ana").id
    agent = BrowserAgent(browser=Static(), say=say, profile_id=pid, installation_id="br", phone=PHONE, decide=model)
    return agent, pid, said, seen


def answer(say, remember=None):
    return Decision(kind="answer", steps=[], say=say, reason="x", evidence=None, remember=remember)


def actions(kind):
    with session_scope() as s:
        return [a for a in s.exec(select(BrowserAction).order_by(BrowserAction.id)).all() if a.kind == kind]


# ---------------------------------------------------------------- checking what may be remembered

@pytest.mark.parametrize("key, raw, path, value", [
    ("date_of_birth", "March 14th, 1988", "date_of_birth", "1988-03-14"),
    ("date_of_birth", "03/14/1988", "date_of_birth", "1988-03-14"),
    ("address.city", " Springfield ", "address.city", "Springfield"),
    ("household_size", "3 people", "household_size", 3),
    ("housing_cost", "$1,200", "housing_cost", 1200.0),
    ("full_name", "Ana Maria Lopez", "full_name", "Ana Maria Lopez"),
    ("preferred_language", "Spanish", "preferred_language", "es"),
])
def test_check_converts_values_to_the_stored_shape(key, raw, path, value):
    p = agent_memory.check(key, raw)
    assert isinstance(p, agent_memory.Proposal) and (p.path, p.value) == (path, value)


@pytest.mark.parametrize("key, raw, why", [
    ("ssn_last4", "1234", "sensitive"),
    ("password", "hunter2", "unknown profile key"),
    ("address", "412 Elm St", "several fields"),
    ("household_members[0]", "Leo", "several fields"),
    ("date_of_birth", "sometime in spring", "not a date"),
    ("household_size", "a few", "not a whole number"),
])
def test_check_refuses_what_must_not_be_remembered(key, raw, why):
    problem = agent_memory.check(key, raw)
    assert isinstance(problem, str) and why in problem


def test_spoken_readback_is_natural():
    assert agent_memory.check("date_of_birth", "1988-03-14").spoken == "your date of birth, March 14, 1988"
    assert agent_memory.check("address.zip", "62704").spoken == "your home address ZIP code, 62704"


# ---------------------------------------------------------------- recall

def test_recall_hides_sensitive_facts_and_flags_stale_ones():
    pid = identity.create_profile(PHONE).id
    memory.set_fact(pid, "date_of_birth", "1988-03-14", source_type="form")
    memory.set_fact(pid, "ssn_last4", "6789", source_type="form")
    memory.set_fact(pid, "monthly_income", 1300, source_type="form",
                    confirmed_at=datetime.now(timezone.utc) - timedelta(days=60))
    text = agent_memory.recall(pid)
    assert "date_of_birth: 1988-03-14" in text and "6789" not in text and "ssn" not in text
    income = next(line for line in text.splitlines() if "monthly_income" in line)
    assert "may be out of date" in income
    assert "may be out of date" not in next(line for line in text.splitlines() if "date_of_birth" in line)


def test_saved_facts_reach_the_model_prompt():
    pid = identity.create_profile(PHONE).id
    memory.set_value(pid, "address.city", "Springfield", source_type="conversation")
    agent, _, said, seen = _agent([answer("It's asking for your city.")], profile_id=pid)
    converse(agent, "What does this page want?")
    assert '"city": "Springfield"' in seen[0]
    assert said == ["It's asking for your city."]  # nothing to offer: the caller didn't tell us anything new


def test_nothing_saved_yet_is_said_plainly():
    agent, _, _, seen = _agent([answer("Okay.")])
    converse(agent, "Hi")
    assert "(nothing saved yet)" in seen[0]


# ---------------------------------------------------------------- learn, only with a yes

def test_a_detail_the_caller_gives_is_saved_only_after_they_say_yes():
    agent, pid, said, _ = _agent([answer("I'll use March 14, 1988.", [Remember(key="date_of_birth",
                                                                              value="March 14, 1988")])])

    async def scenario():
        await agent.handle("My birthday is March 14, 1988.")
        await settle(agent)
        assert memory.get_fact(pid, "date_of_birth") is None  # asked, not saved yet
        assert said[-1] == "Would you like me to remember your date of birth, March 14, 1988 for next time?"
        await agent.handle("Yes.")
        await settle(agent)

    run(scenario())
    fact = memory.get_fact(pid, "date_of_birth")
    assert fact.value == "1988-03-14" and fact.source_type == "conversation" and fact.source_ref.startswith("browser_task:")
    assert said[-1] == "Got it. I'll remember that for next time."
    assert actions("remembered")[0].element_label == "date_of_birth"
    with session_scope() as s:
        activity = s.exec(select(Activity).where(Activity.kind == "memory_saved")).one()
    assert "1988" not in activity.description  # values never go in the activity log


def test_no_means_nothing_is_saved():
    agent, pid, said, _ = _agent([answer("Noted.", [Remember(key="address.city", value="Springfield")])])

    async def scenario():
        await agent.handle("I live in Springfield.")
        await settle(agent)
        await agent.handle("No thanks.")
        await settle(agent)

    run(scenario())
    assert memory.get_fact(pid, "address") is None
    assert said[-1] == "Okay, I won't save it."
    assert actions("not_remembered")


def test_moving_on_drops_the_offer_and_handles_the_new_request():
    decisions = [answer("Noted.", [Remember(key="address.city", value="Springfield")]),
                 answer("It's the registration form.")]
    agent, pid, said, seen = _agent(decisions)

    async def scenario():
        await agent.handle("I live in Springfield.")
        await settle(agent)
        await agent.handle("What is this page?")
        await settle(agent)

    run(scenario())
    assert memory.get_fact(pid, "address") is None
    assert said[-1] == "It's the registration form." and len(seen) == 2


def test_repeating_a_saved_detail_refreshes_it_without_asking():
    pid = identity.create_profile(PHONE).id
    memory.set_value(pid, "address.city", "Springfield", source_type="form")
    memory.confirm_fact(pid, "address")
    with session_scope() as s:  # make it stale
        from app.models import ProfileFact

        f = s.exec(select(ProfileFact).where(ProfileFact.key == "address")).one()
        f.confirmed_at = datetime.now(timezone.utc) - timedelta(days=400)
        s.add(f)
        s.commit()
    assert not memory.get_fact(pid, "address").fresh
    agent, _, said, _ = _agent([answer("Thanks.", [Remember(key="address.city", value="springfield")])], profile_id=pid)
    converse(agent, "Yes, still Springfield.")
    assert said == ["Thanks."]  # no "would you like me to remember"
    assert memory.get_fact(pid, "address").fresh


def test_refused_proposals_are_fed_back_to_the_model_and_never_offered():
    ask = Decision(kind="ask_user", steps=[], say="Which city do you live in?", reason="x", evidence=None,
                   remember=[Remember(key="ssn_last4", value="6789")])
    agent, pid, said, seen = _agent([ask, answer("Sure.")])
    converse(agent, "The last four of my social are 6789.", "Springfield.")  # the answer continues the same task
    assert memory.get_fact(pid, "ssn_last4") is None
    assert not any("remember" in s.lower() for s in said)
    assert "Not remembered" in seen[1] and "sensitive" in seen[1]


# ---------------------------------------------------------------- recall needs the PIN

@pytest.fixture
def paired(monkeypatch):
    sent = []
    monkeypatch.setattr(pairing, "deliver_code", lambda phone, code, delivery: sent.append(code))
    started = pairing.start_pairing(PHONE)
    out = pairing.confirm_pairing(started.pairing_id, sent[-1], name="Margaret", pin="4821")
    profile = identity.profiles_for_phone(PHONE)[0]
    memory.set_fact(profile.id, "date_of_birth", "1951-07-04", source_type="form")
    model = ScriptedModel()
    monkeypatch.setattr(runner, "llm_decide", model)
    conn = FakeConnection(out["installation_id"], profile.id)
    hub.register(conn)
    yield model
    hub.unregister(conn)


def _until(ws, needle):
    for _ in range(30):
        msg = ws.receive_json()
        if msg.get("type") in ("text", "end") and needle.lower() in (msg.get("token") or "<end>").lower():
            return
    raise AssertionError(f"never heard {needle!r}")


def test_saved_facts_never_reach_the_model_without_the_pin(paired):
    model = paired
    with TestClient(app) as client, client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CA9"})
        _until(ws, "PIN")
        for wrong in ("1111", "2222", "3333"):
            ws.send_json({"type": "prompt", "voicePrompt": wrong, "last": True})
        _until(ws, "locked")
    assert model.calls == 0


def test_after_the_pin_the_model_sees_saved_facts(paired):
    model = paired
    with TestClient(app) as client, client.websocket_connect("/twilio/voice/relay") as ws:
        ws.send_json({"type": "setup", "from": PHONE, "callSid": "CA10"})
        _until(ws, "PIN")
        ws.send_json({"type": "prompt", "voicePrompt": "4821", "last": True})
        _until(ws, "What would you like help with?")
        ws.send_json({"type": "prompt", "voicePrompt": "I need to make an appointment with Dr. Smith.", "last": True})
        _until(ws, "What would you like to see Dr. Smith about?")
    assert "date_of_birth: 1951-07-04" in model.prompts[0]
