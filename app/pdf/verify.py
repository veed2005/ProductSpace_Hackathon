"""Re-read a filled PDF and check it against what we meant to write. Owner: Lane C."""

from collections.abc import Iterable
from pathlib import Path

import pymupdf as fitz

from app.contracts import FieldMismatch, FormField, FormSchema, VerificationResult
from app.pdf.fields import OFF, PdfField, WidgetInfo, button_state, list_fields

MISSING = "<missing field>"
MIN_AUTO_FONT_SIZE = 6.0  # auto-sized text squeezed below this is unreadable: treat as truncated
LINE_HEIGHT = 1.15  # multiline text: line spacing as a multiple of font size

# Widget font names -> base-14 fonts that pymupdf can measure. Unknown fonts are measured
# as Helvetica, which is close enough for a "does it fit" check.
_FONTS = {"helv": "helv", "hebo": "hebo", "tiro": "tiro", "tibo": "tibo", "cour": "cour",
          "cobo": "cobo", "times": "tiro", "courier": "cour", "helvetica": "helv", "arial": "helv"}


def read_fields(path: Path) -> dict[str, str]:
    """{pdf_field: current value}. Checkboxes and radio groups read as their on-state or "Off"."""
    return {f.name: f.value for f in list_fields(path)}


def verify_pdf(path: Path, expected: dict[str, str],
               required: FormSchema | Iterable[str] | None = None) -> VerificationResult:
    """Compare every intended {pdf_field: value} with what the PDF actually contains.

    Checks:
    - mismatches: the stored value differs from the intended one (checkboxes and radios are
      compared by state, so "yes" matches a box that is checked with any on-state; a choice
      value that isn't one of the field's options is a mismatch).
    - truncated: text longer than the field's max length (PDF MaxLen or schema max_length), or
      wider/taller than its box at the field's font size.
    - missing_required: required fields left empty. Pass the FormSchema (reports field ids;
      conditional fields are only required when their condition holds in the PDF; checkboxes
      are never "missing" because unchecked means "no"), or a list of pdf field names (reports
      those names; an unchecked box counts as empty).
    ok is True only when all three lists are empty.
    """
    fields = {f.name: f for f in list_fields(path)}
    schema = required if isinstance(required, FormSchema) else None
    max_lengths = {f.pdf_field: f.max_length for f in schema.fields if f.pdf_field} if schema else {}

    mismatches: list[FieldMismatch] = []
    truncated: list[str] = []
    for name, want in expected.items():
        f = fields.get(name)
        if f is None:
            mismatches.append(FieldMismatch(pdf_field=name, expected=str(want), actual=MISSING))
            continue
        mismatch = _compare(f, str(want))
        if mismatch:
            mismatches.append(mismatch)
        elif f.type == "text" and _truncated(f, max_lengths.get(name)):
            truncated.append(name)

    if schema:
        missing = _missing_from_schema(schema, fields)
    elif required is not None:
        missing = [name for name in required if name not in fields or _is_empty(fields[name])]
    else:
        missing = []

    return VerificationResult(
        ok=not (mismatches or truncated or missing),
        fields_checked=len(expected),
        mismatches=mismatches,
        truncated=truncated,
        missing_required=missing,
    )


def _compare(f: PdfField, want: str) -> FieldMismatch | None:
    if f.is_button:
        target = button_state(f, want)
        if target is None or target != f.value:
            return FieldMismatch(pdf_field=f.name, expected=want, actual=f.value)
        return None
    if f.type in ("combobox", "listbox") and not f.editable_choice and f.options:
        if f.value not in f.options:
            return FieldMismatch(pdf_field=f.name, expected=want, actual=f"{f.value} (not an option)")
        if f.value.casefold() != want.strip().casefold():
            return FieldMismatch(pdf_field=f.name, expected=want, actual=f.value)
        return None
    if f.value != want:
        return FieldMismatch(pdf_field=f.name, expected=want, actual=f.value)
    return None


def _truncated(f: PdfField, schema_max: int | None) -> bool:
    text = f.value
    if not text:
        return False
    for limit in (f.max_length, schema_max):
        if limit and len(text) > limit:
            return True
    if f.comb:
        return False  # one character per cell: MaxLen above is the real limit
    return any(not _fits(text, w, f.multiline) for w in f.widgets)


def _fits(text: str, w: WidgetInfo, multiline: bool) -> bool:
    x0, y0, x1, y1 = w.rect
    pad = 2 * (w.border_width + 1)
    width, height = (x1 - x0) - pad, (y1 - y0) - pad
    if width <= 0 or height <= 0:
        return False
    font = _FONTS.get(w.font.lower(), "helv")
    if not multiline:
        length_1pt = fitz.get_text_length(text, fontname=font, fontsize=1)
        if w.font_size == 0:  # auto-size: viewer shrinks the text to fit the box
            return length_1pt == 0 or width / length_1pt >= MIN_AUTO_FONT_SIZE
        return length_1pt * w.font_size <= width
    size = w.font_size or MIN_AUTO_FONT_SIZE
    lines = _wrap_count(text, font, size, width)
    return lines * size * LINE_HEIGHT <= height


def _wrap_count(text: str, font: str, size: float, width: float) -> int:
    """Lines needed to show text with greedy word wrap (long words break mid-word)."""
    lines = 0
    for paragraph in text.splitlines() or [""]:
        lines += 1
        current = 0.0
        space = fitz.get_text_length(" ", fontname=font, fontsize=size)
        for word in paragraph.split(" "):
            wlen = fitz.get_text_length(word, fontname=font, fontsize=size)
            if current and current + space + wlen <= width:
                current += space + wlen
                continue
            if current:
                lines += 1
            while wlen > width:  # word longer than a line
                lines += 1
                wlen -= width
            current = wlen
    return lines


def _is_empty(f: PdfField) -> bool:
    return f.value in ("", OFF) if f.is_button else not f.value.strip()


def _missing_from_schema(schema: FormSchema, fields: dict[str, PdfField]) -> list[str]:
    by_id = {sf.id: sf for sf in schema.fields}
    missing = []
    for sf in schema.fields:
        if not sf.required or not sf.pdf_field:
            continue
        f = fields.get(sf.pdf_field)
        if f is not None and f.type == "checkbox":
            continue
        if sf.condition and not _condition_holds(sf, by_id, fields):
            continue
        if f is None or _is_empty(f):
            missing.append(sf.id)
    return missing


def _condition_holds(sf: FormField, by_id: dict[str, FormField], fields: dict[str, PdfField]) -> bool:
    """Evaluate the field's condition against what the PDF contains.

    A condition that can't be checked from the PDF (unknown field, not written to the PDF)
    counts as not holding, so we never flag a field the person may rightly have skipped.
    """
    dep = by_id.get(sf.condition.field)
    if dep is None or not dep.pdf_field or dep.pdf_field not in fields:
        return False
    actual = fields[dep.pdf_field]
    equals = str(sf.condition.equals)
    target = dep.pdf_values.get(equals, equals)
    if actual.is_button:
        return button_state(actual, target) == actual.value
    return actual.value.strip().casefold() == target.strip().casefold()
