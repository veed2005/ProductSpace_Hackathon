"""Deterministic pieces of phone form filling: parsing, conversion, verification policy, masking."""

import pytest

from app.contracts import FormField
from app.formcall import language, privacy, values, verify


@pytest.mark.parametrize("text, letters", [
    ("D-O-U-B-E-K", "doubek"), ("D O U B E K.", "doubek"), ("D as in David, O, U, B, E, K", "doubek"),
    ("de, o, u, be, e, ka", "doubek"), ("double s", "ss"), ("My name is Evan", None), ("Doubek", None),
])
def test_spelling(text, letters):
    assert values.parse_spelling(text) == letters


@pytest.mark.parametrize("text, email", [
    ("evan dot doubek at gmail dot com", "evan.doubek@gmail.com"),
    ("e v a n punto doubek arroba gmail punto com", "evan.doubek@gmail.com"),
    ("It's evan underscore d at yahoo dot com", "evan_d@yahoo.com"),
    ("my email is not here", None),
])
def test_spoken_email(text, email):
    assert values.parse_email(text) == email


def test_email_is_spelled_back():
    assert values.say_email("evan.doubek@gmail.com") == "E, V, A, N, dot, D, O, U, B, E, K, at gmail dot com"
    assert "arroba gmail punto com" in values.say_email("evan.doubek@gmail.com", "es")


@pytest.mark.parametrize("text, amount, period", [
    ("300 a week", 300, "week"), ("trescientos dólares por semana", 300, "week"), ("$1,200 each month", 1200, "month"),
    ("900 every two weeks", 900, "biweekly"), ("mil doscientos al mes", 1200, "month"),
    ("three hundred fifty a week", 350, "week"),
])
def test_amounts_and_periods(text, amount, period):
    assert values.parse_number(text) == amount and values.parse_period(text) == period


def test_conversion_is_fixed_arithmetic():
    assert values.convert_amount(300, "week", "month") == 1299  # 300 x 4.33
    assert values.convert_amount(900, "biweekly", "month") == 1953  # 900 x 2.17
    assert values.convert_amount(24000, "year", "month") == 2000
    assert values.convert_amount(15, "hour", "month") is None  # needs hours: ask instead of guessing


@pytest.mark.parametrize("text, iso", [
    ("March 14th, 1988", "1988-03-14"), ("14 de marzo de 1988", "1988-03-14"), ("22 de julio del 79", "1979-07-22"),
    ("03/14/1988", "1988-03-14"), ("catorce de marzo de 1988", "1988-03-14"), ("sometime in spring", None),
])
def test_dates(text, iso):
    assert values.parse_date(text) == iso


def test_phone_from_digit_words():
    assert values.parse_phone("two one seven, five five five, zero one zero four") == "+12175550104"


def test_names_must_come_from_the_callers_words():
    assert values.name_from_caller("Evan Doubek", "Me llamo Evan Doubek.")
    assert not values.name_from_caller("John", "Me llamo Juan")


@pytest.mark.parametrize("text, lang, sure", [
    ("Quiero solicitar beneficios de SNAP.", "es", True), ("I need help applying for SNAP", "en", True),
    ("¿Cómo?", "es", True), ("412 Elm Street", None, False),
])
def test_language_detection(text, lang, sure):
    got, confident = language.detect(text)
    assert confident == sure and (got == lang or not sure)


def test_explicit_language_switch():
    assert language.requested_switch("Can you explain that part in English?") == "en"
    assert language.requested_switch("Háblame en español") == "es"
    assert language.requested_switch("I live on Spanish Oak Lane") is None


def F(**kw):
    base = dict(id="x", label="X", type="text", question_hint="?")
    return FormField(**{**base, **kw})


def test_verification_policy():
    name = F(id="applicant_name", label="Full name", profile_key="full_name")
    assert verify.decide(name, channel="voice", provenance="caller", confidence=0.95, spelling_uncertain=False,
                         transformed=False) == "read_back"
    assert verify.decide(name, channel="voice", provenance="caller", confidence=0.95, spelling_uncertain=True,
                         transformed=False) == "spell"
    assert verify.decide(name, channel="sms", provenance="caller", confidence=0.5, spelling_uncertain=True,
                         transformed=False) == "none"  # typed: what they typed is what goes on the form
    assert verify.decide(name, channel="voice", provenance="memory", confidence=0, spelling_uncertain=True,
                         transformed=False) == "none"
    money = F(type="money")
    assert verify.decide(money, channel="sms", provenance="caller", confidence=1, spelling_uncertain=False,
                         transformed=True) == "read_back"  # converted: always heard back
    count = F(type="number")
    assert verify.decide(count, channel="voice", provenance="caller", confidence=0.9, spelling_uncertain=False,
                         transformed=False) == "none"  # ordinary and confident: no repetition
    ssn = F(type="ssn_last4", sensitive=True)
    assert verify.decide(ssn, channel="voice", provenance="caller", confidence=1, spelling_uncertain=False,
                         transformed=False) == "double_entry"


def test_masking_policy():
    ssn = F(type="ssn_last4", sensitive=True)
    assert privacy.display(ssn, "6789") == "***-**-6789"
    acct = F(label="Account number", sensitive=True)
    assert privacy.display(acct, "123454321") == "****4321"
    pin = F(id="account_pin", label="PIN")
    assert privacy.is_secret(pin) and privacy.display(pin, "4821") == "[not shown]"
