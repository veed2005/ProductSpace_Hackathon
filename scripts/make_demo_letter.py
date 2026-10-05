"""Print-ready fake Medicaid renewal letter for demo step 3.

  uv run python scripts/make_demo_letter.py                    # deadline 30 days from today
  uv run python scripts/make_demo_letter.py --deadline 2026-11-01

Writes data/demo/medicaid_renewal_letter.pdf (print it and photograph it on stage) and a .png of
the same page (for `scripts/simulate.py --image` and the backup plan). Addressed to the demo
persona Rosa Martínez from docs/DEMO_SCRIPT.md. Clearly marked SAMPLE: never a real notice.
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402


def nice(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


PERSONA = {
    "name": "ROSA MARTINEZ",
    "street": "77 MAPLE AVE APT 3",
    "city": "SPRINGFIELD IL 62703",
    "case": "IL-MED-504318",
}


def build(deadline: date, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)  # US Letter
    ink, grey, red = (0.1, 0.1, 0.12), (0.45, 0.45, 0.48), (0.7, 0.1, 0.1)

    def text(x, y, s, size=11, color=ink, bold=False):
        page.insert_text((x, y), s, fontsize=size, color=color, fontname="hebo" if bold else "helv")

    page.draw_rect(pymupdf.Rect(0, 0, 612, 70), color=None, fill=(0.12, 0.25, 0.55))
    text(48, 32, "STATE DEPARTMENT OF HUMAN SERVICES", 15, (1, 1, 1), bold=True)
    text(48, 52, "Medical Assistance Programs  ·  P.O. Box 0000, Springfield", 10, (0.85, 0.9, 1))
    text(400, 100, f"Notice date: {nice(date.today())}", 10, grey)
    text(400, 115, f"Case number: {PERSONA['case']}", 10, grey)

    y = 150
    for line in (PERSONA["name"], PERSONA["street"], PERSONA["city"]):
        text(48, y, line, 11)
        y += 15

    text(48, 225, "IMPORTANT: Your Medicaid coverage must be renewed", 16, bold=True)
    body = [
        "We are reviewing your household's eligibility for Medicaid. To keep your coverage,",
        "you must complete and return the enclosed renewal form.",
        "",
        f"Return the renewal form by {nice(deadline)}.",
        "",
        "If we do not receive your renewal form by this date, your Medicaid coverage and",
        "the coverage of the children in your household may end.",
        "",
        "What you need to do:",
        "  1. Fill out the Medicaid Renewal form.",
        "  2. Include proof of income for the last 30 days (pay stubs or a letter from your employer).",
        "  3. Return it by mail, fax, or at your local office.",
        "",
        "Questions? Call your caseworker. If you need help in another language, ask for an",
        "interpreter at no cost to you.",
    ]
    y = 260
    for line in body:
        bold = line.startswith("Return the renewal form by") or line == "What you need to do:"
        text(48, y, line, 11.5, red if line.startswith("Return the renewal") else ink, bold=bold)
        y += 18

    page.draw_rect(pymupdf.Rect(48, 680, 564, 730), color=red, width=1.5)
    text(60, 702, "SAMPLE FOR A HACKATHON DEMO. NOT A REAL GOVERNMENT NOTICE.", 12, red, bold=True)
    text(60, 720, "Fictional person, case number, and agency.", 10, red)

    pdf_path = out_dir / "medicaid_renewal_letter.pdf"
    png_path = out_dir / "medicaid_renewal_letter.png"
    doc.save(pdf_path)
    page.get_pixmap(dpi=130).save(png_path)
    doc.close()
    return pdf_path, png_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deadline", type=date.fromisoformat, default=date.today() + timedelta(days=30))
    args = ap.parse_args()
    pdf, png = build(args.deadline, get_settings().data_dir / "demo")
    print(f"PDF (print this): {pdf}\nPNG (for --image): {png}")


if __name__ == "__main__":
    main()
