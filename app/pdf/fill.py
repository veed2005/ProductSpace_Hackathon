"""Fill AcroForm PDFs. Owner: Lane C."""

import logging
from pathlib import Path

import pymupdf as fitz

from app.pdf.fields import OFF, PdfField, button_state, choice_value, has_xfa, list_fields_in

log = logging.getLogger(__name__)


def fill_pdf(template: Path, values: dict[str, str], out_path: Path) -> Path:
    """Write {pdf_field_name: value} into a copy of the template. Unknown fields are ignored.

    - Text: written as given.
    - Checkbox: "yes"/"true"/"on"/"x"/the widget's own on-state check it; "no"/"off"/"" clear it.
      The real on-state is read from the PDF, so a schema can say "Yes" even if the box uses "On".
    - Radio: the value names the option's on-state (case-insensitive); the others are cleared.
    - Choice: matched to the option list case-insensitively.
    Values that can't be mapped are left unset and show up as mismatches in verify_pdf.
    Appearances are regenerated so the values show in every viewer.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(template)
    try:
        fields = {f.name: f for f in list_fields_in(doc)}
        for page in doc:
            # Clear radio/checkbox widgets before setting the selected one, so the group's
            # value ends up on the selected option.
            widgets = sorted(page.widgets(), key=lambda w: _is_selected(w, fields, values))
            for widget in widgets:
                name = widget.field_name
                if name in values and name in fields:
                    _set(doc, widget, fields[name], values[name])
        if has_xfa(doc):
            # XFA-aware viewers (Acrobat) would show the empty XFA layer instead of our values.
            doc.xref_set_key(doc.pdf_catalog(), "AcroForm/XFA", "null")
        doc.need_appearances(True)
        doc.save(out_path, garbage=1)
    finally:
        doc.close()
    return out_path


def _is_selected(widget: fitz.Widget, fields: dict[str, PdfField], values: dict[str, str]) -> bool:
    f = fields.get(widget.field_name)
    if f is None or not f.is_button or widget.field_name not in values:
        return False
    on = widget.on_state()
    return bool(on) and button_state(f, values[widget.field_name]) == on


def _set(doc: fitz.Document, widget: fitz.Widget, f: PdfField, value: str) -> None:
    if f.is_button:
        state = button_state(f, value)
        if state is None:
            log.warning("fill_pdf: %r is not a valid state for %s %r", value, f.type, f.name)
            return
        mine = widget.on_state() if state == widget.on_state() else OFF
        widget.field_value = mine
        _update(widget)
        _write_state(doc, widget.xref, mine, state)
        return
    if f.type in ("combobox", "listbox"):
        widget.field_value = choice_value(f, value)
    elif f.type == "text":
        widget.field_value = str(value)
    else:
        return  # push buttons, signatures: nothing to fill
    _update(widget)


def _write_state(doc: fitz.Document, xref: int, widget_state: str, field_state: str) -> None:
    """Write the button's /AS and the field's /V as the PDF names exactly as stored.

    pymupdf decodes escaped names when it writes them: "Hispanic#2FLatino" becomes
    /Hispanic/Latino, which no longer matches the appearance and leaves the button blank.
    """
    doc.xref_set_key(xref, "AS", f"/{widget_state}")
    # A widget without its own /T is a kid of the field (a radio option); otherwise the
    # widget is the field and /Parent is just a node in the name tree (e.g. form1[0]).
    kind, parent = doc.xref_get_key(xref, "Parent")
    own_name = doc.xref_get_key(xref, "T")[0] != "null"
    holder = int(parent.split()[0]) if kind == "xref" and not own_name else xref
    if widget_state != OFF or field_state == OFF:
        doc.xref_set_key(holder, "V", f"/{field_state}")


def _update(widget: fitz.Widget) -> None:
    """Regenerate the appearance. Fall back to Helvetica if the form's font isn't usable."""
    try:
        widget.update()
    except Exception:
        log.warning("fill_pdf: appearance update failed for %r, retrying with Helv", widget.field_name)
        widget.text_font = "Helv"
        widget.update()
