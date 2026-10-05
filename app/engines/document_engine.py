"""Explain a photographed letter or document.

The model reads the photos and proposes a DocumentExplanation; this module prepares the images
and then checks the proposal: related_form_id must be a real form, Social Security numbers are
dropped, high-stakes documents are caught even if the model misses them, and confidence is kept
in range.
"""

import base64
import logging
import mimetypes
import re
from datetime import date
from pathlib import Path

import pymupdf as fitz

from app.contracts import DocumentExplanation
from app.engines import form_library
from app.llm import client as llm
from app.llm.prompts import document as prompts

log = logging.getLogger(__name__)

SUPPORTED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGES = 10  # a long letter sent page by page; more than this is almost certainly a mistake
MAX_PDF_PAGES = 5
MAX_EDGE_PX = 2000  # larger photos are downscaled: faster, cheaper, and no less readable
MAX_BYTES = 3_500_000  # stays under the API's per-image limit after base64

# Backstop for high_stakes: any of these in what the model extracted means "refer to legal help".
_HIGH_STAKES = re.compile(r"evict|foreclos|\bcourts?\b|summons|lawsuit|immigra|deport|"
                          r"notice to (quit|vacate)|writ of", re.IGNORECASE)
_SSN = re.compile(r"(?<!\d)\d{3}[- ]?\d{2}[- ]?\d{4}(?!\d)")


def explain_document(media_paths: list[str], *, language: str = "en") -> DocumentExplanation:
    """Read photo(s) of a document and return a structured explanation (fields in English).

    Accepts JPEG/PNG/GIF/WebP photos and PDFs (first pages rendered). Unreadable attachments are
    listed in unreadable_parts. With nothing readable, returns confidence 0 without calling the
    model. Raises anthropic.APIError (or ValueError on a refusal) if the model call fails, so the
    caller can apologize and retry rather than report a blurry photo.
    """
    blocks, skipped = _image_blocks(media_paths)
    if not blocks:
        return DocumentExplanation(
            document_type="unknown",
            plain_summary="No readable photo of the document was received.",
            confidence=0.0,
            unreadable_parts=[f"could not open: {name}" for name in skipped] or ["no photo received"],
        )

    forms = form_library.list_forms()
    proposal = llm.structured(
        DocumentExplanation,
        system=prompts.SYSTEM + "\n\n" + prompts.library_text(forms),
        messages=[{"role": "user", "content": [
            *blocks,
            {"type": "text", "text": prompts.user_text(pages=len(blocks), today=date.today(),
                                                       language=language, skipped=skipped)},
        ]}],
        model=llm.strong_model(),
    )
    return _validated(proposal, {f.form_id for f in forms}, skipped)


def _validated(result: DocumentExplanation, form_ids: set[str], skipped: list[str]) -> DocumentExplanation:
    related = result.related_form_id
    if related and related not in form_ids:
        log.warning("explain_document: model proposed unknown form %r; dropped", related)
        related = None
    text = " ".join(filter(None, [result.document_type, result.plain_summary, result.action_required]))
    unreadable = list(result.unreadable_parts)
    for name in skipped:
        if not any(name in u for u in unreadable):
            unreadable.append(f"could not open: {name}")
    return result.model_copy(update={
        "related_form_id": related,
        "high_stakes": result.high_stakes or bool(_HIGH_STAKES.search(text)),
        "confidence": min(1.0, max(0.0, result.confidence)),
        "reference_numbers": [r for r in result.reference_numbers if not _SSN.search(r)],
        "unreadable_parts": unreadable,
    })


# ---------------------------------------------------------------- images

def _image_blocks(media_paths: list[str]) -> tuple[list[dict], list[str]]:
    """Content blocks for every readable attachment, plus names of the ones we couldn't open."""
    blocks: list[dict] = []
    skipped: list[str] = []
    for p in map(Path, media_paths):
        try:
            if _media_type(p) == "application/pdf":
                blocks.extend(_pdf_blocks(p))
            else:
                blocks.append(_photo_block(p))
        except Exception as e:  # corrupt file, HEIC, not an image at all
            log.warning("explain_document: skipping %s: %s", p.name, e)
            skipped.append(p.name)
    if len(blocks) > MAX_IMAGES:
        skipped.append(f"{len(blocks) - MAX_IMAGES} extra page(s) beyond the first {MAX_IMAGES}")
        blocks = blocks[:MAX_IMAGES]
    return blocks, skipped


def _media_type(p: Path) -> str | None:
    if p.read_bytes()[:5] == b"%PDF-":
        return "application/pdf"
    return mimetypes.guess_type(p.name)[0]


def _photo_block(p: Path) -> dict:
    data = p.read_bytes()
    pix = fitz.Pixmap(str(p))  # also proves the file is an image
    if _media_type(p) in SUPPORTED_TYPES and len(data) <= MAX_BYTES and max(pix.width, pix.height) <= MAX_EDGE_PX:
        return _block(data, _media_type(p))
    return _block(*_encode(pix))


def _pdf_blocks(p: Path) -> list[dict]:
    doc = fitz.open(p)
    try:
        return [_block(*_encode(page.get_pixmap(dpi=150))) for page in list(doc)[:MAX_PDF_PAGES]]
    finally:
        doc.close()


def _encode(pix: fitz.Pixmap) -> tuple[bytes, str]:
    """Downscale to MAX_EDGE_PX and encode as JPEG (photos) for a small, supported upload."""
    if pix.alpha or pix.colorspace is None or pix.colorspace.n not in (1, 3):
        pix = fitz.Pixmap(fitz.csRGB, pix, 0)
    while max(pix.width, pix.height) > MAX_EDGE_PX:
        pix.shrink(1)  # halves each side
    return pix.tobytes("jpeg", jpg_quality=85), "image/jpeg"


def _block(data: bytes, media_type: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.standard_b64encode(data).decode("ascii")}}
