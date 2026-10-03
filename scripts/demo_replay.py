"""Replay the DEMO_SCRIPT.md path through handle_turn, by text, with a fake LLM that always answers
correctly. Anything that goes wrong here is deterministic code, not model quality.

    uv run python scripts/demo_replay.py

Uses a throwaway database; never touches data/formline.db or Twilio.
"""

import datetime
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{tmp}/replay.db", DATA_DIR=tmp, ANTHROPIC_API_KEY="fake",
                  TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN="", TWILIO_PHONE_NUMBER="")

from sqlmodel import select  # noqa: E402

from app import db  # noqa: E402
from app.contracts import Deadline, DocumentExplanation, TurnRequest  # noqa: E402
from app.core import identity  # noqa: E402
from app.core.router import IntentClassification  # noqa: E402
from app.core.turn import handle_turn  # noqa: E402
from app.dashboard.demo import sync_forms  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.engines import document_engine, form_library  # noqa: E402
from app.engines.form_engine import _NormalizedMoney  # noqa: E402
from app.llm import client as llm  # noqa: E402
from app.models import Task  # noqa: E402


def fake_structured(model, **_kw):
    if model is IntentClassification:
        return IntentClassification(intent="fill_form")
    if model is _NormalizedMoney:  # "about 350 a week, it changes"
        return _NormalizedMoney(monthly_amount=1515.5, varies=True)
    raise RuntimeError(f"no fake for {model.__name__}")


llm.structured = fake_structured
llm.text = lambda **_kw: "[translated]"

PHONE = "+12025550104"


def say(text: str = "", media: list[str] | None = None) -> None:
    r = handle_turn(TurnRequest(phone=PHONE, channel="sms", text=text, media_paths=media or []))
    print(f"ROSA> {text or '[photo]'}\n  FL> {r.reply[:300]}")
    for sms in r.followup_sms:
        print(f"  SMS> {sms}")


def current_task() -> Task | None:
    sess = identity.get_session(PHONE)
    if not sess.active_task_id:
        return None
    with session_scope() as s:
        return s.get(Task, sess.active_task_id)


def main() -> None:
    db.init_db()
    sync_forms()

    print("=== Step 1: Rosa's scripted lines (DEMO_SCRIPT.md persona card) ===")
    for line in ["Hola", "Español, por favor.", "Sí, está bien.", "Sí", "4821",
                 "Necesito pedir estampillas de comida.", "Rosa Martínez.",
                 "Veintidós de julio de mil novecientos setenta y nueve.", "07/22/1979",
                 "77 Maple Avenue, apartamento tres, Springfield, Illinois, 62703",
                 "No", "2175550104", "Prefiero no darlo.", "skip", "No",
                 "Somos tres: yo y mis dos hijos, Diego de nueve años y Ana de seis.", "3",
                 "Sí, en Lincoln Laundromat.", "Sí"]:
        say(line)

    print("\n=== Finish SNAP with plain answers, then the scripted read-back correction ===")
    plain = {"date": "07/22/1979", "phone": "2175550104", "yes_no": "no", "number": "3",
             "money": "900", "ssn_last4": "skip", "choice": "weekly",
             "address": "77 Maple Ave Apt 3, Springfield IL 62703"}
    for _ in range(40):
        task = current_task()
        if not task or task.status != "active":
            break
        field = next(f for f in form_library.load_schema(task.form_id).fields if f.id == task.current_field)
        answer = {"employed": "yes", "employer": "Lincoln Laundromat"}.get(field.id) \
            or plain.get(field.type, "Rosa Martinez")
        say(answer)
    say("La renta es novecientos cincuenta, no novecientos.")
    say("sí")

    print("\n=== Step 2: verification and the filled PDF ===")
    with session_scope() as s:
        task = s.exec(select(Task).order_by(Task.id.desc())).first()
    print("status:", task.status, "| verification:", {k: v for k, v in (task.verification or {}).items() if v})
    if task.output_pdf_path:
        import pymupdf
        doc = pymupdf.open(task.output_pdf_path)
        values = [w.field_value for p in doc for w in p.widgets()
                  if w.field_type_string == "Text" and w.field_value]
        print("PDF text values:", values[:12])

    print("\n=== Deadline contract check ===")
    try:
        Deadline(date=datetime.date.today(), description="check")
        print("Deadline.date accepts a date: OK")
    except Exception as e:  # the bug: field name `date` shadows the type
        print("Deadline.date REJECTS a date:", type(e).__name__)

    print("\n=== Steps 3-4: letter photo, reminder, related form from memory ===")
    # model_construct works around the Deadline bug so the rest of the flow can be seen.
    document_engine.explain_document = lambda paths, language="en": DocumentExplanation(
        document_type="Medicaid renewal notice",
        plain_summary="This is a Medicaid renewal notice. Return the form by the deadline or coverage may end.",
        deadlines=[Deadline.model_construct(date=datetime.date.today() + datetime.timedelta(days=30),
                                            description="Return Medicaid renewal")],
        related_form_id="il_medicaid", confidence=0.9)
    say(media=["letter.png"])
    for line in ["Sí, recuérdame.", "sí", "Sí, llénala ahora.", "sí", "Ana", "sí"]:
        say(line)
    print("session state:", identity.get_session(PHONE).state)


if __name__ == "__main__":
    main()
