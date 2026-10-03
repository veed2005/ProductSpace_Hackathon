"""Deterministic guards: step validation, consequential-action detection, replies, page sanitizing."""

import pytest

from app.agent.policy import classify_reply, is_consequential, is_stop, page_fingerprint, validate_step
from app.agent.render import page_text, site_name
from app.browser.protocol import PageElement, PageState
from app.browser.sanitize import sanitize_page
from fake_portal import S


def page(*elements, title="Portal", url="https://portal.test/#/x", doc="d1"):
    return PageState(doc_id=doc, url=url, title=title, elements=list(elements))


def E(id_, role, label, **kw):
    return PageElement(id=id_, role=role, label=label, **kw)


H = lambda text: PageElement(role="heading", label=text, level=1)  # noqa: E731


# ---------------------------------------------------------------- validate_step

def test_unknown_or_stale_element_is_rejected():
    p = page(E("e1", "button", "Next"))
    assert "not an element on the current page" in validate_step(S("click", "e9"), p)
    assert validate_step(S("click", "e1"), p) is None


@pytest.mark.parametrize("step,element,problem", [
    (S("type", "e1", "hi"), E("e1", "checkbox", "Agree"), "not a text field"),
    (S("type", "e1", "hunter2"), E("e1", "password", "Password", sensitive=True), "private field"),
    (S("type", "e1", "4111"), E("e1", "textbox", "Card number", sensitive=True), "private field"),
    (S("select", "e1", "x"), E("e1", "button", "Menu"), "custom lists"),
    (S("check", "e1"), E("e1", "textbox", "Name"), "not a checkbox"),
    (S("uncheck", "e1"), E("e1", "radio", "Tuesday"), "can't be unchecked"),
    (S("click", "e1"), E("e1", "button", "Next", enabled=False), "disabled"),
    (S("type", "e1", ""), E("e1", "textbox", "Reason"), "needs a value"),
])
def test_action_must_fit_the_element(step, element, problem):
    assert problem in validate_step(step, page(element))


def test_the_model_cannot_type_web_addresses():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        S("navigate", None, "https://evil.example/steal")
    p = page(url="https://portal.test/a")
    assert validate_step(S("scroll", None, "sideways"), p)
    assert validate_step(S("scroll", None, "down"), p) is None


# ---------------------------------------------------------------- consequential

@pytest.mark.parametrize("label,heading,expected", [
    ("Schedule an appointment", "Visits", False),          # starts a flow
    ("Schedule appointment", "Review your appointment", True),  # finishes it
    ("Next", "Review your appointment", False),
    ("Back", "Review your appointment", False),
    ("Send", "New message", True),
    ("Send a message", "Messages", False),
    ("Submit", "Application", True),
    ("Pay now", "Billing", True),
    ("Cancel appointment", "Visits", True),
    ("Cancel", "Choose a time", False),
    ("Delete", "Saved cards", True),
    ("Renew selected items", "My account", False),
    ("Confirm renewal", "Renew items", True),
    ("Show more times", "Choose a time", False),
])
def test_consequential_buttons(label, heading, expected):
    p = page(H(heading), E("b", "button", label))
    assert is_consequential(S("click", "b"), p) is expected


def test_typing_and_choosing_are_drafts_but_agreeing_is_not():
    p = page(E("t", "textbox", "Reason"), E("c", "checkbox", "I agree to the terms of use"),
             E("r", "checkbox", "Text me a reminder"))
    assert not is_consequential(S("type", "t", "knee"), p)
    assert is_consequential(S("check", "c"), p)
    assert not is_consequential(S("check", "r"), p)


def test_fingerprint_changes_with_values_and_summary():
    a = page(H("Review"), PageElement(role="text", label="Thursday 2 PM"), E("b", "button", "Book"))
    b = page(H("Review"), PageElement(role="text", label="Tuesday 10 AM"), E("b", "button", "Book"))
    c = page(H("Review"), PageElement(role="text", label="Thursday 2 PM"), E("b", "button", "Book"), doc="d2")
    assert page_fingerprint(a, "b") == page_fingerprint(a.model_copy(), "b")
    assert page_fingerprint(a, "b") != page_fingerprint(b, "b")
    assert page_fingerprint(a, "b") != page_fingerprint(c, "b")


# ---------------------------------------------------------------- replies

@pytest.mark.parametrize("text,verdict", [
    ("Yes.", "yes"), ("yeah go ahead", "yes"), ("Sí, por favor", "yes"), ("okay", "yes"), ("Please do.", "yes"),
    ("No.", "no"), ("not yet", "no"), ("wait", "no"), ("No, cancel", "no"),
    ("Yes but make it Tuesday", "other"), ("Actually, Tuesday", "other"), ("", "other"), ("Tuesday", "other"),
])
def test_classify_reply(text, verdict):
    assert classify_reply(text) == verdict


@pytest.mark.parametrize("text,stop", [("Stop!", True), ("stop it", True), ("Para.", True), ("hold on", True),
                                       ("Don't stop", False), ("Thursday", False), ("bus stop", False)])
def test_is_stop(text, stop):
    assert is_stop(text) is stop


# ---------------------------------------------------------------- sanitizing and rendering

def test_sanitize_withholds_secrets_even_if_the_extension_missed_them():
    raw = page(E("p", "password", "Password", value="hunter2"),
               E("c", "textbox", "Card number", value="4111 1111 1111 1111"),
               E("n", "textbox", "Name", value="Margaret"),
               PageElement(role="text", label="SSN 123-45-6789 on file; card 4111-1111-1111-1111"),
               url="https://portal.test/pay?session=abc123#review")
    clean = sanitize_page(raw)
    blob = clean.model_dump_json()
    for secret in ("hunter2", "4111", "123-45-6789", "abc123"):
        assert secret not in blob
    assert clean.control("n").value == "Margaret"
    assert clean.control("c").sensitive and clean.control("c").value is None
    assert clean.url == "https://portal.test/pay#review"


