"""The phone-only form demo, end to end over the real call path: a Spanish-speaking caller with no computer
applies for SNAP (the fake Riverbend County form) and gets an emailed receipt.

    # 1. a server with the phone-forms workflow (rehearsal only: signatures off)
    TWILIO_VALIDATE_SIGNATURES=false FORMLINE_PHONE_FORMS=true uv run uvicorn app.main:app --port 8000
    # 2. the caller
    uv run python scripts/formcall_demo.py [--server http://localhost:8000] [--phone +1555...]

It speaks ConversationRelay exactly like Twilio (scripts/call_sim.py) and answers whatever Formline actually asks,
so it works with the real model: spelling the surname if asked, confirming read-backs, asking why the rent is
needed, reading one notice, correcting the income, saying "ajá" (not consent) before the real "sí", and dictating
an email address. Watch it on the dashboard (http://localhost:8000/dashboard). The receipt goes through the
configured provider (FORMLINE_EMAIL_PROVIDER: outbox by default, so it lands in data/outbox/).
"""

from __future__ import annotations

import argparse
import asyncio
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from call_sim import PhoneCall  # noqa: E402

# (pattern in what Formline said, what the caller says, how many times) - checked in order, first match wins.
SCRIPT: list[tuple[str, str, int]] = [
    (r"Hola, habla Formline|How can I help", "Quiero solicitar beneficios de SNAP.", 1),
    (r"Acepto usar su información|Responda sí o no", "Sí, está bien.", 3),
    (r"Elija un PIN", "/key 4821", 2),
    (r"apellido.*Me lo deletrea", "D, O, U, B, E, K.", 2),
    (r"Me lo deletrea", "S, U, N, R, I, S, E, espacio, D, I, N, E, R.", 2),
    (r"nombre completo", "Me llamo Evan Doubek.", 2),
    (r"fecha de nacimiento", "14 de marzo de 1988.", 2),
    (r"domicilio", "412 Elm Street, Springfield, IL 62704", 2),
    (r"teléfono", "217 555 0104", 2),
    (r"una vez más", "6789", 2),
    (r"Seguro Social", "6789", 2),
    (r"cuántas personas", "Somos tres.", 2),
    (r"tiene trabajo", "Sí.", 2),
    (r"Para quién trabaja", "Sunrise Diner.", 2),
    (r"cuánto gana", "Trescientos dólares por semana.", 2),
    (r"renta al mes", "¿Por qué necesitan saber la renta?", 1),
    (r"renta al mes", "Ochocientos cincuenta al mes.", 2),
    (r"sigamos con la solicitud", "Sí, sigamos.", 3),
    (r"lea el texto exacto", "Sí, léalo.", 1),
    (r"lea el texto exacto", "Está bien.", 6),
    (r"si seguimos", "Está bien, sigamos.", 3),
    (r"Esto es lo que tengo", "En realidad son trescientos quince por semana, no trescientos.", 1),
    (r"Está todo correcto|qué cambiar", "Sí, está todo correcto.", 3),
    (r"Preparo su solicitud", "Ajá.", 1),
    (r"Preparo su solicitud|sí o un no claro", "Sí.", 2),
    (r"copia de la información", "Sí.", 1),
    (r"A qué correo", "evan punto doubek arroba gmail punto com", 2),
    (r"¿Es correcto\?|¿Está bien\?|¿Lo hago\?", "Sí.", 20),
    (r"algo más en que le pueda ayudar", "/quit", 1),
]


def answer(heard: str, used: dict[int, int]) -> str | None:
    for i, (pattern, reply, times) in enumerate(SCRIPT):
        if re.search(pattern, heard, re.IGNORECASE) and used.get(i, 0) < times:
            used[i] = used.get(i, 0) + 1
            return reply
    return None


async def run(server: str, phone: str) -> int:
    used: dict[int, int] = {}
    started = time.monotonic()
    async with PhoneCall(server, phone) as call:
        while True:
            heard = await call.hear(timeout=90)
            if heard is None:
                print("(call ended)")
                break
            print(f"FORMLINE  {heard}")
            if heard.strip() in ("Un momento.", "One moment."):
                continue
            reply = answer(heard, used)
            if reply is None:
                print("(the script has no answer for that; stopping)")
                return 1
            if reply == "/quit":
                break
            if reply.startswith("/key "):
                print(f"CALLER    [keypad {reply[5:]}]")
                await call.key(reply[5:])
            else:
                print(f"CALLER    {reply}")
                await call.say(reply, lang="es-US" if not re.match(r"^[\d ]+$|^\d+ Elm", reply) else "en-US")
    print(f"\nDone in {time.monotonic() - started:.0f}s. Open {server}/dashboard to see the form, notices, and receipt.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--phone", default=f"+1555013{random.randint(1000, 9999)}", help="a new caller each run")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args.server, args.phone)))


if __name__ == "__main__":
    main()
