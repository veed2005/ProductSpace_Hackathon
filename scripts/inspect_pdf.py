"""Print every AcroForm field in a PDF: name, type, options, checkbox on-values, page, rect.

Use it to check that a candidate form is fillable and to find pdf_field names for a schema.
Run: uv run python scripts/inspect_pdf.py forms/sample_benefits/form.pdf [--json]
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.pdf.fields import FF_READ_ONLY, FF_REQUIRED, decode_name, has_xfa, list_fields_in  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    doc = fitz.open(args.pdf)
    try:
        fields = list_fields_in(doc)
        xfa = has_xfa(doc)
        pages = doc.page_count
    finally:
        doc.close()

    if args.json:
        print(json.dumps({"pages": pages, "xfa": xfa, "fields": [asdict(f) for f in fields]}, indent=2))
        return

    print(f"{args.pdf}: {pages} page(s), {len(fields)} field(s){', has XFA' if xfa else ''}")
    if not fields:
        print("This PDF has no fillable fields (flat or scanned form).")
        return
    if xfa:
        print("Note: XFA form. fill_pdf removes the XFA layer so viewers show the AcroForm values.")
    for f in fields:
        flags = [n for n, bit in (("required", FF_REQUIRED), ("read-only", FF_READ_ONLY)) if f.flags & bit]
        if f.multiline:
            flags.append("multiline")
        if f.comb:
            flags.append("comb")
        print(f"\n{f.name}")
        print(f"  type: {f.type}" + (f"  [{', '.join(flags)}]" if flags else ""))
        if f.type in ("checkbox", "radio"):
            print(f"  on-values: {', '.join(decode_name(s) for s in f.on_states)}")
        elif f.options:
            print(f"  options: {', '.join(f.options)}")
        if f.max_length:
            print(f"  max length: {f.max_length}")
        if f.value and f.value != "Off":
            print(f"  current value: {f.value!r}")
        for w in f.widgets:
            rect = ", ".join(f"{v:.0f}" for v in w.rect)
            size = f"{w.font_size:g}pt" if w.font_size else "auto"
            on = f"  on={decode_name(w.on_state)}" if w.on_state else ""
            print(f"  page {w.page + 1}  rect ({rect})  font {w.font} {size}{on}")


if __name__ == "__main__":
    main()
