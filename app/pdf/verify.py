"""Re-read a filled PDF and check it against what we meant to write. Owner: Lane C."""

from pathlib import Path

import pymupdf as fitz

from app.contracts import FieldMismatch, VerificationResult


def read_fields(path: Path) -> dict[str, str]:
    doc = fitz.open(path)
    try:
        return {w.field_name: str(w.field_value or "") for page in doc for w in page.widgets()}
    finally:
        doc.close()


def verify_pdf(path: Path, expected: dict[str, str]) -> VerificationResult:
    """Compare every intended {pdf_field: value} with what the PDF actually contains.

    TODO(Lane C): truncation check (text wider than the box), missing required fields.
    """
    actual = read_fields(path)
    mismatches = [
        FieldMismatch(pdf_field=name, expected=want, actual=actual.get(name, "<missing field>"))
        for name, want in expected.items()
        if actual.get(name) != want
    ]
    return VerificationResult(ok=not mismatches, fields_checked=len(expected), mismatches=mismatches)
