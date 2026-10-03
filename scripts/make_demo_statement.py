"""Write demo_sites/testbench/statement.pdf: a fictional rent statement laid out as a table, like the charge
summaries document portals produce. Labels and amounts sit in separate columns, so the PDF's text puts them far
apart: answers must still be found and quoted.

    uv run python scripts/make_demo_statement.py
"""

from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent.parent / "demo_sites" / "testbench" / "statement.pdf"

ROWS = [("Rent Income", "$1,767.00"), ("Utilities - Water/Sewer/Trash", "$83.50"), ("Internet Package", "$65.00"),
        ("Parking Space P-14", "$75.00"), ("Pet Rent (1 cat)", "$25.00"), ("Renters Insurance (required)", "$15.00"),
        ("Valet Trash", "$30.00"), ("Amenity Fee", "$20.00")]


def main() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "The University Group", fontsize=16, fontname="hebo")
    page.insert_text((72, 92), "Monthly Charges Summary - Lease 2026-2027", fontsize=11, fontname="helv")
    page.insert_text((72, 110), "Resident: Evan D.   Unit 4C, 1200 College Ave", fontsize=10, fontname="helv")
    page.insert_text((72, 140), "Charge", fontsize=10, fontname="hebo")
    page.insert_text((430, 140), "Monthly Amount", fontsize=10, fontname="hebo")
    y = 160
    for label, _ in ROWS:  # the label column first, then the amount column: extraction keeps them apart
        page.insert_text((72, y), label, fontsize=10, fontname="helv")
        y += 18
    y = 160
    for _, amount in ROWS:
        page.insert_text((450, y), amount, fontsize=10, fontname="helv")
        y += 18
    page.insert_text((72, y + 14), "Total:", fontsize=11, fontname="hebo")
    page.insert_text((450, y + 14), "$2,080.50", fontsize=11, fontname="hebo")
    page.insert_text((72, y + 50), "Charges are due on the 1st of each month. A late fee of $50.00 applies after the 5th.",
                     fontsize=9, fontname="helv")
    page.insert_text((72, y + 64), "Questions: (217) 555-0190 or the resident portal.", fontsize=9, fontname="helv")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
