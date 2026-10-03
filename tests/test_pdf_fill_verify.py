"""fill_pdf / verify_pdf against a fixture PDF with every field type real forms use."""

import subprocess
import sys
from pathlib import Path

import pymupdf as fitz
import pytest

from app.contracts import FieldCondition, FormField, FormSchema
from app.pdf.fields import list_fields
from app.pdf.fill import fill_pdf
from app.pdf.verify import MISSING, read_fields, verify_pdf

ROOT = Path(__file__).resolve().parent.parent


def _add(page, name, ftype, rect, **kw):
    w = fitz.Widget()
    w.field_name, w.field_type, w.rect = name, ftype, fitz.Rect(rect)
    for k, v in kw.items():
        setattr(w, k, v)
    page.add_widget(w)


def _rename_on_state(doc, xref, new):
    on = doc.xref_get_key(xref, "AP/N/Yes")[1]
    off = doc.xref_get_key(xref, "AP/N/Off")[1]
    doc.xref_set_key(xref, "AP/N", f"<</{new} {on}/Off {off}>>")


@pytest.fixture
def form_pdf(tmp_path: Path) -> Path:
    """One page with text, max-length, multiline, auto-size, checkbox (on-state "On"), a real
    radio group (Rent/Own/Other under one parent), a combo box and a list box."""
    doc = fitz.open()
    page = doc.new_page()
    T = fitz.PDF_WIDGET_TYPE_TEXT
    _add(page, "name", T, (50, 50, 200, 68), text_fontsize=10)
    _add(page, "zip", T, (50, 80, 200, 98), text_fontsize=10, text_maxlen=5)
    _add(page, "notes", T, (50, 110, 200, 140), text_fontsize=10, field_flags=1 << 12)
    _add(page, "auto", T, (50, 150, 200, 168), text_fontsize=0)
    _add(page, "employed", fitz.PDF_WIDGET_TYPE_CHECKBOX, (50, 180, 64, 194))
    _add(page, "employer", T, (50, 200, 300, 218), text_fontsize=10)
    _add(page, "color", fitz.PDF_WIDGET_TYPE_COMBOBOX, (50, 230, 200, 248), choice_values=["Red", "Green"])
    _add(page, "size", fitz.PDF_WIDGET_TYPE_LISTBOX, (50, 260, 200, 300), choice_values=["S", "M", "L"])
    for i in range(3):
        _add(page, f"radio{i}", fitz.PDF_WIDGET_TYPE_RADIOBUTTON, (50 + 60 * i, 320, 64 + 60 * i, 334),
             field_value=False)

    by_name = {w.field_name: w.xref for w in page.widgets()}
    _rename_on_state(doc, by_name["employed"], "On")
    kids = [by_name[f"radio{i}"] for i in range(3)]
    parent = doc.get_new_xref()
    doc.update_object(parent, f"<</FT/Btn/Ff 49152/T(housing)/V/Off/Kids[{' '.join(f'{k} 0 R' for k in kids)}]>>")
    for xref, state in zip(kids, ["Rent", "Own", "Other"]):
        _rename_on_state(doc, xref, state)
        for key in ("T", "FT", "Ff", "V"):
            doc.xref_set_key(xref, key, "null")
        doc.xref_set_key(xref, "Parent", f"{parent} 0 R")
    others = [x for n, x in by_name.items() if not n.startswith("radio")]
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm/Fields", f"[{' '.join(f'{x} 0 R' for x in others)} {parent} 0 R]")

    path = tmp_path / "form.pdf"
    doc.save(path)
    doc.close()
    return path


GOOD = {"name": "Ana Lopez", "zip": "60601", "notes": "Works nights.", "auto": "Short",
        "employed": "Yes", "employer": "Corner Market", "color": "Green", "size": "M", "housing": "Own"}


def test_reads_every_field_type(form_pdf):
    fields = {f.name: f for f in list_fields(form_pdf)}
    assert {n: f.type for n, f in fields.items()} == {
        "name": "text", "zip": "text", "notes": "text", "auto": "text", "employed": "checkbox",
        "employer": "text", "color": "combobox", "size": "listbox", "housing": "radio"}
    assert fields["employed"].on_states == ["On"]
    assert fields["housing"].on_states == ["Rent", "Own", "Other"]
    assert fields["zip"].max_length == 5 and fields["notes"].multiline


