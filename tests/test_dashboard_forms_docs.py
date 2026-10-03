import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT_DIR, get_settings
from app.contracts import FormSchema
from app.core import identity
from app.db import session_scope
from app.models import Document, Task


@pytest.fixture
def forms_dir(tmp_path, monkeypatch):
    """A private copy of the form library, so tests can change schemas safely."""
    d = tmp_path / "forms"
    shutil.copytree(ROOT_DIR / "forms", d)
    monkeypatch.setenv("FORMS_DIR", str(d))
    get_settings.cache_clear()
    yield d
    get_settings.cache_clear()


@pytest.fixture
def client(forms_dir):
    from app.main import app
    with TestClient(app) as c:
        yield c


def _pdf_bytes() -> bytes:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    w = pymupdf.Widget()
    w.field_name, w.field_type, w.rect = "applicant", pymupdf.PDF_WIDGET_TYPE_TEXT, pymupdf.Rect(50, 50, 300, 70)
    page.add_widget(w)
    return doc.tobytes()


# ---------------------------------------------------------------- form library

def test_list_and_detail(client):
    forms = client.get("/api/forms").json()
    sample = next(f for f in forms if f["form_id"] == "sample_benefits")
    assert sample["field_count"] == 8 and sample["memory_coverage"] == 75
    detail = client.get("/api/forms/sample_benefits").json()
    assert detail["problems"] == []
    assert detail["fields"][0]["question_hint"]
    assert client.get("/api/forms/nope").status_code == 404
    assert client.get("/api/forms/..%2Fapp").status_code == 404
    assert client.get("/api/forms/sample_benefits/pdf").content.startswith(b"%PDF")


def test_problems_are_reported(client, forms_dir):
    path = forms_dir / "sample_benefits" / "schema.json"
    data = json.loads(path.read_text())
    data["fields"][0]["pdf_field"] = "no_such_field"
    data["fields"][1]["profile_key"] = "favorite_color"
    data["fields"][2]["condition"] = {"field": "ghost", "equals": "yes"}
    path.write_text(json.dumps(data))
    problems = client.get("/api/forms/sample_benefits").json()["problems"]
    assert any("no_such_field" in p for p in problems)
    assert any("favorite_color" in p for p in problems)
    assert any("ghost" in p for p in problems)


def test_mark_reviewed_round_trip(client, forms_dir):
    assert client.post("/api/forms/sample_benefits/review", json={"reviewed": False}).json()["reviewed"] is False
    on_disk = FormSchema.model_validate_json((forms_dir / "sample_benefits" / "schema.json").read_text())
    assert on_disk.reviewed is False
    assert client.post("/api/forms/sample_benefits/review", json={"reviewed": True}).json()["reviewed"] is True


