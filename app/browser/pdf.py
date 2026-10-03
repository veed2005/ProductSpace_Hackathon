"""Reading a PDF the person has open in Chrome.

Chrome shows PDFs in its own viewer, which page snapshots can't see into. When a tab is a PDF, the
extension downloads it from the page (with the person's own session, nothing else) and sends the
bytes here. This extracts the whole text, page by page, with secrets masked like any page text.
Scanned PDFs have no text layer, so their pages are rendered as images for the model to read.
"""

from __future__ import annotations

import base64
import re
from typing import Optional
from urllib.parse import unquote, urlparse

import pymupdf

from app.browser.protocol import PdfDocument
from app.browser.sanitize import mask

# File names document portals give every file: not worth reading out as a title.
_GENERIC_NAMES = {"original", "download", "view", "file", "document", "pdf", "attachment", "content", "inline",
                  "preview", "show", "get", "blob", "index", "untitled", "doc", "open", "render", "data"}

MAX_CHARS = 250_000  # about 60k tokens: the whole text of nearly any document a person would call about
SCANNED_CHARS_PER_PAGE = 40
MAX_IMAGE_PAGES = 8


def _title(doc: pymupdf.Document, url: str) -> str:
    title = (doc.metadata or {}).get("title") or ""
    if title.strip() and title.strip().lower() not in _GENERIC_NAMES | {"microsoft word - document1"}:
        return title.strip()[:120]
    return title_from_url(url)


def read(data: bytes, url: str) -> PdfDocument:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        return PdfDocument(title=_title_from_url(url), pages=0, text="", error=f"Not a readable PDF ({e.__class__.__name__}).")
    try:
        if doc.needs_pass:
            return PdfDocument(title=_title(doc, url), pages=doc.page_count, text="",
                               error="This PDF is password-protected.")
        parts: list[str] = []
        chars = 0
        truncated = False
        for i, page in enumerate(doc, 1):
            text = re.sub(r"[ \t]+", " ", page.get_text("text").replace("\u00ad", "")).strip()  # drop soft hyphens
            block = f"[Page {i} of {doc.page_count}]\n{text}"
            if chars + len(block) > MAX_CHARS:
                truncated = True
                break
            parts.append(block)
            chars += len(block)
        text = mask("\n\n".join(parts))
        body_chars = sum(len(p.split("\n", 1)[-1]) for p in parts)
        scanned = doc.page_count > 0 and body_chars < SCANNED_CHARS_PER_PAGE * doc.page_count
        images = _render(doc) if scanned else []
        return PdfDocument(title=_title(doc, url), pages=doc.page_count, text=text, truncated=truncated,
                           scanned=scanned, images=images)
    finally:
        doc.close()


def _render(doc: pymupdf.Document) -> list[str]:
    out = []
    for page in list(doc)[:MAX_IMAGE_PAGES]:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5))
        out.append(base64.b64encode(pix.tobytes("jpeg", jpg_quality=70)).decode("ascii"))
    return out


def _title_from_url(url: str) -> str:
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "document"
    return re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)[:120]


def friendly_name(document: PdfDocument) -> str:
    return f"the PDF “{document.title}”" if document.title else "a PDF"


def title_from_url(url: str) -> str:
    """'https://bucket.s3.amazonaws.com/leases/Unit_3B-Lease.pdf?X-Amz-...' -> 'Unit 3B Lease' (empty if it's a random id)."""
    name = _title_from_url(url).replace("_", " ").replace("-", " ").strip()
    letters = sum(c.isalpha() for c in name)
    if name.lower() in _GENERIC_NAMES or letters < 3 or re.fullmatch(r"[0-9a-f ]{16,}", name.lower()):
        return ""
    return name


def on_screen(image_b64: str, title: str, reason: str) -> PdfDocument:
    return PdfDocument(title=title, images=[image_b64], on_screen_only=reason or "The file couldn't be read.")


def decode(b64: str) -> bytes:
    return base64.b64decode(b64)


def describe(document: Optional[PdfDocument]) -> str:
    """The document part of the page text the model reads."""
    if document is None:
        return ""
    if document.error:
        what = f" ({document.title})" if document.title else ""
        return (f"This tab shows a PDF{what} that can't be read: {document.error} Tell the caller plainly; "
                "if the link expired, opening the document again from the website where they found it will let "
                "you read it.")
    if document.on_screen_only:
        what = f" (“{document.title}”)" if document.title else ""
        return (f"This tab shows a PDF{what}, but the file itself can't be read: {document.on_screen_only} "
                "A picture of the part visible on screen is attached; that is ALL you can see. Answer only from it, "
                "and tell the caller you can only see what's on screen right now. To read the whole document, "
                "they can open it again from the website where they found it (its link will be fresh), and you'll "
                "read every page.")
    head = f"This tab shows a PDF document: “{document.title}”, {document.pages} page{'s' * (document.pages != 1)}."
    if document.scanned:
        shown = min(document.pages, MAX_IMAGE_PAGES)
        return (head + f" It is scanned (no text layer); pictures of the first {shown} page(s) are attached. "
                "Read them carefully.")
    note = " The text below was cut off at the end because the document is very long." if document.truncated else ""
    return f"{head} Its full text follows, with page markers.{note}\n\n{document.text}"