def test_full_round_trip_passes(form_pdf, tmp_path):
    out = fill_pdf(form_pdf, GOOD, tmp_path / "out.pdf")
    result = verify_pdf(out, GOOD)
    assert result.ok, result
    assert result.fields_checked == len(GOOD)
    actual = read_fields(out)
    assert actual["employed"] == "On"  # schema said "Yes"; the box's real on-state is used
    assert actual["housing"] == "Own"


def test_filled_values_are_visible(form_pdf, tmp_path):
    out = fill_pdf(form_pdf, GOOD, tmp_path / "out.pdf")
    doc = fitz.open(out)
    assert doc.xref_get_key(doc.pdf_catalog(), "AcroForm/NeedAppearances")[1] == "true"
    doc.bake()  # flatten appearances into page content, like a viewer that ignores form data
    text = doc[0].get_text()
    doc.close()
    assert "Ana Lopez" in text and "Corner Market" in text


@pytest.mark.parametrize("value, state", [("yes", "On"), ("X", "On"), ("On", "On"),
                                          ("no", "Off"), ("Off", "Off"), ("", "Off")])
def test_checkbox_values(form_pdf, tmp_path, value, state):
    out = fill_pdf(form_pdf, {"employed": value}, tmp_path / "out.pdf")
    assert read_fields(out)["employed"] == state
    assert verify_pdf(out, {"employed": value}).ok


def test_decode_name():
    from app.pdf.fields import decode_name

    assert decode_name("Not#20Hispanic#2FLatino") == "Not Hispanic/Latino"
    assert decode_name("Yes") == "Yes"


def test_wrong_checkbox_fails(form_pdf, tmp_path):
    out = fill_pdf(form_pdf, {"employed": "Yes"}, tmp_path / "out.pdf")
    result = verify_pdf(out, {"employed": "Off"})
    assert not result.ok
    assert result.mismatches[0].pdf_field == "employed" and result.mismatches[0].actual == "On"


def test_unrecognized_checkbox_value_is_not_guessed(form_pdf, tmp_path):
    out = fill_pdf(form_pdf, {"employed": "sometimes"}, tmp_path / "out.pdf")
    assert read_fields(out)["employed"] == "Off"
    assert not verify_pdf(out, {"employed": "sometimes"}).ok


def test_radio_selects_one_option_case_insensitively(form_pdf, tmp_path):
    first = fill_pdf(form_pdf, {"housing": "rent"}, tmp_path / "first.pdf")
    assert read_fields(first)["housing"] == "Rent"
    second = fill_pdf(first, {"housing": "OWN"}, tmp_path / "second.pdf")
    states = [w.on_state for w in list_fields(second)[-1].widgets]
    doc = fitz.open(second)
    selected = [doc.xref_get_key(w.xref, "AS")[1] for w in doc[0].widgets() if w.field_name == "housing"]
    doc.close()
    assert states == ["Rent", "Own", "Other"] and selected == ["/Off", "/Own", "/Off"]
    assert verify_pdf(second, {"housing": "Own"}).ok
    assert not verify_pdf(second, {"housing": "Rent"}).ok


def test_choice_matches_options_and_rejects_others(form_pdf, tmp_path):
    out = fill_pdf(form_pdf, {"color": "green", "size": "l"}, tmp_path / "out.pdf")
    assert read_fields(out)["color"] == "Green" and read_fields(out)["size"] == "L"
    assert verify_pdf(out, {"color": "Green", "size": "L"}).ok

    bad = fill_pdf(form_pdf, {"color": "Purple"}, tmp_path / "bad.pdf")
    result = verify_pdf(bad, {"color": "Purple"})
    assert not result.ok and result.mismatches[0].pdf_field == "color"


def test_text_mismatch_and_missing_field(form_pdf, tmp_path):
    out = fill_pdf(form_pdf, {"name": "Ana Lopez", "not_a_field": "x"}, tmp_path / "out.pdf")
    result = verify_pdf(out, {"name": "Someone Else", "not_a_field": "x"})
    assert not result.ok
    assert {(m.pdf_field, m.actual) for m in result.mismatches} == {
        ("name", "Ana Lopez"), ("not_a_field", MISSING)}


# ------------------------------------------------------------------ truncation

def _truncated(form_pdf, tmp_path, values, required=None):
    out = fill_pdf(form_pdf, values, tmp_path / "out.pdf")
    result = verify_pdf(out, values, required)
    assert not result.mismatches, result.mismatches
    return result.truncated


