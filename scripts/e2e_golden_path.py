"""The golden demo, end to end, with the real model: Chromium + extension + portal + a simulated phone.

    uv run python scripts/e2e_golden_path.py [--headed] [--runs 3]

1. Start an isolated Formline server, launch Chromium with the extension, pair it through the popup.
2. Open the Riverbend portal. "Call" Formline over the real ConversationRelay websocket.
3. Enter the PIN, then: "I need to make an appointment with Dr. Smith." Answer each question the way
   the demo caller would (knee pain, Thursday), and say yes to the confirmation.
4. Pass only if the portal itself shows the booking (Dr. Smith, knee, Thursday) and the task was
   verified complete. Prints the conversation and the time Formline took per caller turn.

Needs OPENAI_API_KEY (or ANTHROPIC_API_KEY) in .env, and `uv run playwright install chromium`.
"""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from browser_harness import Server, launch_chromium_async, pair_async  # noqa: E402
from call_sim import PhoneCall  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

PHONE = "+1555000111{}"  # one number per run: pairing allows 3 codes per number per 10 minutes
PIN = "4821"


CALLER_PERSONA = """You are Margaret Ellis, 68, on the phone with Formline, an assistant that is operating the \
patient portal on your computer for you. You want an appointment with Dr. Smith because your knee has been hurting \
for about two weeks. You'd like Thursday afternoon if possible; otherwise any time is fine. You're happy with an \
in-person office visit at the main clinic. When Formline summarizes the appointment and asks whether to go ahead, \
say yes if it's Dr. Smith, about your knee, on Thursday. Reply with ONE short, natural spoken sentence, like a real \
caller. Never invent questions; just answer what you were asked."""


def caller_reply(line: str, history: list[str]) -> str:
    """What the demo caller says back to Formline: a model playing Margaret, with keyword rules as fallback."""
    try:
        from app.llm import client as llm

        convo = "\n".join(history[-8:])
        reply = llm.text(system=CALLER_PERSONA, model="gpt-4.1-mini", max_tokens=60,
                         messages=[{"role": "user", "content": f"Conversation so far:\n{convo}\n\nFormline just said: "
                                    f"{line}\n\nYour reply:"}])
        if reply:
            return reply.strip().strip('"')
    except Exception:
        pass
    return keyword_reply(line)


def keyword_reply(line: str) -> str:
    t = line.lower()
    if re.search(r"\b(go ahead|book it|should i|shall i|want me to|would you like me to|confirm|okay to)\b", t):
        return "Yes, please book it."
    if re.search(r"\b(about|reason|why|what brings|symptom|what's going on|wrong)\b", t):
        return "My knee has been hurting."
    if re.search(r"\b(prefer|which|what time|when|tuesday|thursday|time works|options)\b", t):
        return "Thursday at 2 works."
    if re.search(r"\b(type|visit type|in person|video)\b", t):
        return "In person, at the office."
    if re.search(r"\b(location|clinic|where)\b", t):
        return "The main clinic is fine."
    return "Yes."


async def one_run(pw, server: Server, headed: bool, run: int) -> dict:
    context, ext = await launch_chromium_async(pw, headless=not headed)
    try:
        await pair_async(context, ext, server.url, phone=PHONE.format(run), name="Margaret", pin=PIN)
        page = await context.new_page()
        await page.goto(server.url + "/demo/riverbend/")
        await page.evaluate("localStorage.clear()")
        await page.reload()
        await page.wait_for_selector("h1")
        await page.bring_to_front()
        await asyncio.sleep(1.0)

        transcript: list[str] = []
        turn_times: list[float] = []
        started = time.monotonic()
        async with PhoneCall(server.url, PHONE.format(run)) as call:
            async def hear() -> str:
                line = await call.hear(90)
                transcript.append(f"FORMLINE: {line}")
                print(f"  FORMLINE: {line}", flush=True)
                return line or ""

            async def say(text: str) -> None:
                transcript.append(f"CALLER:   {text}")
                print(f"  CALLER:   {text}", flush=True)
                await call.say(text)

            line = await hear()
            while "pin" not in line.lower() and "help" not in line.lower():
                line = await hear()
            if "pin" in line.lower():  # skipped if the PIN was entered in the last 30 minutes
                transcript.append(f"CALLER:   [keys {PIN}]")
                await call.key(PIN)
                line = await hear()
                while "help" not in line.lower():
                    line = await hear()
            assert "Riverbend" in line, line

            await say("I need to make an appointment with Dr. Smith.")
            t0 = time.monotonic()
            done = False
            for _ in range(30):
                line = await hear()
                if not line:
                    break
                low = line.lower()
                if line.rstrip().endswith("?"):
                    turn_times.append(time.monotonic() - t0)
                    await say(caller_reply(line, transcript))
                    t0 = time.monotonic()
                elif re.search(r"\b(scheduled|booked|confirmation)\b", low) and "?" not in line:
                    turn_times.append(time.monotonic() - t0)
                    done = True
                    break
            total = time.monotonic() - started

        await asyncio.sleep(0.5)
        body = await page.inner_text("main")
        appts = json.loads(await page.evaluate("localStorage.getItem('rb_appointments') || '[]'"))
        booked = [a for a in appts if a.get("provider") == "smith"]
        db = sqlite3.connect(server.data_dir / "formline.db")
        status = db.execute("select status, steps, model_calls from browsertask order by id desc limit 1").fetchone()
        actions = db.execute("select kind, element_label, ok from browseraction order by id").fetchall()
        db.close()

        checks = {
            "said it was done": done,
            "success page shown": "Your appointment is scheduled" in body,
            "one booking with Dr. Smith": len(booked) == 1,
            "reason mentions knee": bool(booked) and "knee" in booked[0]["reason"].lower(),
            "booked on a Thursday": bool(booked) and _weekday(booked[0]["when"]) == 3,
            "task verified complete": bool(status) and status[0] == "completed",
            "confirmation asked before booking": [k for k, _, _ in actions].count("confirm_request") >= 1,
        }
        return {"run": run, "checks": checks, "total_s": round(total, 1), "turn_s": [round(t, 1) for t in turn_times],
                "steps": status[1] if status else None, "model_calls": status[2] if status else None,
                "actions": actions, "transcript": transcript}
    finally:
        await context.close()


def _weekday(iso: str) -> int:
    from datetime import datetime

    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().weekday()


async def main_async(args) -> int:
    failures = 0
    with Server(port=8767) as server:
        async with async_playwright() as pw:
            for run in range(1, args.runs + 1):
                print(f"\n=== run {run} ===")
                try:
                    r = await one_run(pw, server, args.headed, run)
                except Exception as e:
                    failures += 1
                    print(f"RUN {run} CRASHED: {e!r}\n--- server log ---\n{server.tail(60)}")
                    continue
                ok = all(r["checks"].values())
                failures += not ok
                for name, passed in r["checks"].items():
                    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
                print(f"  total {r['total_s']}s, Formline time per caller turn {r['turn_s']}, "
                      f"{r['steps']} browser steps, {r['model_calls']} model calls")
                if not ok or args.verbose:
                    print("  actions:", *r["actions"], sep="\n    ")
                    print("  agent log:", *[line for line in server.tail(600).splitlines()
                                            if "app.agent" in line or "ERROR" in line][-80:], sep="\n    ")
    print(f"\n{args.runs - failures}/{args.runs} runs passed")
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--headed", action="store_true")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--verbose", action="store_true", help="print the action list and agent log for every run")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
