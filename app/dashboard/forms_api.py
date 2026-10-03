"""Form library and documents for the dashboard. Owner: Lane D.

GET  /api/forms                     library list with review status and memory coverage
GET  /api/forms/{form_id}           schema + meta + problems found by checking it against the PDF
GET  /api/forms/{form_id}/pdf       the blank official PDF
POST /api/forms/{form_id}/review    {"reviewed": true|false}
POST /api/forms/upload              multipart: file, name, aliases (comma-separated), form_id (optional)
GET  /api/phones/{phone}/documents  letters this phone sent, newest first, with explanations
GET  /api/documents/{id}/media/{i}  one photo of a letter
"""

import mimetypes
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import select

from app.config import get_settings
from app.dashboard.api import _iso
from app.dashboard.demo import sync_forms
from app.db import session_scope
from app.engines import form_library, ingest
from app.memory import profile as memory
from app.models import Document, Profile, Task

router = APIRouter(prefix="/api")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_]{1,48}$")


# ---------------------------------------------------------------- form library

def _pdf_fields(form_id: str) -> Optional[set[str]]:
    from app.pdf.verify import read_fields

    path = form_library.pdf_path(form_id)
    if not path.exists():
        return None
    try:
        return set(read_fields(path))
    except Exception:
        return None


def schema_problems(form_id: str) -> list[str]:
    """Things a reviewer should fix before this form is used with real people."""
    schema = form_library.load_schema(form_id)
    problems: list[str] = []
    pdf_fields = _pdf_fields(form_id)
    if pdf_fields is None:
        problems.append("form.pdf is missing or unreadable")
    ids = [f.id for f in schema.fields]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"Duplicate field id: {dup}")
    for f in schema.fields:
        if f.pdf_field and pdf_fields is not None and f.pdf_field not in pdf_fields:
            problems.append(f"{f.id}: PDF has no field named {f.pdf_field!r}")
        if f.profile_key:
            issue = memory.validate_profile_key(f.profile_key)
            if issue:
                problems.append(f"{f.id}: {issue}")
        if f.condition and f.condition.field not in ids:
            problems.append(f"{f.id}: condition refers to unknown field {f.condition.field!r}")
        if f.type == "choice" and not f.options:
            problems.append(f"{f.id}: choice question has no options")
        if "ssn" in f.id.lower() and f.type != "ssn_last4" and not f.sensitive:
            problems.append(f"{f.id}: looks like an SSN field but isn't marked sensitive / ssn_last4")
    return problems


def _summary(form_id: str) -> dict:
    meta = form_library.load_meta(form_id)
    schema = form_library.load_schema(form_id)
    fields = schema.fields
    mapped = sum(1 for f in fields if f.profile_key)
    return {
        "form_id": form_id, "name": meta.name, "agency": meta.agency, "description": meta.description,
        "aliases": meta.aliases, "reviewed": schema.reviewed, "field_count": len(fields),
        "required_count": sum(f.required for f in fields),
        "memory_mapped": mapped, "memory_coverage": round(100 * mapped / len(fields)) if fields else 0,
        "has_pdf": form_library.pdf_path(form_id).exists(),
    }


@router.get("/forms")
def list_forms() -> list[dict]:
    return [_summary(m.form_id) for m in form_library.list_forms()]


@router.get("/forms/{form_id}")
def get_form(form_id: str) -> dict:
    if not _SLUG.match(form_id) or not (form_library.form_dir(form_id) / "schema.json").exists():
        raise HTTPException(404, "form not found")
    schema = form_library.load_schema(form_id)
    return {**_summary(form_id), "problems": schema_problems(form_id),
            "fields": [f.model_dump(mode="json") for f in schema.fields]}


@router.get("/forms/{form_id}/pdf")
def get_form_pdf(form_id: str) -> FileResponse:
    path = form_library.pdf_path(form_id)
    if not _SLUG.match(form_id) or not path.exists():
        raise HTTPException(404, "no PDF for this form")
    return FileResponse(path, media_type="application/pdf", filename=f"{form_id}.pdf",
                        content_disposition_type="inline")


class ReviewBody(BaseModel):
    reviewed: bool


