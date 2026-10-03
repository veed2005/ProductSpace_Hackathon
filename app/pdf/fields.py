"""Read AcroForm fields out of a PDF. Owner: Lane C.

Shared by fill, verify, scripts/inspect_pdf.py and (later) ingestion. A "field" here is one
logical form field: a radio group, or a text field repeated on several pages, is one PdfField
with several widgets.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz

OFF = "Off"

# How a checkbox value from a schema or a person gets mapped to checked / unchecked.
# Anything else is left as-is so verification flags it instead of guessing.
CHECKED_WORDS = {"yes", "y", "true", "on", "x", "1", "checked"}
UNCHECKED_WORDS = {"no", "n", "false", "off", "0", "", "unchecked"}

# PDF field flags (PDF 1.7 spec, table 226 / 228 / 230).
FF_READ_ONLY = 1 << 0
FF_REQUIRED = 1 << 1
FF_MULTILINE = 1 << 12
FF_COMB = 1 << 24
FF_EDIT = 1 << 18  # combo box accepts free text

_TYPES = {
    fitz.PDF_WIDGET_TYPE_TEXT: "text",
    fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
    fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
    fitz.PDF_WIDGET_TYPE_COMBOBOX: "combobox",
    fitz.PDF_WIDGET_TYPE_LISTBOX: "listbox",
    fitz.PDF_WIDGET_TYPE_BUTTON: "button",
    fitz.PDF_WIDGET_TYPE_SIGNATURE: "signature",
}


@dataclass
class WidgetInfo:
    page: int  # 0-based
    rect: tuple[float, float, float, float]
    on_state: str | None  # checkbox / radio only
    font: str
    font_size: float  # 0 means auto-size
    border_width: float
    xref: int = 0  # PDF object number of the widget


@dataclass
class PdfField:
    name: str
    type: str  # text | checkbox | radio | combobox | listbox | button | signature | unknown
    value: str  # checkbox/radio: the selected on-state, or "Off"
    options: list[str] = field(default_factory=list)  # choice options, or button on-states
    max_length: int | None = None
    flags: int = 0
    widgets: list[WidgetInfo] = field(default_factory=list)

    @property
    def on_states(self) -> list[str]:
        return [w.on_state for w in self.widgets if w.on_state]

    @property
    def multiline(self) -> bool:
        return bool(self.flags & FF_MULTILINE)

    @property
    def comb(self) -> bool:
        return bool(self.flags & FF_COMB)

    @property
    def editable_choice(self) -> bool:
        return self.type == "combobox" and bool(self.flags & FF_EDIT)

    @property
    def is_button(self) -> bool:
        return self.type in ("checkbox", "radio")


def _appearance_state(doc: fitz.Document, xref: int) -> str:
    kind, val = doc.xref_get_key(xref, "AS")
    return val.lstrip("/") if kind == "name" else OFF


def list_fields(path: Path) -> list[PdfField]:
    """Every AcroForm field in document order, widgets of the same name merged."""
    doc = fitz.open(path)
    try:
        return list_fields_in(doc)
    finally:
        doc.close()


def list_fields_in(doc: fitz.Document) -> list[PdfField]:
    fields: dict[str, PdfField] = {}
    for page in doc:
        for w in page.widgets():
            ftype = _TYPES.get(w.field_type, "unknown")
            f = fields.get(w.field_name)
            if f is None:
                f = fields[w.field_name] = PdfField(
                    name=w.field_name,
                    type=ftype,
                    value=OFF if ftype in ("checkbox", "radio") else _as_text(w.field_value),
                    options=[_as_text(o) for o in (w.choice_values or [])],
                    max_length=w.text_maxlen or None,
                    flags=w.field_flags or 0,
                )
            on = w.on_state() if ftype in ("checkbox", "radio") else None
            if on:
                if on not in f.options:
                    f.options.append(on)
                # xref_get_key returns names decoded ("Hispanic/Latino"), on_state() does not.
                if decode_name(_appearance_state(doc, w.xref)) == decode_name(on):
                    f.value = on
            f.widgets.append(WidgetInfo(
                page=page.number,
                rect=tuple(w.rect),
                on_state=on,
                font=w.text_font or "Helv",
                font_size=w.text_fontsize or 0.0,
                border_width=w.border_width or 0.0,
                xref=w.xref,
            ))
    return list(fields.values())


def has_xfa(doc: fitz.Document) -> bool:
    return doc.xref_get_key(doc.pdf_catalog(), "AcroForm/XFA")[0] != "null"


def _as_text(value) -> str:
    if value is None or value is False:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def checkbox_state(value: str, on_state: str) -> str | None:
    """Map a checkbox value to on_state or "Off". None if the value is not recognizable."""
    v = str(value).strip()
    if v.casefold() in (on_state.casefold(), decode_name(on_state).casefold()) or v.casefold() in CHECKED_WORDS:
        return on_state
    if v.casefold() in UNCHECKED_WORDS:
        return OFF
    return None


def decode_name(name: str) -> str:
    """PDF names escape characters as #xx: "Not#20Hispanic#2FLatino" -> "Not Hispanic/Latino"."""
    return re.sub(r"#([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), name)


def button_state(f: PdfField, value: str) -> str | None:
    """Map a value to one of a checkbox/radio field's states ("Off" included). None if no match.

    Accepts the on-state as stored in the PDF or in readable form ("Hispanic/Latino" for
    "Hispanic#2FLatino"); always returns the stored form.
    """
    v = str(value).strip().casefold()
    for on in f.on_states:
        if v in (on.casefold(), decode_name(on).casefold()):
            return on
    if f.type == "checkbox" and len(set(f.on_states)) == 1:
        return checkbox_state(value, f.on_states[0])
    if v in UNCHECKED_WORDS:
        return OFF
    return None


def choice_value(f: PdfField, value: str) -> str:
    """Match a choice value to the field's option case-insensitively; unchanged if none match."""
    for opt in f.options:
        if opt.casefold() == str(value).strip().casefold():
            return opt
    return str(value)
