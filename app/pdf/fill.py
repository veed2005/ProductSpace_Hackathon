"""Fill AcroForm PDFs. Owner: Lane C."""

from pathlib import Path

import pymupdf as fitz


def fill_pdf(template: Path, values: dict[str, str], out_path: Path) -> Path:
    """Write {pdf_field_name: value} into a copy of the template. Unknown fields are ignored."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(template)
    try:
        for page in doc:
            for widget in page.widgets():
                if widget.field_name in values:
                    widget.field_value = values[widget.field_name]
                    widget.update()
        doc.save(out_path)
    finally:
        doc.close()
    return out_path