@router.post("/forms/{form_id}/review")
def review_form(form_id: str, body: ReviewBody) -> dict:
    if not _SLUG.match(form_id) or not (form_library.form_dir(form_id) / "schema.json").exists():
        raise HTTPException(404, "form not found")
    schema = form_library.load_schema(form_id)
    schema.reviewed = body.reviewed
    form_library.save_schema(schema)
    sync_forms()
    return _summary(form_id)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:48]
    return slug or f"form_{uuid.uuid4().hex[:6]}"


@router.post("/forms/upload")
async def upload_form(file: UploadFile = File(...), name: str = Form(...), aliases: str = Form(""),
                      form_id: str = Form("")) -> dict:
    """Ingest a brand-new official PDF so it becomes fillable by phone."""
    name = name.strip()
    if not name:
        raise HTTPException(400, "Give the form a name.")
    form_id = (form_id.strip().lower() or _slugify(name))
    if not _SLUG.match(form_id):
        raise HTTPException(400, "Form id must be lowercase letters, numbers, and underscores.")
    if form_library.form_dir(form_id).exists():
        raise HTTPException(409, f"A form with id {form_id!r} already exists.")

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "PDF is larger than 20 MB.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "That file isn't a PDF.")

    upload_dir = get_settings().data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    tmp = upload_dir / f"{form_id}_{uuid.uuid4().hex[:8]}.pdf"
    tmp.write_bytes(data)
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    try:
        await run_in_threadpool(ingest.ingest_pdf, tmp, form_id=form_id, name=name, aliases=alias_list)
    except NotImplementedError as e:
        _cleanup(form_id)
        raise HTTPException(501, f"Form ingestion isn't available yet: {e}")
    except ValueError as e:  # e.g. "This PDF has no fillable fields"
        _cleanup(form_id)
        raise HTTPException(422, str(e))
    except Exception as e:
        _cleanup(form_id)
        raise HTTPException(500, f"Ingestion failed: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    sync_forms()
    return {**_summary(form_id), "problems": schema_problems(form_id)}


def _cleanup(form_id: str) -> None:
    """Remove a half-written form folder so a failed upload can be retried."""
    d = form_library.form_dir(form_id)
    if d.exists() and d.parent.resolve() == get_settings().forms_dir.resolve():
        shutil.rmtree(d)


# ---------------------------------------------------------------- documents (letters)

@router.get("/phones/{phone}/documents")
def phone_documents(phone: str, limit: int = 10) -> list[dict]:
    with session_scope() as s:
        pids = [p.id for p in s.exec(select(Profile).where(Profile.phone == phone)).all()]
        if not pids:
            return []
        docs = s.exec(select(Document).where(Document.profile_id.in_(pids))
                      .order_by(Document.created_at.desc()).limit(limit)).all()
        started = {t.document_id: t for t in s.exec(select(Task).where(Task.document_id.in_([d.id for d in docs]))).all()}
    names = {m.form_id: m.name for m in form_library.list_forms()}
    out = []
    for d in docs:
        result = d.result or {}
        related = d.related_form_id or result.get("related_form_id")
        task = started.get(d.id)
        out.append({
            "id": d.id, "profile_id": d.profile_id, "created_at": _iso(d.created_at),
            "document_type": d.document_type or result.get("document_type"),
            "explanation": result,
            "confidence": d.confidence if d.confidence is not None else result.get("confidence"),
            "related_form": {"form_id": related, "name": names.get(related, related)} if related else None,
            "form_started": {"task_id": task.id, "status": task.status} if task else None,
            "media": [f"/api/documents/{d.id}/media/{i}" for i in range(len(d.media_paths))],
        })
    return out


@router.get("/documents/{document_id}/media/{index}")
def document_media(document_id: int, index: int) -> FileResponse:
    with session_scope() as s:
        doc = s.get(Document, document_id)
    if doc is None or not (0 <= index < len(doc.media_paths)):
        raise HTTPException(404, "no such photo")
    path = Path(doc.media_paths[index]).resolve()
    data_dir = get_settings().data_dir.resolve()
    if data_dir not in path.parents or not path.is_file():
        raise HTTPException(404, "no such photo")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")
