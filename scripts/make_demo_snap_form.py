"""Generate forms/demo_snap: a FAKE county SNAP application for the phone-form demo.

It's small (10 questions, one monthly-income field so "300 a week" is converted on the call), explains its own
questions, and has a rights-and-responsibilities page with the kinds of clauses Formline should point out,
including one that is broader than the form needs. Fictional county, fictional agency.

Writes form.pdf, schema.json, meta.json, notices.json (reviewed findings; quotes are checked against the PDF
at runtime) and i18n.es.json (Spanish questions and labels).

    uv run python scripts/make_demo_snap_form.py
"""

import hashlib
import json
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(__file__).resolve().parent.parent / "forms" / "demo_snap"
NAME = "Riverbend County SNAP Application"

FIELDS = [
    dict(id="applicant_name", label="Full name", type="text", required=True, profile_key="full_name", group="About you",
         question_hint="What is your full name?",
         es=("¿Cuál es su nombre completo?", "Nombre completo")),
    dict(id="date_of_birth", label="Date of birth", type="date", required=True, profile_key="date_of_birth",
         group="About you", question_hint="What is your date of birth?",
         es=("¿Cuál es su fecha de nacimiento?", "Fecha de nacimiento")),
    dict(id="address", label="Home address", type="address", required=True, profile_key="address", group="About you",
         question_hint="What is your home address, including the city and ZIP code?",
         es=("¿Cuál es su domicilio, con la ciudad y el código postal?", "Domicilio")),
    dict(id="phone", label="Phone number", type="phone", required=False, profile_key="phone", group="About you",
         question_hint="What is the best phone number to reach you?",
         es=("¿A qué número de teléfono le podemos llamar?", "Teléfono")),
    dict(id="ssn_last4", label="Last 4 of Social Security number", type="ssn_last4", required=False,
         profile_key="ssn_last4", sensitive=True, group="About you",
         question_hint="What are the last four digits of your Social Security number? You can skip this.",
         es=("¿Cuáles son los últimos cuatro dígitos de su Seguro Social? Puede omitirlo.",
             "Últimos 4 del Seguro Social")),
    dict(id="household_size", label="People in household", type="number", required=True, profile_key="household_size",
         group="Household", question_hint="Including you, how many people live in your home?",
         es=("Contándole a usted, ¿cuántas personas viven en su casa?", "Personas en el hogar")),
    dict(id="employed", label="Anyone working", type="yes_no", required=True, group="Income",
         question_hint="Does anyone in your home have a job right now?", pdf_values={"yes": "Yes", "no": "Off"},
         es=("¿Alguien en su casa tiene trabajo ahora?", "Alguien trabaja")),
    dict(id="employer", label="Employer", type="text", required=True, profile_key="employment.employer",
         group="Income", condition={"field": "employed", "equals": "yes"},
         question_hint="Who do they work for?", es=("¿Para quién trabaja?", "Empleador")),
    dict(id="monthly_income", label="Monthly income from work", type="money", required=True, period="month",
         profile_key="monthly_income", group="Income", condition={"field": "employed", "equals": "yes"},
         question_hint="About how much does your household earn from work, before taxes?",
         es=("¿Más o menos cuánto gana su hogar por trabajo, antes de impuestos?", "Ingreso mensual por trabajo")),
    dict(id="rent_amount", label="Monthly rent", type="money", required=True, period="month",
         profile_key="housing_cost", group="Housing costs", question_hint="How much is your rent each month?",
         es=("¿Cuánto paga de renta al mes?", "Renta mensual")),
]

EXPLAINERS = [
    "About these questions",
    "Household means the people who live with you and usually buy and prepare food together. A roommate who buys "
    "and cooks food separately is not part of your household.",
    "Household income means the total money that everyone in your household earns from work before taxes, "
    "including wages, tips, and self-employment. We use it to decide whether you qualify and how much you receive.",
    "We ask for your rent because housing costs can increase your monthly benefit. If you leave it blank, we "
    "cannot count it.",
    "Giving the last four digits of your Social Security number helps us find your records faster. You can apply "
    "without it.",
]

RIGHTS = [
    "Rights and responsibilities",
    "I certify under penalty of perjury that the information on this application is true and complete to the best "
    "of my knowledge.",
    "By signing, you authorize the Riverbend County Department of Human Services to verify your income with your "
    "employer, banks, and other agencies.",
    "If you receive benefits you were not eligible for, you will have to pay them back, and you may be disqualified "
    "from the program.",
    "You must report changes in income of more than $100 within 10 days.",
    "The information on this application may also be shared with Riverbend Partner Network members for marketing "
    "of additional services and for research.",
    "We will decide on your application within 30 days.",
]

