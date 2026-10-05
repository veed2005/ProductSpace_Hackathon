"""Form fields: one question at a time, no format hints read aloud, answers typed in the field's format."""

import asyncio

import pytest

from app.agent import fields
from app.agent.decision import Decision
from app.agent.runner import BrowserAgent
from app.browser.protocol import ActionResult, PageElement, PageState
from fake_portal import FakeBrowser, S


def box(id, label, value=None, placeholder=None):
    return PageElement(id=id, role="textbox", label=label, value=value, placeholder=placeholder)


def form(*elements):
    return PageState(doc_id="d1", url="https://clinic.test/register", title="Register", elements=list(elements))


# ---------------------------------------------------------------- format hints


@pytest.mark.parametrize("text", ["Birthdate (MM-DD-YYYY)", "fecha (DD/MM/AAAA)", "Datum TT.MM.JJJJ", "date m/d/yy",
                                  "Phone (###) ###-####", "SSN XXX-XX-XXXX"])
def test_format_hints_are_found(text):
    assert fields.hints_in(text)


@pytest.mark.parametrize("text", ["What's your date of birth?", "Thursday at 2 PM, room 3-B", "Call 555-0104",
                                  "Visit A/B testing", "Max 40 lbs"])
def test_ordinary_words_are_not_hints(text):
    assert fields.hints_in(text) == []


@pytest.mark.parametrize("said, spoken", [
    ("What's your birthdate (MM-DD-YYYY)?", "What's your birthdate?"),
    ("Please tell me your birthdate in the format MM/DD/YYYY.", "Please tell me your birthdate."),
    ("What's your phone number, like (XXX) XXX-XXXX?", "What's your phone number?"),
])
def test_hints_are_taken_out_of_what_is_said(said, spoken):
    assert fields.strip_hints(said) == spoken


# ---------------------------------------------------------------- one question at a time


def test_a_question_about_several_empty_fields_goes_back():
    page = form(box("e1", "First name"), box("e2", "Last name"), box("e3", "Birthdate (MM-DD-YYYY)"))
    problem = fields.question_problem("What's your first name, last name, and birthdate?", page)
    assert "ONE field" in problem and "first name" in problem and "birthdate" in problem


def test_a_question_about_one_field_is_fine():
    page = form(box("e1", "First name", value="Ana"), box("e2", "Last name"), box("e3", "Birthdate"))
    assert fields.question_problem("Thanks, Ana. What's your last name?", page) is None  # first name is filled


def test_two_questions_in_one_go_back():
    assert "ONE field" in fields.question_problem("What's your name? And where do you live?", form())


def test_reading_a_format_hint_goes_back():
    page = form(box("e3", "Birthdate (MM-DD-YYYY)"))
    assert "format hint" in fields.question_problem("What's your birthdate, MM-DD-YYYY?", page)


# ---------------------------------------------------------------- typing in the field's format


@pytest.mark.parametrize("value", ["March 14th, 1988", "march 14 1988", "14 March 1988", "14 de marzo de 1988",
                                   "1988-03-14", "3/14/1988", "03-14-88", "03141988", "Mar. 14, 1988"])
def test_dates_are_typed_in_the_fields_format(value):
    assert fields.fit_value(value, box("e3", "Birthdate (MM-DD-YYYY)")) == "03-14-1988"


@pytest.mark.parametrize("label, placeholder, expected", [
    ("Date of birth", "DD/MM/YYYY", "14/03/1988"),
    ("Fecha de nacimiento (DD/MM/AAAA)", None, "14/03/1988"),
    ("Birth date (YYYY-MM-DD)", None, "1988-03-14"),
    ("DOB (M/D/YY)", None, "3/14/88"),
])
def test_other_formats(label, placeholder, expected):
    assert fields.fit_value("March 14, 1988", box("e3", label, placeholder=placeholder)) == expected


