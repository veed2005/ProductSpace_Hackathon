"""A form's own text, and the check that a quote really is in it. Shared by explanations and notices.

A model may say the form says something; Formline repeats it as the form's words only if `find_quote` finds it
in the PDF text (case, spacing, quote marks and hyphenation aside).
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Optional

import pymupdf

from app.engines import form_library

_STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "is", "are", "be", "you", "your", "this",
         "that", "it", "if", "do", "does", "what", "why", "how", "my", "i", "me", "with", "by", "as", "at", "from",
         "el", "la", "los", "las", "de", "del", "que", "y", "o", "en", "por", "para", "es", "mi", "me", "un", "una",
         "se", "lo", "su", "sus", "con", "qué", "por qué"}


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").replace("’", "'").replace("‘", "'").replace("“", '"')
    t = t.replace("”", '"').replace("–", "-").replace("—", "-")
    t = re.sub(r"-\s*\n\s*", "", t)  # words hyphenated across lines
    t = re.sub(r"[^\w\s$%'.,]", " ", t.casefold())
    return " ".join(t.split())


@lru_cache(maxsize=16)
def _pages(path: str, mtime: float) -> tuple[str, ...]:
    with pymupdf.open(path) as doc:
        return tuple(page.get_text() for page in doc)


def pages(form_id: str) -> list[str]:
    path = form_library.pdf_path(form_id)
    if not path.exists():
        return []
    return list(_pages(str(path), path.stat().st_mtime))


def find_quote(quote: Optional[str], page_texts: list[str]) -> Optional[int]:
    """1-based page where every piece of the quote appears (pieces may be joined with '...'). None if any piece
    is missing, or the quote is too short to mean anything."""
    if not quote:
        return None
    pieces = [norm(p).strip(" .,") for p in re.split(r"\.\.\.|…", quote)]
    pieces = [p for p in pieces if p]
    if not pieces or sum(len(p) for p in pieces) < 12:
        return None
    normalized = [norm(p) for p in page_texts]
    whole = " ".join(normalized)
    if not all(p in whole for p in pieces):
        return None
    for i, page in enumerate(normalized, start=1):
        if pieces[0] in page:
            return i
    return 1


def relevant(form_id: str, query: str, *, limit_chars: int = 6000) -> str:
    """The form's paragraphs that share the most words with the query, best first, up to limit_chars."""
    texts = pages(form_id)
    terms = {w for w in re.findall(r"\w+", norm(query)) if w not in _STOP and len(w) > 2}
    chunks: list[tuple[int, int, str]] = []
    for page_no, text in enumerate(texts, start=1):
        for para in re.split(r"\n\s*\n", text):
            para = " ".join(para.split())
            if len(para) < 40:
                continue
            score = sum(1 for w in set(re.findall(r"\w+", norm(para))) if w in terms)
            if score:
                chunks.append((score, page_no, para))
    chunks.sort(key=lambda c: -c[0])
    out, used = [], 0
    for _, page_no, para in chunks:
        if used + len(para) > limit_chars:
            break
        out.append(f"[page {page_no}] {para}")
        used += len(para)
    return "\n\n".join(out)
