"""Write demo_sites/testbench/lease.pdf: a fictional 6-page residential lease for the PDF-reading demo.

    uv run python scripts/make_demo_pdf.py

The answers to likely questions are spread across pages (pets on page 4, early termination on page 5,
the signature block on page 6), so a run only passes if the whole document was read.
"""

from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent.parent / "demo_sites" / "testbench" / "lease.pdf"

PAGES = [
    ("RESIDENTIAL LEASE AGREEMENT", [
        "This Residential Lease Agreement (the \"Lease\") is made on September 15, 2026, between Riverside "
        "Property Management LLC (\"Landlord\") and Margaret Ellis (\"Tenant\").",
        "1. PREMISES. Landlord leases to Tenant the apartment at 418 Willow Street, Unit 3B, Springfield, "
        "Illinois 62704, together with one assigned parking space (No. 17).",
        "2. TERM. The Lease begins on October 1, 2026 and ends on September 30, 2027. After that, it continues "
        "month to month unless either party gives 60 days' written notice.",
        "This Lease is a fictional document created for software demonstrations. It is not legal advice.",
    ]),
    ("RENT AND PAYMENTS", [
        "3. RENT. Monthly rent is $1,450.00, due on the first day of each month. Rent may be paid by check, "
        "money order, or through the online resident portal.",
        "4. LATE FEES. If rent is not received by 11:59 p.m. on the fifth day of the month, Tenant will owe a "
        "late fee of $75.00, plus $10.00 per day after that, up to a maximum of $150.00 per month.",
        "5. RETURNED PAYMENTS. A $35.00 fee applies to any returned check or failed electronic payment.",
        "6. SECURITY DEPOSIT. Tenant has paid a security deposit of $1,450.00. Landlord will return the deposit, "
        "less itemized deductions for damage beyond normal wear and tear, within 30 days after Tenant moves out.",
    ]),
    ("UTILITIES, MAINTENANCE, AND ENTRY", [
        "7. UTILITIES. Landlord pays water, sewer, and trash. Tenant pays electricity, gas, and internet.",
        "8. MAINTENANCE. Tenant must report needed repairs in writing or through the resident portal. Landlord "
        "will make non-emergency repairs within 10 business days. For emergencies (no heat, flooding, gas smell), "
        "call the 24-hour line at (217) 555-0142.",
        "9. ENTRY. Landlord may enter the Premises with at least 24 hours' notice for inspections, repairs, or "
        "showings, and without notice in an emergency.",
        "10. ALTERATIONS. Tenant may not paint, remove fixtures, or install locks without Landlord's written "
        "permission. Small nail holes for pictures are allowed.",
    ]),
    ("PETS, GUESTS, AND SMOKING", [
        "11. PETS. Tenant may keep up to two cats or one dog weighing under 40 pounds, with Landlord's prior "
        "written approval. Tenant must pay a one-time pet deposit of $300.00 and monthly pet rent of $25.00 per "
        "pet. Reptiles, birds of prey, and aggressive breeds listed in Addendum B are not allowed. Service and "
        "assistance animals are not pets and are not charged a deposit or pet rent.",
        "12. GUESTS. Guests may stay up to 14 days in any six-month period. Longer stays require Landlord's "
        "written consent and may require the guest to apply as an occupant.",
        "13. SMOKING. Smoking and vaping of any substance are prohibited inside the building and within 25 feet "
        "of any entrance.",
    ]),
    ("ENDING THE LEASE EARLY", [
        "14. EARLY TERMINATION. Tenant may end this Lease before September 30, 2027 by giving 60 days' written "
        "notice and paying an early termination fee equal to one month's rent ($1,450.00). The fee is waived if "
        "Tenant is called to active military duty or is a victim of domestic violence, as provided by Illinois law.",
        "15. SUBLETTING. Tenant may not sublet or assign the Lease without Landlord's written consent, which will "
        "not be unreasonably withheld.",
        "16. RENEWAL. Landlord will offer renewal terms at least 75 days before the Lease ends. Any rent increase "
        "will be stated in the renewal offer.",
    ]),
    ("SIGNATURES", [
        "17. ENTIRE AGREEMENT. This Lease, with Addendum A (Parking Rules) and Addendum B (Pet Policy), is the "
        "entire agreement between the parties.",
        "Landlord: Riverside Property Management LLC, by Dana Whitfield, Property Manager. Date: September 15, 2026.",
        "Tenant: Margaret Ellis. Date: September 15, 2026.",
    ]),
]


def main() -> None:
    doc = pymupdf.open()
    doc.set_metadata({"title": "Residential Lease Agreement - 418 Willow St Unit 3B", "author": "Riverside Property Management"})
    for n, (heading, paragraphs) in enumerate(PAGES, 1):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 80), heading, fontsize=15, fontname="hebo")
        y = 110
        for para in paragraphs:
            rect = pymupdf.Rect(72, y, 540, 760)
            left = page.insert_textbox(rect, para, fontsize=11, fontname="helv", lineheight=1.35)
            used = rect.height - left
            y += used + 14
        page.insert_text((72, 770), f"Page {n} of {len(PAGES)}   Lease for 418 Willow St, Unit 3B", fontsize=8,
                         color=(0.4, 0.4, 0.4))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    print(f"wrote {OUT} ({len(PAGES)} pages)")


if __name__ == "__main__":
    main()