def test_numbers_follow_the_fields_own_day_month_order():
    dmy = box("e3", "Date of birth (DD/MM/YYYY)")
    assert fields.fit_value("04/03/1988", dmy) == "04/03/1988"  # 4 March, as the caller filling this form meant
    assert fields.fit_value("03/14/1988", dmy) == "14/03/1988"  # no 14th month: read the other way round


@pytest.mark.parametrize("value, el", [
    ("Ana Lopez", box("e3", "Birthdate (MM-DD-YYYY)")),  # not a date
    ("02/30/1988", box("e3", "Birthdate (MM-DD-YYYY)")),  # no such day
    ("March 14, 1988", box("e3", "Birthdate")),  # the field shows no format
    ("March 14, 1988", PageElement(id="e4", role="button", label="Pick a date (MM-DD-YYYY)")),
])
def test_anything_else_is_typed_as_it_is(value, el):
    assert fields.fit_value(value, el) == value


# ---------------------------------------------------------------- in the agent loop


class FormBrowser(FakeBrowser):
    def __init__(self, *elements):
        super().__init__()
        self.elements = {e.id: e for e in elements}
        self.typed: list[tuple[str, str]] = []

    async def page_state(self, *, fresh=False):
        await asyncio.sleep(0)
        return form(*self.elements.values())

    async def act(self, step, doc_id):
        self.typed.append((step.element_id, step.value))
        self.elements[step.element_id] = self.elements[step.element_id].model_copy(update={"value": step.value})
        return ActionResult(success=True, action=step.action, page_changed=False)


def agent_for(browser, decisions):
    said, seen = [], []

    async def model(system, messages):
        seen.append(messages[-1]["content"])
        return decisions.pop(0)

    async def say(text):
        said.append(text)

    from app.core import identity

    profile = identity.create_profile("+15550004444", display_name="Ana")
    agent = BrowserAgent(browser=browser, say=say, profile_id=profile.id, installation_id="br",
                         phone="+15550004444", decide=model)
    return agent, said, seen


async def settle(agent):
    for _ in range(200):
        await asyncio.sleep(0)
        if not agent.busy:
            return
    if agent._loop_task:
        await asyncio.wait_for(asyncio.shield(agent._loop_task), 5)


def ask(say):
    return Decision(kind="ask_user", steps=[], say=say, reason="needs a detail", evidence=None)


def test_agent_asks_for_one_field_at_a_time_and_types_dates_in_the_fields_format():
    browser = FormBrowser(box("e1", "Full name"), box("e2", "Birthdate (MM-DD-YYYY)"))
    agent, said, seen = agent_for(browser, [
        ask("What's your full name and your birthdate?"),  # several at once: sent back
        ask("What's your full name?"),
        Decision(kind="act", steps=[S("type", "e1", "Ana Lopez")], say="", reason="name", evidence=None),
        ask("Thanks. What's your birthdate (MM-DD-YYYY)?"),  # format hint: sent back
        ask("And what's your date of birth?"),
        Decision(kind="act", steps=[S("type", "e2", "March 14th, 1988")], say="", reason="dob", evidence=None),
        ask("Is there anything else?"),
    ])

    async def scenario():
        await agent.handle("Fill out this registration form for me.")
        await settle(agent)
        await agent.handle("Ana Lopez")
        await settle(agent)
        await agent.handle("March fourteenth, 1988")
        await settle(agent)

    asyncio.run(scenario())
    assert said == ["What's your full name?", "And what's your date of birth?", "Is there anything else?"]
    assert "ONE field" in seen[1] and "format hint" in seen[4]
    assert browser.typed == [("e1", "Ana Lopez"), ("e2", "03-14-1988")]
    assert "'03-14-1988'" in seen[6]  # the model sees what was really typed


def test_a_question_that_still_reads_a_hint_is_asked_without_it():
    browser = FormBrowser(box("e2", "Birthdate (MM-DD-YYYY)"))
    agent, said, _ = agent_for(browser, [ask("What's your birthdate (MM-DD-YYYY)?"),
                                         ask("What's your birthdate (MM-DD-YYYY)?")])

    async def scenario():
        await agent.handle("Fill out this form.")
        await settle(agent)

    asyncio.run(scenario())
    assert said == ["What's your birthdate?"]  # never a dead end, never the letters


