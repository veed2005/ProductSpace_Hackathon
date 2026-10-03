"""Generate forms/sample_benefits/form.pdf: a small fillable placeholder form.

Lets everyone work on the full pipeline before the real SNAP/Medicaid/school-meals PDFs
are in. Run: uv run python scripts/make_sample_form.py
"""

import sys
from pathlib import Path

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.engines.form_library import load_schema, pdf_path  # noqa: E402

FORM_ID = "sample_benefits"


def main() -> None:
    schema = load_schema(FORM_ID)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Sample Benefits Application (placeholder)", fontsize=16)
    y = 90
    for field in schema.fields:
        if not field.pdf_field:
            continue
        page.insert_text((50, y + 13), field.label, fontsize=10)
        w = fitz.Widget()
        w.field_name = field.pdf_field
        if field.type == "yes_no":
            w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
            w.rect = fitz.Rect(300, y, 316, y + 16)
        else:
            w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            w.rect = fitz.Rect(300, y, 550, y + 18)
            w.text_fontsize = 10
        page.add_widget(w)
        y += 30
    out = pdf_path(FORM_ID)
    doc.save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
