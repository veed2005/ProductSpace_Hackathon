"""Filling a form from saved details: Formline asks first, and types them only after a yes."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.agent import memory as agent_memory
from app.agent.decision import Decision
from app.agent.memory import Saved
from app.agent.runner import BrowserAgent
from app.browser.protocol import ActionResult, PageElement, PageState
from app.core import identity
from app.db import session_scope
from app.memory import profile as memory
from app.models import BrowserAction
from fake_portal import FakeBrowser, S

PHONE = "+15550005555"

FACTS = [Saved("name", True, ["Ana", "Lopez"]), Saved("date_of_birth", True, ["1988-03-14"]),
         Saved("phone", True, ["+12175550104"]), Saved("address", True, ["412 Elm St", "Springfield", "IL", "62704"]),
         Saved("household_size", True, ["3"])]


# ---------------------------------------------------------------- which saved fact a value comes from


@pytest.mark.parametrize("value, key", [
    ("Ana", "name"), ("Ana Lopez", "name"), ("LOPEZ", "name"),
    ("03-14-1988", "date_of_birth"), ("March 14, 1988", "date_of_birth"), ("14/03/1988", "date_of_birth"),
    ("(217) 555-0104", "phone"), ("217-555-0104", "phone"),
    ("412 Elm St, Springfield, IL 62704", "address"), ("IL", "address"), ("3", "household_size"),
])
def test_a_typed_value_is_traced_to_its_saved_fact(value, key):
    assert agent_memory.source_of(value, FACTS).key == key


@pytest.mark.parametrize("value", ["My knee hurts", "Dr. Smith", "Chicago", "04-15-1988", "555-0199", ""])
def test_other_values_are_not_from_memory(value):
    assert agent_memory.source_of(value, FACTS) is None


def test_short_values_are_not_traced_unless_the_model_said_they_are_saved():
    assert agent_memory.source_of("IL", FACTS, strict=True) is None
    assert agent_memory.source_of("3", FACTS, strict=True) is None
    assert agent_memory.source_of("Ana", FACTS, strict=True).key == "name"


def test_the_offer_names_what_is_saved_and_never_the_values():
    text = agent_memory.fill_offer_text(["name", "date_of_birth", "address"], "en")
    assert text == "I already have your name, date of birth and address saved. Would you like me to fill that in for you?"
    assert "fecha de nacimiento" in agent_memory.fill_offer_text(["date_of_birth"], "es")


# ---------------------------------------------------------------- in the agent loop


def box(id, label, value=None):
    return PageElement(id=id, role="textbox", label=label, value=value)


class FormBrowser(FakeBrowser):
    def __init__(self):
        super().__init__()
        self.elements = {e.id: e for e in [box("e1", "First name"), box("e2", "Last name"),
                                            box("e3", "Birthdate (MM-DD-YYYY)"), box("e4", "Reason for visit")]}
        self.typed: list[tuple[str, str]] = []

    async def page_state(self, *, fresh=False):
        await asyncio.sleep(0)
        return PageState(doc_id="d1", url="https://clinic.test/register", title="Register",
                         elements=list(self.elements.values()))

    async def act(self, step, doc_id):
        self.typed.append((step.element_id, step.value))
        self.elements[step.element_id] = self.elements[step.element_id].model_copy(update={"value": step.value})
        return ActionResult(success=True, action=step.action, page_changed=False)


def offer(*steps):
    return Decision(kind="offer_fill", steps=list(steps), say="", reason="saved details fit this form", evidence=None)


def ask(say, field_id=None):
    return Decision(kind="ask_user", steps=[], say=say, reason="needs a detail", evidence=None, field_id=field_id)


def act(*steps):
    return Decision(kind="act", steps=list(steps), say="", reason="filling in", evidence=None)


ALL_SAVED = (S("type", "e1", "Ana"), S("type", "e2", "Lopez"), S("type", "e3", "1988-03-14"))
OFFER = "I already have your name and date of birth saved. Would you like me to fill that in for you?"


def setup(decisions, *, stale_dob=False):
    pid = identity.create_profile(PHONE, display_name="Ana").id
    memory.set_value(pid, "full_name", "Ana Lopez", source_type="conversation")
    memory.set_fact(pid, "date_of_birth", "1988-03-14", source_type="form")
    if stale_dob:  # date of birth never goes stale, so use a fact that does
        memory.set_fact(pid, "phone", "+12175550104", source_type="form",
                        confirmed_at=datetime.now(timezone.utc) - timedelta(days=400))
    said, seen = [], []
    browser = FormBrowser()

    async def model(system, messages):
        seen.append(messages[-1]["content"])
        return decisions.pop(0)

    async def say(text):
        said.append(text)

    agent = BrowserAgent(browser=browser, say=say, profile_id=pid, installation_id="br", phone=PHONE, decide=model)
    return agent, browser, said, seen


def converse(agent, *lines):
    async def scenario():
        for line in lines:
            await agent.handle(line)
            for _ in range(200):
                await asyncio.sleep(0)
                if not agent.busy:
                    break
            else:
                await asyncio.wait_for(asyncio.shield(agent._loop_task), 5)

    asyncio.run(scenario())


def kinds():
    with session_scope() as s:
        return [a.kind for a in s.exec(select(BrowserAction).order_by(BrowserAction.id)).all()]


def test_saved_details_are_offered_and_typed_only_after_a_yes():
    agent, browser, said, seen = setup([offer(*ALL_SAVED), ask("What's the reason for your visit?", "e4")])
    converse(agent, "Fill out this registration form for me.")
    assert said == [OFFER] and browser.typed == []  # asked first; nothing typed, no value read aloud
    assert "NOT been asked" in seen[0]

    converse(agent, "Yes please.")
    assert browser.typed == [("e1", "Ana"), ("e2", "Lopez"), ("e3", "03-14-1988")]  # date in the field's format
    assert said[1:] == ["Okay, I filled that in.", "What's the reason for your visit?"]
    assert "has agreed to use these" in seen[1] and "3 field(s) were filled in" in seen[1]
    assert "autofill_offer" in kinds() and "autofill_accepted" in kinds()


def test_no_means_nothing_is_filled_and_the_details_are_hidden_from_the_model():
    agent, browser, said, seen = setup([offer(*ALL_SAVED), ask("What's your first name?", "e1"),
                                        act(S("type", "e1", "Ana")), ask("And your last name?", "e2")])
    converse(agent, "Fill out this form.", "No, I'll tell you myself.")
    assert browser.typed == []
    assert said[1:] == ["No problem. I'll ask you instead.", "What's your first name?"]
    assert "Lopez" not in seen[1] and "hidden" in seen[1]

    converse(agent, "Ana")  # what the caller says themselves is typed, saved or not
    assert browser.typed == [("e1", "Ana")] and said[-1] == "And your last name?"
    assert "autofill_declined" in kinds()


def test_typing_a_saved_detail_without_asking_becomes_the_offer():
    agent, browser, said, _ = setup([act(S("type", "e1", "Ana"), S("type", "e2", "Lopez")),
                                     ask("What's your date of birth?", "e3")])
    converse(agent, "Fill out this form.")
    assert browser.typed == []
    assert said == ["I already have your name saved. Would you like me to fill that in for you?"]

    converse(agent, "Sure")
    assert browser.typed == [("e1", "Ana"), ("e2", "Lopez")]


def test_what_the_caller_just_said_is_not_treated_as_a_saved_detail():
    agent, browser, said, _ = setup([ask("What's your last name?", "e2"), act(S("type", "e2", "Lopez")),
                                     ask("What's the reason for your visit?", "e4")])
    converse(agent, "Fill out this form.", "Lopez")
    assert browser.typed == [("e2", "Lopez")]
    assert said == ["What's your last name?", "What's the reason for your visit?"]


def test_only_saved_up_to_date_details_are_offered():
    agent, browser, said, seen = setup(
        [offer(S("type", "e1", "Ana"), S("type", "e3", "(217) 555-0104"), S("type", "e4", "Knee pain"),
               S("click", "e9")),
         ask("What's the reason for your visit?", "e4")], stale_dob=True)
    converse(agent, "Fill out this form.", "yes")
    assert said[0] == "I already have your name saved. Would you like me to fill that in for you?"
    assert browser.typed == [("e1", "Ana")]  # not the stale phone number, not a made-up reason, never a click


def test_an_offer_with_nothing_saved_in_it_goes_back_to_the_model():
    agent, browser, said, seen = setup([offer(S("type", "e4", "Knee pain")),
                                        ask("What's the reason for your visit?", "e4")])
    converse(agent, "Fill out this form.")
    assert said == ["What's the reason for your visit?"] and browser.typed == []
    assert "isn't one of the caller's saved details" in seen[1]


def test_stop_during_the_offer_stops_and_fills_nothing():
    agent, browser, said, _ = setup([offer(*ALL_SAVED)])
    converse(agent, "Fill out this form.", "Stop.")
    assert browser.typed == [] and agent.status == "stopped" and agent.pending is None


def test_another_reply_to_the_offer_fills_nothing_and_goes_to_the_model():
    agent, browser, said, seen = setup([offer(*ALL_SAVED), ask("What's your first name?", "e1")])
    converse(agent, "Fill out this form.", "Actually my name changed, it's Ana Ruiz now.")
    assert browser.typed == [] and said[-1] == "What's your first name?"
    assert "they said something else" in seen[1]


def test_a_yes_covers_the_rest_of_the_request_but_not_the_next_one():
    agent, browser, said, _ = setup([offer(S("type", "e1", "Ana")), act(S("type", "e2", "Lopez")),
                                     Decision(kind="answer", steps=[], say="Your name is filled in.", reason="x",
                                              evidence=None),
                                     act(S("type", "e3", "1988-03-14"))])
    converse(agent, "Fill out this form.", "yes")
    assert browser.typed == [("e1", "Ana"), ("e2", "Lopez")]  # agreed once: no second question for the last name

    converse(agent, "Now fill out the birthdate on this other form.")  # a new request: asked again
    assert said[-1] == "I already have your date of birth saved. Would you like me to fill that in for you?"
    assert ("e3", "03-14-1988") not in browser.typed