def test_each_answer_is_typed_in_before_the_next_question():
    browser = FormBrowser(box("e1", "First name"), box("e2", "Last name"))
    agent, said, seen = agent_for(browser, [
        Decision(kind="ask_user", steps=[], say="What's your first name?", reason="x", evidence=None, field_id="e1"),
        # the model tries to save the answer up for later: sent back
        Decision(kind="ask_user", steps=[], say="And your last name?", reason="x", evidence=None, field_id="e2"),
        # a question inside an act's status isn't spoken; ask_user asks it once
        Decision(kind="act", steps=[S("type", "e1", "Ana")], say="Got it. And your last name?", reason="x",
                 evidence=None),
        Decision(kind="ask_user", steps=[], say="And your last name?", reason="x", evidence=None, field_id="e2"),
    ])

    async def scenario():
        await agent.handle("Fill out this form.")
        await settle(agent)
        await agent.handle("Ana")
        await settle(agent)

    asyncio.run(scenario())
    assert browser.typed == [("e1", "Ana")]
    assert said == ["What's your first name?", "And your last name?"]
    assert "they replied 'Ana'" in seen[2] and "Type their answer into it now" in seen[2]
    assert agent.pending["field"] == "e2"


def test_asking_the_same_field_again_is_allowed():
    """The caller didn't give an answer ("why do they need that?"): asking for the same field again is fine."""
    browser = FormBrowser(box("e3", "Birthdate (MM-DD-YYYY)"))
    agent, said, _ = agent_for(browser, [
        Decision(kind="ask_user", steps=[], say="What's your date of birth?", reason="x", evidence=None, field_id="e3"),
        Decision(kind="ask_user", steps=[], say="The clinic uses it to find your records. What's your date of birth?",
                 reason="x", evidence=None, field_id="e3"),
    ])

    async def scenario():
        await agent.handle("Fill out this form.")
        await settle(agent)
        await agent.handle("Why do they need that?")
        await settle(agent)

    asyncio.run(scenario())
    assert said[-1].startswith("The clinic uses it")


def test_a_question_carrying_the_last_answer_types_it_first():
    browser = FormBrowser(box("e1", "First name"), box("e2", "Last name"),
                          PageElement(id="e9", role="button", label="Submit registration"))
    agent, said, _ = agent_for(browser, [
        Decision(kind="ask_user", steps=[], say="What's your first name?", reason="x", evidence=None, field_id="e1"),
        Decision(kind="ask_user", steps=[S("type", "e1", "Ana"), S("click", "e9")], say="And your last name?",
                 reason="x", evidence=None, field_id="e2"),
    ])

    async def scenario():
        await agent.handle("Fill out this form.")
        await settle(agent)
        await agent.handle("Ana")
        await settle(agent)

    asyncio.run(scenario())
    assert browser.typed == [("e1", "Ana")]  # typed; the click is never run from a question
    assert said[-1] == "And your last name?"


def test_a_sites_search_box_is_not_a_form_field():
    page = form(PageElement(id="e1", role="searchbox", label="Search"), box("e2", "Name"))
    assert fields.question_problem("What name should I search for?", page) is None


@pytest.mark.parametrize("value", ["+12175550104", "2175550104", "217-555-0104", "217.555.0104", "(217) 555-0104"])
def test_phone_numbers_are_typed_in_the_pattern_the_field_shows(value):
    assert fields.fit_value(value, box("e4", "Phone number", placeholder="(###) ###-####")) == "(217) 555-0104"
    assert fields.fit_value(value, box("e4", "Phone (XXX-XXX-XXXX)")) == "217-555-0104"


@pytest.mark.parametrize("value", ["555-0104", "call my sister", "+44 20 7946 0958"])
def test_numbers_that_do_not_fit_the_pattern_are_typed_as_they_are(value):
    assert fields.fit_value(value, box("e4", "Phone number", placeholder="(###) ###-####")) == value
