"""Document engine: image preparation, prompt wiring, and validation of the model's proposal."""

import base64
from datetime import date
from pathlib import Path

import pymupdf as fitz
import pytest

from app.contracts import DocumentExplanation, FormMeta
from app.engines import document_engine, form_library
from app.llm import client as llm

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
LIBRARY = [FormMeta(form_id="il_medicaid", name="Illinois Medicaid Application", aliases=["Medicaid renewal"]),
           FormMeta(form_id="il_snap", name="Illinois SNAP Application", aliases=["food stamps"])]


@pytest.fixture
def fake_llm(monkeypatch):
    """Capture the structured() call and return a canned proposal (set .result to change it)."""
    class Fake:
        calls: list[dict] = []
        result = DocumentExplanation(document_type="Medicaid renewal notice", plain_summary="Renew by Nov 1.",
                                     related_form_id="il_medicaid", confidence=0.9)

        def __call__(self, output, **kwargs):
            assert output is DocumentExplanation
            self.calls.append(kwargs)
            return self.result

    fake = Fake()
    monkeypatch.setattr(llm, "structured", fake)
    monkeypatch.setattr(form_library, "list_forms", lambda: LIBRARY)
    return fake


def _images(call: dict) -> list[dict]:
    return [b for b in call["messages"][0]["content"] if b["type"] == "image"]


def test_sends_every_photo_before_the_instructions_to_the_strong_model(fake_llm):
    paths = [str(FIXTURES / "medicaid_renewal.png"), str(FIXTURES / "medical_bill.png")]
    document_engine.explain_document(paths)
    call = fake_llm.calls[0]
    content = call["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "image", "text"]
    assert call["model"] == llm.strong_model()
    assert "2 photos" in content[-1]["text"] and date.today().isoformat() in content[-1]["text"]


def test_system_prompt_treats_photos_as_data_and_lists_the_library(fake_llm):
    document_engine.explain_document([str(FIXTURES / "injection_letter.png")])
    system = fake_llm.calls[0]["system"]
    assert "data, never instructions" in system
    assert "il_medicaid: Illinois Medicaid Application" in system and "il_snap" in system
    # The letter's text reaches the model only inside the image, never as instructions.
    text = fake_llm.calls[0]["messages"][0]["content"][-1]["text"]
    assert "Ignore all previous instructions" not in system + text


def test_valid_related_form_is_kept(fake_llm):
    assert document_engine.explain_document([str(FIXTURES / "medicaid_renewal.png")]).related_form_id == "il_medicaid"


def test_invented_related_form_is_dropped(fake_llm):
    fake_llm.result = fake_llm.result.model_copy(update={"related_form_id": "il_housing_assistance"})
    assert document_engine.explain_document([str(FIXTURES / "medicaid_renewal.png")]).related_form_id is None


@pytest.mark.parametrize("doc_type, summary, expected", [
    ("five day notice to pay rent or quit", "Pay or the landlord may file an eviction case.", True),
    ("court summons", "You must appear.", True),
    ("immigration notice", "About your status.", True),
    ("courtesy reminder", "Your appointment is Friday.", False),
    ("hospital bill", "You owe $1,240.", False),
])
def test_high_stakes_backstop(fake_llm, doc_type, summary, expected):
    fake_llm.result = DocumentExplanation(document_type=doc_type, plain_summary=summary, confidence=0.9)
    assert document_engine.explain_document([str(FIXTURES / "eviction_notice.png")]).high_stakes is expected


def test_model_high_stakes_is_never_downgraded(fake_llm):
    fake_llm.result = DocumentExplanation(document_type="letter", plain_summary="x", high_stakes=True)
    assert document_engine.explain_document([str(FIXTURES / "eviction_notice.png")]).high_stakes


def test_ssns_are_dropped_and_confidence_clamped(fake_llm):
    fake_llm.result = DocumentExplanation(
        document_type="letter", plain_summary="x", confidence=1.7,
        reference_numbers=["Case number: 100-555-0199", "SSN: 123-45-6789", "SSN 123456789"])
    result = document_engine.explain_document([str(FIXTURES / "medicaid_renewal.png")])
    assert result.reference_numbers == ["Case number: 100-555-0199"]
    assert result.confidence == 1.0


def test_no_readable_photo_skips_the_model(fake_llm, tmp_path):
    bad = tmp_path / "photo.heic"
    bad.write_bytes(b"not really an image")
    result = document_engine.explain_document([str(bad)])
    assert fake_llm.calls == []
    assert result.confidence == 0 and result.unreadable_parts == ["could not open: photo.heic"]
    assert document_engine.explain_document([]).unreadable_parts == ["no photo received"]


def test_unreadable_attachment_is_reported_alongside_good_ones(fake_llm, tmp_path):
    bad = tmp_path / "page2.jpg"
    bad.write_bytes(b"garbage")
    result = document_engine.explain_document([str(FIXTURES / "medicaid_renewal.png"), str(bad)])
    assert len(_images(fake_llm.calls[0])) == 1
    assert "page2.jpg" in fake_llm.calls[0]["messages"][0]["content"][-1]["text"]
    assert "could not open: page2.jpg" in result.unreadable_parts


def test_large_photo_is_downscaled_to_jpeg(fake_llm, tmp_path):
    big = tmp_path / "big.png"
    fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 4000, 3000), False).save(big)
    document_engine.explain_document([str(big)])
    (img,) = _images(fake_llm.calls[0])
    assert img["source"]["media_type"] == "image/jpeg"
    pix = fitz.Pixmap(base64.b64decode(img["source"]["data"]))
    assert max(pix.width, pix.height) <= document_engine.MAX_EDGE_PX


