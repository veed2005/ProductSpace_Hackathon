"""The real demo forms: library files, the hand-written SNAP schema, and alias matching."""

from pathlib import Path

import pytest

from app.contracts import FormMeta
from app.engines import form_library
from app.pdf.fields import button_state, list_fields
from app.pdf.fill import fill_pdf
from app.pdf.verify import read_fields, verify_pdf

DEMO_FORMS = ["il_snap", "il_medicaid", "il_school_meals"]

# Fake persona answers for the SNAP schema, keyed by field id (what Lane A collects).
ROSA = {
    "applicant_name": "Rosa Martinez", "date_of_birth": "03/14/1988",
    "address": "1420 W Maple St, Apt 3, Springfield, IL 62704", "phone": "(217) 555-0104",
    "speaks_english": "no", "language": "Spanish", "household_size": "3",
    "has_household_members": "yes", "member_name": "Martinez, Leo", "member_relationship": "Son",
    "member_date_of_birth": "04/02/2016", "member_buys_food": "yes", "elderly_in_household": "no",
    "disability_in_household": "no", "employed": "yes", "worker_name": "Rosa Martinez",
    "employer": "Corner Market", "gross_pay": "650.00", "hours_per_week": "30", "pay_frequency": "biweekly",
    "stopped_working": "no", "rents_home": "yes", "rent_amount": "850.00", "electric_bill": "60.00",
}


def _pdf_values(answers: dict[str, str]) -> dict[str, str]:
    """What Lane A does at completion: map answers to {pdf_field: value} via pdf_values."""
    schema = form_library.load_schema("il_snap")
    by_id = {f.id: f for f in schema.fields}
    return {by_id[k].pdf_field: by_id[k].pdf_values.get(v, v) for k, v in answers.items() if by_id[k].pdf_field}


@pytest.mark.parametrize("form_id", DEMO_FORMS)
def test_demo_form_has_fillable_pdf_and_meta(form_id):
    meta = FormMeta.model_validate_json((form_library.form_dir(form_id) / "meta.json").read_text(encoding="utf-8"))
    assert meta.form_id == form_id and meta.aliases and meta.agency
    assert len(list_fields(form_library.pdf_path(form_id))) > 50, "not a fillable AcroForm"


def test_snap_schema_has_no_review_problems():
    from app.dashboard.forms_api import schema_problems

    assert schema_problems("il_snap") == []
    assert 15 <= len(form_library.load_schema("il_snap").fields) <= 30


def test_snap_yes_no_values_are_real_states_with_yes_on_the_left():
    """Every Yes/No on this form is laid out '[ ] Yes [ ] No', but the on-state names are
    not consistent ("0" is Yes on most rows and No on others), so check by position."""
    fields = {f.name: f for f in list_fields(form_library.pdf_path("il_snap"))}
    for sf in form_library.load_schema("il_snap").fields:
        if sf.type != "yes_no" or not sf.pdf_field:
            continue
        pf = fields[sf.pdf_field]
        assert button_state(pf, sf.pdf_values["yes"]) not in (None, "Off"), sf.id
        assert button_state(pf, sf.pdf_values["no"]) is not None, sf.id
        if pf.type == "radio":
            x = {w.on_state: w.rect[0] for w in pf.widgets}
            assert x[sf.pdf_values["yes"]] < x[sf.pdf_values["no"]], sf.id


def test_snap_fills_and_verifies_with_a_persona(tmp_path: Path):
    values = _pdf_values(ROSA)
    out = fill_pdf(form_library.pdf_path("il_snap"), values, tmp_path / "snap.pdf")
    result = verify_pdf(out, values, form_library.load_schema("il_snap"))
    assert result.ok, result
    actual = read_fields(out)
    snap = {f.id: f for f in form_library.load_schema("il_snap").fields}
    assert actual[snap["speaks_english"].pdf_field] == snap["speaks_english"].pdf_values["no"]
    assert actual[snap["rents_home"].pdf_field] != "Off"


def test_snap_verify_flags_missing_conditional_answers(tmp_path: Path):
    answers = {k: v for k, v in ROSA.items() if k not in ("employer", "rent_amount")}
    values = _pdf_values(answers)
    out = fill_pdf(form_library.pdf_path("il_snap"), values, tmp_path / "snap.pdf")
    result = verify_pdf(out, values, form_library.load_schema("il_snap"))
    assert result.missing_required == ["employer", "rent_amount"]


def test_radio_states_with_escaped_names(tmp_path: Path):
    """The school meals form stores "Hispanic/Latino" as the PDF name Hispanic#2FLatino."""
    pdf = form_library.pdf_path("il_school_meals")
    out = fill_pdf(pdf, {"Ethnic Identity": "Hispanic/Latino"}, tmp_path / "meals.pdf")
    assert read_fields(out)["Ethnic Identity"] == "Hispanic#2FLatino"
    assert verify_pdf(out, {"Ethnic Identity": "Hispanic/Latino"}).ok
    assert not verify_pdf(out, {"Ethnic Identity": "Not Hispanic/Latino"}).ok


@pytest.mark.parametrize("text, form_id", [
    ("I need food stamps", "il_snap"),
    ("can you help me with SNAP?", "il_snap"),
    ("my EBT card", "il_snap"),
    ("Quiero cupones de alimentos", "il_snap"),
    ("I got a letter about my medical debt", None),  # "ebt" inside "debt" must not match
    ("snapshot of my bill", None),
    ("benefits application", "sample_benefits"),
])
def test_match_form_whole_words(text, form_id):
    assert form_library.match_form(text) == form_id


def test_match_form_ignores_forms_without_a_schema():
    # School meals has a PDF and meta but its schema comes in C4, so it isn't offered yet.
    assert form_library.match_form("free lunch for my kids") is None


def test_match_form_folds_accents_and_case(monkeypatch):
    monkeypatch.setattr(form_library, "list_forms", lambda: [
        FormMeta(form_id="x", name="X", aliases=["almuerzo gratis"])])
    assert form_library.match_form("¿Puedo pedir ALMUERZO  GRATÍS?") == "x"
