"""ingest_pdf: PDF description, draft -> schema, checks, retry, repair, and the files it writes."""

import json
import subprocess
import sys
from pathlib import Path

import pymupdf as fitz
import pytest

from app import config
from app.engines import form_library, ingest
from app.engines.ingest import Draft, DraftField
from app.llm import client as llm

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def forms_dir(tmp_path, monkeypatch):
    d = tmp_path / "forms"
    d.mkdir()
    monkeypatch.setenv("FORMS_DIR", str(d))
    config.get_settings.cache_clear()
    yield d
    config.get_settings.cache_clear()


def _rename_on_state(doc, xref, new):
    on = doc.xref_get_key(xref, "AP/N/Yes")[1]
    off = doc.xref_get_key(xref, "AP/N/Off")[1]
    doc.xref_set_key(xref, "AP/N", f"<</{new} {on}/Off {off}>>")


@pytest.fixture
def form_pdf(tmp_path) -> Path:
    """Generic field names (as authoring tools leave them) with printed labels; a Yes/No radio
    whose left button (Yes) has state "1" and right button (No) has state "0"."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 62), "Full name:", fontsize=10)
    page.insert_text((50, 92), "Social Security Number:", fontsize=10)
    page.insert_text((50, 122), "Are you working now?", fontsize=10)
    page.insert_text((266, 122), "Yes", fontsize=10)
    page.insert_text((316, 122), "No", fontsize=10)
    page.insert_text((50, 152), "Employer:", fontsize=10)
    page.insert_text((50, 182), "Homeless", fontsize=10)

    def add(name, ftype, rect, **kw):
        w = fitz.Widget()
        w.field_name, w.field_type, w.rect = name, ftype, fitz.Rect(rect)
        for k, v in kw.items():
            setattr(w, k, v)
        page.add_widget(w)

    T = fitz.PDF_WIDGET_TYPE_TEXT
    add("TextField1[0]", T, (200, 50, 450, 66), text_fontsize=10, text_maxlen=40)
    add("TextField1[1]", T, (200, 80, 450, 96), text_fontsize=10)
    add("RadioButtonList[0]", fitz.PDF_WIDGET_TYPE_RADIOBUTTON, (250, 112, 262, 124), field_value=False)
    add("RadioButtonList[0]", fitz.PDF_WIDGET_TYPE_RADIOBUTTON, (300, 112, 312, 124), field_value=False)
    add("TextField1[2]", T, (200, 140, 450, 156), text_fontsize=10)
    add("CheckBox1[0]", fitz.PDF_WIDGET_TYPE_CHECKBOX, (200, 170, 212, 182))
    radios = [w.xref for w in page.widgets() if w.field_name == "RadioButtonList[0]"]
    _rename_on_state(doc, radios[0], "1")
    _rename_on_state(doc, radios[1], "0")
    path = tmp_path / "upload.pdf"
    doc.save(path)
    doc.close()
    return path


GOOD = [
    DraftField(id="applicant_name", label="Full name", type="text", question_hint="What is your full name?",
               pdf_field="TextField1[0]", profile_key="full_name", required=True, group="About you"),
    DraftField(id="ssn", label="Social Security number", type="text", question_hint="What is your SSN?",
               pdf_field="TextField1[1]"),
    # The model gets the states backwards; printed labels must win.
    DraftField(id="employed", label="Working now", type="yes_no", question_hint="Are you working right now?",
               pdf_field="RadioButtonList[0]", yes_value="0", no_value="1", group="Income"),
    DraftField(id="employer", label="Employer", type="text", question_hint="Who do you work for?",
               pdf_field="TextField1[2]", profile_key="employment.employer",
               condition_field="employed", condition_equals="yes"),
    DraftField(id="homeless", label="Homeless", type="yes_no", question_hint="Are you without a home right now?",
               pdf_field="CheckBox1[0]"),
]


@pytest.fixture
def fake_llm(monkeypatch):
    class Fake:
        drafts: list[list[DraftField]] = [GOOD]
        calls: list[dict] = []

        def __call__(self, output, **kwargs):
            assert output is Draft
            self.calls.append(kwargs)
            return Draft(fields=self.drafts[min(len(self.calls), len(self.drafts)) - 1])

    fake = Fake()
    monkeypatch.setattr(llm, "structured", fake)
    return fake


def test_writes_pdf_schema_and_meta(form_pdf, fake_llm, forms_dir):
    schema = ingest.ingest_pdf(form_pdf, form_id="test_form", name="Test Form", aliases=["test app"])
    assert len(fake_llm.calls) == 1
    assert schema.reviewed is False
    assert (forms_dir / "test_form" / "form.pdf").read_bytes() == form_pdf.read_bytes()
    assert form_library.load_schema("test_form") == schema
    meta = json.loads((forms_dir / "test_form" / "meta.json").read_text(encoding="utf-8"))
    assert meta["name"] == "Test Form" and meta["aliases"] == ["test app"]
    assert form_library.match_form("help with my test app") == "test_form"


def test_code_supplies_what_it_can_read_from_the_pdf(form_pdf, fake_llm):
    f = {x.id: x for x in ingest.ingest_pdf(form_pdf, form_id="test_form", name="Test").fields}
    assert f["employed"].pdf_values == {"yes": "1", "no": "0"}  # from the printed Yes / No labels
    assert f["homeless"].pdf_values == {"yes": "Yes", "no": "Off"}
    assert f["ssn"].type == "ssn_last4" and f["ssn"].sensitive and f["ssn"].validation == r"^\d{4}$"
    assert f["applicant_name"].max_length == 40
    assert f["employer"].condition.field == "employed" and f["employer"].condition.equals == "yes"


def test_prompt_describes_fields_with_printed_labels(form_pdf, fake_llm):
    ingest.ingest_pdf(form_pdf, form_id="test_form", name="Test")
    text = fake_llm.calls[0]["messages"][0]["content"]
    assert "TextField1[0] | text | p1 | Full name: | max 40 chars" in text
    assert "RadioButtonList[0] | radio | p1 | Are you working now? | states: 1='Yes', 0='No'" in text
    assert "- employment: The person's job" in text  # canonical keys from Lane D
    assert fake_llm.calls[0]["model"] == llm.strong_model()


def test_retries_once_with_the_problems(form_pdf, fake_llm):
    bad = [*GOOD[:3],
           DraftField(id="made_up", label="X", type="text", question_hint="?", pdf_field="NoSuchField"),
           GOOD[3].model_copy(update={"condition_field": "later_field"})]
    fake_llm.drafts = [bad, GOOD]
    schema = ingest.ingest_pdf(form_pdf, form_id="test_form", name="Test")
    assert len(fake_llm.calls) == 2
    retry = fake_llm.calls[1]["messages"][0]["content"]
    assert "'NoSuchField' is not in the field list" in retry
    assert "condition_field 'later_field' must be an earlier field" in retry
    assert [f.id for f in schema.fields] == [f.id for f in GOOD]


def test_repairs_what_is_still_wrong_after_the_retry(form_pdf, fake_llm):
    bad = [*GOOD,
           DraftField(id="made_up", label="X", type="text", question_hint="?", pdf_field="NoSuchField"),
           DraftField(id="employer", label="Employer again", type="text", question_hint="Employer?"),
           GOOD[0].model_copy(update={"id": "name_again"}),  # same PDF box twice
           DraftField(id="pets", label="Pets", type="number", question_hint="How many pets?",
                      profile_key="pets_count", condition_field="nope")]
    fake_llm.drafts = [bad, bad]
    schema = ingest.ingest_pdf(form_pdf, form_id="test_form", name="Test")
    ids = [f.id for f in schema.fields]
    assert "made_up" not in ids and "name_again" not in ids
    assert "employer_2" in ids
    pets = next(f for f in schema.fields if f.id == "pets")
    assert pets.profile_key is None and pets.condition is None
    assert ingest.schema_problems(schema, {f.name: f for f in ingest.list_fields_in(fitz.open(form_pdf))}) == []


def test_result_passes_the_dashboard_review_checks(form_pdf, fake_llm):
    from app.dashboard.forms_api import schema_problems

    ingest.ingest_pdf(form_pdf, form_id="test_form", name="Test")
    assert schema_problems("test_form") == []


def test_reingest_keeps_hand_written_meta(form_pdf, fake_llm, forms_dir):
    ingest.ingest_pdf(form_pdf, form_id="test_form", name="Test", aliases=["a"])
    meta_path = forms_dir / "test_form" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta_path.write_text(json.dumps({**meta, "agency": "Hand-written agency"}), encoding="utf-8")
    ingest.ingest_pdf(form_pdf, form_id="test_form", name="Test v2")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["agency"] == "Hand-written agency" and meta["name"] == "Test v2" and meta["aliases"] == ["a"]


def test_rejects_pdfs_without_fillable_fields(tmp_path, fake_llm):
    doc = fitz.open()
    doc.new_page().insert_text((50, 50), "Scanned form")
    flat = tmp_path / "flat.pdf"
    doc.save(flat)
    with pytest.raises(ValueError, match="no fillable fields"):
        ingest.ingest_pdf(flat, form_id="flat_form", name="Flat")
    assert fake_llm.calls == []


def test_rejects_non_pdfs_and_bad_ids(tmp_path, form_pdf, fake_llm):
    junk = tmp_path / "photo.pdf"
    junk.write_bytes(b"not a pdf")
    with pytest.raises(ValueError, match="readable PDF"):
        ingest.ingest_pdf(junk, form_id="junk_form", name="Junk")
    for bad_id in ("../etc", "Has Spaces", "x", "1form"):
        with pytest.raises(ValueError, match="Form id"):
            ingest.ingest_pdf(form_pdf, form_id=bad_id, name="Bad")


def test_fails_cleanly_when_nothing_usable_is_left(form_pdf, fake_llm):
    junk = [DraftField(id=f"q{i}", label="X", type="text", question_hint="?", pdf_field=f"Nope{i}") for i in range(5)]
    fake_llm.drafts = [junk, junk]
    with pytest.raises(ValueError, match="usable question"):
        ingest.ingest_pdf(form_pdf, form_id="test_form", name="Test")


def test_cli_help():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "ingest_form.py"), "--help"],
                         capture_output=True, text=True, check=True).stdout
    assert "--id" in out and "--alias" in out


@pytest.mark.live
def test_live_ingest_school_meals(forms_dir):
    real = ROOT / "forms" / "il_school_meals" / "form.pdf"
    schema = ingest.ingest_pdf(real, form_id="meals_live", name="School Meals", aliases=["school lunch"])
    from app.dashboard.forms_api import schema_problems

    assert schema_problems("meals_live") == []
    assert 8 <= len(schema.fields) <= 40
    assert any(f.profile_key and f.profile_key.startswith("household_members") for f in schema.fields)