FINDINGS = [
    dict(category="data_sharing", severity="high", unusual=True,
         quote="may also be shared with Riverbend Partner Network members for marketing of additional services and "
               "for research",
         explanation="This section lets the county share your application information with other organizations for "
                     "marketing and research, not just to process your application.",
         reason="It goes beyond what's needed to decide your benefits, so you may want to ask your caseworker "
                "whether you can say no to it.",
         es=("Esta sección permite que el condado comparta la información de su solicitud con otras organizaciones "
             "para publicidad e investigación, no solo para procesar su solicitud.",
             "Va más allá de lo necesario para decidir sus beneficios, así que tal vez quiera preguntarle a su "
             "trabajador del caso si puede negarse.")),
    dict(category="third_party_contact", severity="medium", unusual=False,
         quote="you authorize the Riverbend County Department of Human Services to verify your income with your "
               "employer, banks, and other agencies",
         explanation="By signing, you allow the county to check your income with your employer, your bank and other "
                     "agencies.",
         reason="They may contact your employer or bank to confirm what you told them.",
         es=("Al firmar, usted permite que el condado verifique sus ingresos con su empleador, su banco y otras "
             "agencias.", "Pueden comunicarse con su empleador o su banco para confirmar lo que usted dijo.")),
    dict(category="perjury_certification", severity="medium", unusual=False,
         quote="I certify under penalty of perjury that the information on this application is true and complete",
         explanation="You are promising, under penalty of perjury, that everything on the application is true.",
         reason="Giving false information on purpose can lead to legal penalties.",
         es=("Usted promete, bajo pena de perjurio, que todo en la solicitud es verdad.",
             "Dar información falsa a propósito puede tener consecuencias legales.")),
    dict(category="repayment", severity="medium", unusual=False,
         quote="you will have to pay them back, and you may be disqualified from the program",
         explanation="If you get benefits you shouldn't have, you'll have to repay them and could be removed from "
                     "the program.",
         reason="Reporting accurately protects you from having to pay money back.",
         es=("Si recibe beneficios que no le correspondían, tendrá que devolverlos y podrían sacarle del programa.",
             "Informar con exactitud le evita tener que devolver dinero.")),
    dict(category="deadline", severity="low", unusual=False,
         quote="You must report changes in income of more than $100 within 10 days",
         explanation="After you're approved, you must report income changes of more than $100 within 10 days.",
         reason="Missing this deadline can mean repaying benefits.",
         es=("Después de que le aprueben, debe informar cambios de ingresos de más de $100 dentro de 10 días.",
             "No cumplir ese plazo puede significar devolver beneficios.")),
]


def build_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), NAME, fontsize=15)
    page.insert_text((50, 68), "Riverbend County Department of Human Services (fictional, for demonstration only)",
                     fontsize=8)
    y = 90
    for f in FIELDS:
        page.insert_text((50, y + 12), f["label"], fontsize=10)
        w = pymupdf.Widget()
        w.field_name = f["id"]
        if f["type"] == "yes_no":
            w.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
            w.rect = pymupdf.Rect(300, y, 315, y + 15)
        else:
            w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
            w.rect = pymupdf.Rect(300, y, 560, y + 17)
            w.text_fontsize = 9
        page.add_widget(w)
        y += 27
    y += 10
    for i, para in enumerate(EXPLAINERS):
        rect = pymupdf.Rect(50, y, 560, y + 60)
        page.insert_textbox(rect, para, fontsize=11 if i == 0 else 9)
        y += 22 if i == 0 else 42
    page2 = doc.new_page()
    y = 50
    for i, para in enumerate(RIGHTS):
        page2.insert_textbox(pymupdf.Rect(50, y, 560, y + 60), para, fontsize=13 if i == 0 else 10)
        y += 28 if i == 0 else 44
    page2.insert_text((50, y + 20), "Signature: ______________________     Date: ____________", fontsize=10)
    doc.save(path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    build_pdf(OUT / "form.pdf")
    schema_fields = []
    for f in FIELDS:
        sf = {k: v for k, v in f.items() if k != "es"}
        sf["pdf_field"] = f["id"]
        schema_fields.append(sf)
    (OUT / "schema.json").write_text(json.dumps(
        {"form_id": "demo_snap", "name": NAME, "version": 1, "reviewed": True, "fields": schema_fields},
        indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (OUT / "meta.json").write_text(json.dumps({
        "form_id": "demo_snap", "name": NAME,
        "aliases": ["beneficios de SNAP", "SNAP benefits", "Riverbend SNAP", "Riverbend County SNAP",
                    "solicitud de SNAP", "beneficios de comida de Riverbend"],
        "agency": "Riverbend County Department of Human Services (fictional)",
        "description": "A fake, short SNAP application for demonstrating phone-only form filling.",
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (OUT / "notices.json").write_text(json.dumps(
        {"findings": [{k: v for k, v in f.items() if k != "es"} for f in FINDINGS]}, indent=2, ensure_ascii=False)
        + "\n", encoding="utf-8")
    es = {"form.name": "Solicitud de SNAP del Condado de Riverbend",
          "g.About you": "Sobre usted", "g.Household": "Hogar", "g.Income": "Ingresos",
          "g.Housing costs": "Gastos de vivienda"}
    for f in FIELDS:
        es[f"q.{f['id']}"], es[f"l.{f['id']}"] = f["es"]
    for f in FINDINGS:
        key = hashlib.sha1(f["quote"].encode()).hexdigest()[:10]
        es[f"n.{key}"], es[f"r.{key}"] = f["es"]
    (OUT / "i18n.es.json").write_text(json.dumps(es, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