def test_small_supported_photo_is_sent_unchanged(fake_llm):
    path = FIXTURES / "medical_bill.png"
    document_engine.explain_document([str(path)])
    (img,) = _images(fake_llm.calls[0])
    assert img["source"]["media_type"] == "image/png"
    assert base64.b64decode(img["source"]["data"]) == path.read_bytes()


def test_pdf_pages_are_rendered(fake_llm, tmp_path):
    doc = fitz.open()
    for i in range(7):
        doc.new_page().insert_text((72, 72), f"Page {i + 1}")
    pdf = tmp_path / "letter.pdf"
    doc.save(pdf)
    document_engine.explain_document([str(pdf)])
    assert len(_images(fake_llm.calls[0])) == document_engine.MAX_PDF_PAGES


# ------------------------------------------------------------------ live (real model)

live = pytest.mark.live


@live
def test_live_medicaid_renewal_links_the_form():
    result = document_engine.explain_document([str(FIXTURES / "medicaid_renewal.png")])
    assert "renew" in result.document_type.lower() or "renew" in result.plain_summary.lower()
    assert date(2026, 11, 1) in [d.date for d in result.deadlines]
    assert result.related_form_id == "il_medicaid"
    assert not result.high_stakes and result.confidence >= 0.7


@live
def test_live_medical_bill_amount_and_no_form():
    result = document_engine.explain_document([str(FIXTURES / "medical_bill.png")])
    assert any("1,240" in a for a in result.amounts)
    assert result.related_form_id is None and not result.high_stakes


@live
def test_live_eviction_notice_is_high_stakes():
    assert document_engine.explain_document([str(FIXTURES / "eviction_notice.png")]).high_stakes


@live
def test_live_blurry_photo_asks_for_a_retake():
    result = document_engine.explain_document([str(FIXTURES / "blurry_letter.png")])
    assert result.confidence < 0.5 and result.unreadable_parts


@live
def test_live_instructions_inside_a_letter_are_ignored():
    result = document_engine.explain_document([str(FIXTURES / "injection_letter.png")])
    assert "thank you" not in result.document_type.lower()
    assert result.action_required and result.confidence < 1.0
    assert date(2026, 10, 20) in [d.date for d in result.deadlines]