def test_upload_runs_real_ingestion_with_a_mocked_model(client, forms_dir, monkeypatch):
    from app.engines.ingest import Draft, DraftField
    from app.llm import client as llm

    draft = Draft(fields=[
        DraftField(id="applicant", label="Name", type="text", question_hint="What is your name?",
                   pdf_field="applicant", profile_key="full_name", required=True),
        DraftField(id="heats_with_gas", label="Heats with gas", type="yes_no", question_hint="Do you heat with gas?"),
        DraftField(id="behind_on_bills", label="Behind on bills", type="yes_no", question_hint="Are you behind?"),
    ])
    monkeypatch.setattr(llm, "structured", lambda output, **kw: draft)
    r = client.post("/api/forms/upload", data={"name": "LIHEAP Energy Help"},
                    files={"file": ("liheap.pdf", _pdf_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["form_id"] == "liheap_energy_help" and body["reviewed"] is False and body["problems"] == []
    assert (forms_dir / "liheap_energy_help" / "form.pdf").exists()


def test_upload_of_a_flat_pdf_is_rejected(client, forms_dir):
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "Scanned form")
    r = client.post("/api/forms/upload", data={"name": "Flat Form"},
                    files={"file": ("flat.pdf", doc.tobytes(), "application/pdf")})
    assert r.status_code == 422 and "no fillable fields" in r.json()["detail"]
    assert not (forms_dir / "flat_form").exists()


def test_upload_validation(client):
    pdf = ("f.pdf", _pdf_bytes(), "application/pdf")
    assert client.post("/api/forms/upload", data={"name": "X"},
                       files={"file": ("f.txt", b"hello", "text/plain")}).status_code == 400
    assert client.post("/api/forms/upload", data={"name": "Dup", "form_id": "sample_benefits"},
                       files={"file": pdf}).status_code == 409
    assert client.post("/api/forms/upload", data={"name": "Bad", "form_id": "../evil"},
                       files={"file": pdf}).status_code == 400


def test_upload_success_with_fake_ingest(client, forms_dir, monkeypatch):
    from app.contracts import FormField, FormMeta
    from app.engines import form_library, ingest

    def fake_ingest(pdf_path, *, form_id, name, aliases=None):
        d = forms_dir / form_id
        d.mkdir()
        shutil.copy(pdf_path, d / "form.pdf")
        (d / "meta.json").write_text(FormMeta(form_id=form_id, name=name, aliases=aliases or []).model_dump_json())
        schema = FormSchema(form_id=form_id, name=name, fields=[
            FormField(id="applicant", label="Name", question_hint="What is your name?", pdf_field="applicant",
                      profile_key="full_name")])
        form_library.save_schema(schema)
        return schema

    monkeypatch.setattr(ingest, "ingest_pdf", fake_ingest)
    r = client.post("/api/forms/upload", data={"name": "LIHEAP Energy Help", "aliases": "heating help, LIHEAP"},
                    files={"file": ("liheap.pdf", _pdf_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["form_id"] == "liheap_energy_help" and body["reviewed"] is False and body["problems"] == []
    assert body["aliases"] == ["heating help", "LIHEAP"]
    assert any(f["form_id"] == "liheap_energy_help" for f in client.get("/api/forms").json())


def test_failed_ingest_cleans_up(client, forms_dir, monkeypatch):
    from app.engines import ingest

    def broken(pdf_path, *, form_id, name, aliases=None):
        (forms_dir / form_id).mkdir()
        raise ValueError("This PDF has no fillable fields")

    monkeypatch.setattr(ingest, "ingest_pdf", broken)
    r = client.post("/api/forms/upload", data={"name": "Flat Scan"},
                    files={"file": ("f.pdf", _pdf_bytes(), "application/pdf")})
    assert r.status_code == 422 and "no fillable fields" in r.json()["detail"]
    assert not (forms_dir / "flat_scan").exists()


# ---------------------------------------------------------------- documents

def _doc(phone: str, media: list[str]) -> int:
    pid = identity.create_profile(phone, display_name="Ana").id
    result = {"document_type": "Medicaid renewal notice", "plain_summary": "Renew by Nov 1.",
              "deadlines": [{"date": "2026-11-01", "description": "Return the form"}],
              "related_form_id": "sample_benefits", "confidence": 0.9}
    with session_scope() as s:
        d = Document(profile_id=pid, media_paths=media, document_type=result["document_type"], result=result,
                     related_form_id="sample_benefits", confidence=0.9)
        s.add(d)
        s.commit()
        s.refresh(d)
        s.add(Task(profile_id=pid, kind="fill_form", form_id="sample_benefits", document_id=d.id))
        s.commit()
        return d.id


def test_documents_for_phone_and_media(client, tmp_path):
    photo = tmp_path / "media" / "letter.png"  # tmp_path is DATA_DIR in tests
    photo.parent.mkdir()
    photo.write_bytes(b"\x89PNG fake")
    doc_id = _doc("+12025550170", [str(photo)])
    docs = client.get("/api/phones/+12025550170/documents").json()
    assert len(docs) == 1
    d = docs[0]
    assert d["explanation"]["plain_summary"] == "Renew by Nov 1."
    assert d["related_form"]["name"] == "Sample Benefits Application"
    assert d["form_started"]["status"] == "active"
    r = client.get(d["media"][0])
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    assert client.get(f"/api/documents/{doc_id}/media/5").status_code == 404
    assert client.get("/api/phones/+19999999999/documents").json() == []


def test_media_outside_data_dir_is_refused(client, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "secret.png"
    outside.write_bytes(b"secret")
    doc_id = _doc("+12025550171", [str(outside)])
    assert client.get(f"/api/documents/{doc_id}/media/0").status_code == 404