def test_page_text_and_friendly_site_name():
    p = page(H("Choose a time"), E("s1", "radio", "Thursday at 2 PM", checked=False, group="Times"),
             E("n", "button", "Next", enabled=False), E("pw", "password", "Password", sensitive=True),
             title="Appointments - MyChart", url="https://mychart.example.org/x")
    text = page_text(p)
    assert '[s1] radio "Thursday at 2 PM" group="Times" unchecked' in text
    assert '[n] button "Next" (disabled)' in text
    assert "never type here" in text
    assert site_name(p) == "MyChart"
    assert site_name(page(title="Home", url="https://www.cityofspringfield.gov/")) == "Cityofspringfield"
    assert site_name(page(title="", url="http://localhost:8000/")) == "a web page"
    assert site_name(page(title="Just a moment...", url="https://letterboxd.com/")) == "Letterboxd"
    assert site_name(page(title="\u200eLinky’s profile • Letterboxd", url="https://letterboxd.com/x/")) == "Letterboxd"
    assert site_name(page(title="Reelbox • Social film discovery", url="http://127.0.0.1:8000/x")) == "Reelbox"
    assert site_name(page(title="Your appointment is scheduled - MyRiverbend", url="http://localhost/")) == "MyRiverbend"


# ---------------------------------------------------------------- the search action

def test_search_needs_words_and_only_a_text_box_if_one_is_named():
    p = page(E("q", "searchbox", "Search"), E("b", "button", "Go"), E("pw", "password", "Password", sensitive=True))
    assert validate_step(S("search", None, "Arrival"), p) is None  # the site search, found by the extension
    assert validate_step(S("search", "q", "Arrival"), p) is None
    assert "needs the words" in validate_step(S("search", None, " "), p)
    assert "not a search box" in validate_step(S("search", "b", "Arrival"), p)
    assert "not a search box" in validate_step(S("search", "pw", "Arrival"), p)
    assert "not an element" in validate_step(S("search", "e99", "Arrival"), p)
    assert not is_consequential(S("search", None, "Arrival"), p)  # searching changes nothing


# ---------------------------------------------------------------- what changed since the last look

def test_new_items_are_marked_and_disappearances_counted():
    before = page(H("Films"), E("e1", "button", "search (icon)"), E("e2", "link", "More..."),
                  PageElement(role="text", label="(Note: this page has a search box that is hidden right now)"))
    after = page(H("Films"), E("e1", "button", "search (icon)"), E("e2", "link", "More..."),
                 E("e9", "searchbox", "Search…"))
    text = page_text(after, previous=before)
    assert "1 new item(s), marked with +" in text and "1 item(s) disappeared" in text
    assert '+ [e9] searchbox "Search…"' in text
    assert "+ [e1]" not in text and "+ # Films" not in text


def test_a_new_document_or_a_whole_new_page_is_said_plainly():
    before = page(H("Films"), E("e1", "link", "A"), doc="d1")
    assert "different page" in page_text(page(H("Results"), E("e1", "link", "B"), doc="d2"), previous=before)
    swapped = page(H("Results"), E("e7", "link", "X"), E("e8", "link", "Y"), E("e9", "link", "Z"), doc="d1")
    assert "Most of the page changed" in page_text(swapped, previous=before)
    assert "+ " not in page_text(swapped, previous=before).split("\n\n", 1)[1]
    assert "Nothing changed" in page_text(before, previous=before)


def test_source_files_have_no_stray_control_characters():
    """A mangled escape (a literal backspace where \b was meant) silently breaks a regex; it happened twice."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for folder in ("app", "extension", "scripts"):
        for p in (root / folder).rglob("*"):
            if p.suffix in (".py", ".js") and p.is_file():
                text = p.read_text(encoding="utf-8")
                assert not any(ord(c) < 32 and c not in "\n\r\t" for c in text), p


@pytest.mark.parametrize("text,verdict", [
    ("Oui.", "yes"), ("Ja, bitte.", "yes"), ("Sim", "yes"), ("हाँ", "yes"), ("जी हाँ", "yes"), ("Да.", "yes"),
    ("はい", "yes"), ("はい、お願いします", "yes"), ("haan", "yes"),
    ("Non.", "no"), ("Nein, danke", "no"), ("Não", "no"), ("नहीं", "no"), ("Нет", "no"), ("いいえ", "no"), ("Nee", "no"),
    ("Oui mais mardi", "other"), ("Ja, aber am Dienstag bitte", "other"), ("हाँ लेकिन मंगलवार को", "other"),
    ("Oui je voudrais aussi changer la raison de la visite", "other"),  # long: a new instruction, not consent
    ("Mardi", "other"), ("मंगलवार", "other"),
])
def test_yes_and_no_in_the_other_call_languages(text, verdict):
    assert classify_reply(text, "hi") == verdict  # any call language beyond English and Spanish


def test_foreign_yes_words_dont_count_on_an_english_or_spanish_call():
    assert classify_reply("Sim card, please") == "other"
    assert classify_reply("Sim", "es") == "other"
    assert classify_reply("Yes.", "hi") == "yes"  # English and Spanish answers still work on any call


@pytest.mark.parametrize("text,stop", [
    ("रुको", True), ("Stopp!", True), ("Arrête", True), ("Стоп", True), ("ストップ", True), ("Basta.", True),
    ("Halt", True), ("रविवार", False), ("Bonjour", False),
])
def test_stop_in_the_other_call_languages(text, stop):
    assert is_stop(text) is stop
