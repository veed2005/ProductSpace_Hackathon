"""Demo rehearsal: fill all three Illinois forms for Lane D's seeded persona, from memory.

Mirrors what the brain does at completion: memory-mapped fields come from the profile (via
memory.get_value / format_value), everything else from the persona's spoken answers, then
fill_pdf + verify_pdf. Run with FORMLINE_KEEP_DEMO_PDFS=1 to keep the PDFs in data/demo/filled/
for eyeballing before a rehearsal.
"""

import os
from pathlib import Path

import pytest

from app.config import ROOT_DIR
from app.contracts import FormField, FormSchema
from app.engines import form_library
from app.memory import profile as memory
from app.pdf.fill import fill_pdf
from app.pdf.format import _yes_no, pdf_value
from app.pdf.verify import read_fields, verify_pdf

# What Maria Garcia says for questions her profile can't answer (fake persona, fake numbers).
MARIA_SAYS = {
    "il_snap": {
        "has_mailing_address": "no", "speaks_english": "no", "language": "Spanish",
        "has_household_members": "yes", "member_name": "Garcia, Luis", "member_buys_food": "yes",
        "elderly_in_household": "no", "employed": "yes", "worker_name": "Maria Garcia",
        "stopped_working": "no", "rents_home": "yes", "electric_bill": "95",
    },
    "il_medicaid": {
        "has_mailing_address": "no", "wants_spanish": "yes", "applicant_citizen": "yes",
        "applicant_wants_coverage": "yes", "has_household_members": "yes", "member_name": "Garcia, Luis",
        "member_wants_coverage": "yes", "has_second_member": "yes", "member2_name": "Garcia, Sofia",
        "member2_wants_coverage": "yes", "pregnant": "no", "other_insurance": "no", "has_wages": "yes",
        "gets_child_support": "no", "pays_child_care": "yes", "child_care_cost": "200",
    },
    "il_school_meals": {
        "gets_snap_or_tanf": "yes", "child_name": "Luis Garcia", "child_grade": "5", "child_foster": "no",
        "has_second_child": "no", "homeless": "no",
    },
}


@pytest.fixture
def maria() -> int:
    from app.dashboard.demo import seed_demo

    return seed_demo()["returning"]["profile_id"]


def fill_for_persona(form_id: str, profile_id: int, says: dict[str, str], out_dir: Path):
    """Fill form_id for a persona. Returns (out_path, verification, answer sources by field id)."""
    schema: FormSchema = form_library.load_schema(form_id)
    facts = memory.get_facts(profile_id)
    answers: dict = {}
    sources: dict[str, str] = {}
    for f in schema.fields:
        if f.profile_key and (v := memory.get_value(profile_id, f.profile_key, facts)) and v.value not in (None, ""):
            answers[f.id], sources[f.id] = v.value, "memory"
        elif f.id in says:
            answers[f.id], sources[f.id] = says[f.id], "asked"

    def applies(f) -> bool:
        return not f.condition or _yes_no(answers.get(f.condition.field)) == str(f.condition.equals)

    values = {f.pdf_field: pdf_value(f, answers[f.id])
              for f in schema.fields if f.pdf_field and f.id in answers and applies(f)}
    out = fill_pdf(form_library.pdf_path(form_id), values, out_dir / f"{form_id}_maria.pdf")
    return out, verify_pdf(out, values, schema), sources


def _out_dir(tmp_path: Path) -> Path:
    if os.environ.get("FORMLINE_KEEP_DEMO_PDFS"):
        d = ROOT_DIR / "data" / "demo" / "filled"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return tmp_path


@pytest.mark.parametrize("form_id", ["il_snap", "il_medicaid", "il_school_meals"])
def test_maria_fills_every_demo_form_and_it_verifies(maria, form_id, tmp_path):
    out, result, sources = fill_for_persona(form_id, maria, MARIA_SAYS[form_id], _out_dir(tmp_path))
    assert result.ok, result
    schema = form_library.load_schema(form_id)
    unanswered = [f.id for f in schema.fields if f.required and f.id not in sources
                  and (not f.condition or sources.get(f.condition.field) == "memory"
                       or MARIA_SAYS[form_id].get(f.condition.field) == f.condition.equals)]
    assert unanswered == [], unanswered


def test_school_meals_case_number_fits_its_box(maria):
    """The box holds 9 characters (one per cell). The seed used to say "IL-SNAP-448120", which
    printed cut off; the schema now rejects anything longer so the brain re-asks."""
    import re

    seeded = memory.get_value(maria, "case_numbers.snap").value
    rule = next(f.validation for f in form_library.load_schema("il_school_meals").fields if f.id == "snap_case_number")
    assert re.match(rule, seeded)
    assert not re.match(rule, "IL-SNAP-448120")


def test_second_and_third_forms_come_mostly_from_memory(maria, tmp_path):
    """The headline demo metric: once Maria's profile exists, most answers need no question."""
    for form_id, at_least in (("il_medicaid", 12), ("il_school_meals", 7)):
        _, _, sources = fill_for_persona(form_id, maria, MARIA_SAYS[form_id], tmp_path)
        from_memory = sum(1 for s in sources.values() if s == "memory")
        assert from_memory >= at_least, (form_id, from_memory)


def test_memory_values_are_written_the_way_paper_forms_expect(maria, tmp_path):
    out, _, _ = fill_for_persona("il_medicaid", maria, MARIA_SAYS["il_medicaid"], tmp_path)
    by_id = {f.id: f.pdf_field for f in form_library.load_schema("il_medicaid").fields}
    actual = read_fields(out)
    assert actual[by_id["date_of_birth"]] == "03/14/1988"  # memory: 1988-03-14
    assert actual[by_id["phone"]].startswith("(") and "+" not in actual[by_id["phone"]]  # memory: E.164
    assert actual[by_id["address"]] == "412 Elm St, Apt 2B, Springfield, IL 62704"
    assert actual[by_id["applicant_name"]] == "Maria Elena Garcia"


def _field(type_, **kw) -> FormField:
    return FormField(id="x", label="X", type=type_, question_hint="?", pdf_field="x", **kw)


@pytest.mark.parametrize("type_, answer, expected", [
    ("date", "1988-03-14", "03/14/1988"),
    ("date", "March 1988", "March 1988"),
    ("phone", "+12175550104", "(217) 555-0104"),
    ("phone", "217.555.0104", "(217) 555-0104"),
    ("money", 1300.0, "1300"),
    ("money", "$1,240.50", "1240.50"),
    ("ssn_last4", "XXX-XX-4821", "4821"),
    ("ssn_last4", "48", ""),
    ("number", 3, "3"),
])
def test_pdf_value_formats(type_, answer, expected):
    assert pdf_value(_field(type_), answer) == expected


def test_pdf_value_yes_no_uses_the_forms_states():
    f = _field("yes_no", pdf_values={"yes": "0", "no": "1"})
    assert [pdf_value(f, a) for a in (True, False, "yes", "Sí", "no")] == ["0", "1", "0", "0", "1"]


def test_pdf_value_objects_use_memory_formatting():
    f = _field("address", profile_key="address")
    assert pdf_value(f, {"street": "88 Oak Ave", "apt": "", "city": "Springfield", "state": "IL", "zip": "62702"}) \
        == "88 Oak Ave, Springfield, IL 62702"