def test_text_wider_than_box_is_truncated(form_pdf, tmp_path):
    assert _truncated(form_pdf, tmp_path, {"name": "Maria Guadalupe Hernandez-Rodriguez de la Cruz"}) == ["name"]


def test_text_longer_than_pdf_max_length_is_truncated(form_pdf, tmp_path):
    assert _truncated(form_pdf, tmp_path, {"zip": "60601-1234"}) == ["zip"]


def test_text_longer_than_schema_max_length_is_truncated(form_pdf, tmp_path):
    schema = FormSchema(form_id="t", name="t", fields=[
        FormField(id="name", label="Name", question_hint="?", pdf_field="name", max_length=5)])
    assert _truncated(form_pdf, tmp_path, {"name": "Ana Lopez"}, schema) == ["name"]


def test_multiline_overflow_is_truncated(form_pdf, tmp_path):
    long_note = "Works nights at the corner market and sometimes weekends. " * 3
    assert _truncated(form_pdf, tmp_path, {"notes": long_note}) == ["notes"]


def test_auto_size_only_truncates_when_unreadable(form_pdf, tmp_path):
    assert _truncated(form_pdf, tmp_path, {"auto": "A fairly long line of text that shrinks"}) == []
    assert _truncated(form_pdf, tmp_path, {"auto": "x" * 200}) == ["auto"]


def test_text_that_fits_is_not_truncated(form_pdf, tmp_path):
    assert _truncated(form_pdf, tmp_path, GOOD) == []


# ------------------------------------------------------------------ required fields

SCHEMA = FormSchema(form_id="t", name="t", fields=[
    FormField(id="applicant", label="Name", question_hint="?", pdf_field="name", required=True),
    FormField(id="employed", label="Working", type="yes_no", question_hint="?", pdf_field="employed",
              pdf_values={"yes": "Yes", "no": "Off"}, required=True),
    FormField(id="employer", label="Employer", question_hint="?", pdf_field="employer", required=True,
              condition=FieldCondition(field="employed", equals="yes")),
    FormField(id="housing", label="Housing", type="choice", question_hint="?", pdf_field="housing",
              required=True),
    FormField(id="notes", label="Notes", question_hint="?", pdf_field="notes"),
])


def test_required_fields_left_empty_are_reported(form_pdf, tmp_path):
    values = {"employed": "Yes"}
    out = fill_pdf(form_pdf, values, tmp_path / "out.pdf")
    result = verify_pdf(out, values, SCHEMA)
    assert not result.ok
    assert result.missing_required == ["applicant", "employer", "housing"]


def test_conditional_required_field_skipped_when_condition_false(form_pdf, tmp_path):
    values = {"name": "Ana Lopez", "employed": "no", "housing": "Rent"}
    out = fill_pdf(form_pdf, values, tmp_path / "out.pdf")
    result = verify_pdf(out, values, SCHEMA)
    assert result.ok, result  # unchecked "employed" is a valid answer, employer not needed


def test_required_as_list_of_pdf_fields(form_pdf, tmp_path):
    out = fill_pdf(form_pdf, {"name": "Ana Lopez"}, tmp_path / "out.pdf")
    result = verify_pdf(out, {"name": "Ana Lopez"}, ["name", "employed", "housing"])
    assert result.missing_required == ["employed", "housing"]


# ------------------------------------------------------------------ XFA and tooling

def test_fill_removes_xfa_layer(form_pdf, tmp_path):
    doc = fitz.open(form_pdf)
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm/XFA", "[(template) ()]")
    xfa = tmp_path / "xfa.pdf"
    doc.save(xfa)
    doc.close()
    out = fill_pdf(xfa, {"name": "Ana Lopez"}, tmp_path / "out.pdf")
    doc = fitz.open(out)
    assert doc.xref_get_key(doc.pdf_catalog(), "AcroForm/XFA")[0] == "null"
    doc.close()


def test_inspect_pdf_script_lists_fields(form_pdf):
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "inspect_pdf.py"), str(form_pdf)],
                         capture_output=True, text=True, check=True).stdout
    assert "9 field(s)" in out
    assert "on-values: Rent, Own, Other" in out and "max length: 5" in out


def test_inspect_pdf_reports_non_fillable(tmp_path):
    doc = fitz.open()
    doc.new_page().insert_text((50, 50), "Flat scanned form")
    flat = tmp_path / "flat.pdf"
    doc.save(flat)
    doc.close()
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "inspect_pdf.py"), str(flat)],
                         capture_output=True, text=True, check=True).stdout
    assert "no fillable fields" in out
