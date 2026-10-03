"""End-to-end with the real model: Chromium + extension + a demo site + a simulated phone call.

    uv run python scripts/e2e_golden_path.py                         # the golden demo (book with Dr. Smith)
    uv run python scripts/e2e_golden_path.py --scenario library      # a different site: renew a library book
    uv run python scripts/e2e_golden_path.py --runs 3 --headed --screenshots out/

Each run: start an isolated Formline server, launch Chromium with the extension, pair it through the
popup, open the site, then "call" over the real ConversationRelay websocket. A model plays the caller
from a persona and answers each question in its own words. A run passes only if the website itself
shows the goal was done (read from the page and its storage) and the task was verified complete.

Needs OPENAI_API_KEY (or ANTHROPIC_API_KEY) in .env, and `uv run playwright install chromium`.
"""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from browser_harness import Server, launch_chromium_async, pair_async  # noqa: E402
from call_sim import PhoneCall  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

PHONE = "+1555000111{}"  # one number per run: pairing allows 3 codes per number per 10 minutes
PIN = "4821"


def _weekday(iso: str) -> int:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().weekday()


async def riverbend_checks(page) -> dict:
    body = await page.inner_text("main")
    appts = json.loads(await page.evaluate("localStorage.getItem('rb_appointments') || '[]'"))
    booked = [a for a in appts if a.get("provider") == "smith"]
    return {
        "success page shown": "Your appointment is scheduled" in body,
        "one booking with Dr. Smith": len(booked) == 1,
        "reason mentions knee": bool(booked) and "knee" in booked[0]["reason"].lower(),
        "booked on a Thursday": bool(booked) and _weekday(booked[0]["when"]) == 3,
    }


async def library_checks(page) -> dict:
    body = await page.inner_text("main")
    loans = json.loads(await page.evaluate("localStorage.getItem('mcpl_loans') || '[]'"))
    by_id = {b["id"]: b for b in loans}
    return {
        "renewal page shown": "Renewal complete" in body,
        "The Overstory renewed": by_id.get("b1", {}).get("renewals") == 1,
        "nothing else renewed": by_id.get("b3", {}).get("renewals", 1) == 1 and by_id.get("b2", {}).get("renewals", 2) == 2,
    }


SCENARIOS = {
    "riverbend": {
        "path": "/demo/riverbend/",
        "site_word": "Riverbend",
        "opening": "I need to make an appointment with Dr. Smith.",
        "done": r"\b(scheduled|booked|confirmation)\b",
        "checks": riverbend_checks,
        "persona": """You are Margaret Ellis, 68, on the phone with Formline, an assistant that is operating the \
patient portal on your computer for you. You want an appointment with Dr. Smith because your knee has been hurting \
for about two weeks. You'd like Thursday afternoon if possible; otherwise any time is fine. You're happy with an \
in-person office visit at the main clinic. When Formline summarizes the appointment and asks whether to go ahead, \
say yes if it's Dr. Smith, about your knee, on Thursday.""",
    },
    "library": {
        "path": "/demo/library/index.html",
        "site_word": "Library",
        "opening": "Can you renew my library book, The Overstory?",
        "done": r"\b(renewed|renewal is complete|now due|new due date)\b",
        "checks": library_checks,
        "persona": """You are Margaret Ellis, 68, on the phone with Formline, an assistant that is operating the \
library website on your computer for you. You want to renew The Overstory, and only that book. If asked about \
other books, say you only need The Overstory. When Formline asks whether to go ahead with renewing The Overstory, \
say yes.""",
    },
}

REPLY_RULES = """Reply with ONE short, natural spoken sentence, like a real caller. Never invent questions; just \
answer what you were asked."""


def caller_reply(persona: str, line: str, history: list[str]) -> str:
    """A model plays the caller; "Yes." if it can't be reached."""
    try:
        from app.llm import client as llm

        convo = "\n".join(history[-8:])
        reply = llm.text(system=persona + " " + REPLY_RULES, model="gpt-4.1-mini", max_tokens=60,
                         messages=[{"role": "user", "content": f"Conversation so far:\n{convo}\n\nFormline just said: "
                                    f"{line}\n\nYour reply:"}])
        if reply:
            return reply.strip().strip('"')
    except Exception as e:
        print(f"  (caller model unavailable: {e})")
    return "Yes."


async def one_run(pw, server: Server, scenario: dict, args, run: int) -> dict:
    context, ext = await launch_chromium_async(pw, headless=not args.headed)
    try:
        await pair_async(context, ext, server.url, phone=PHONE.format(run), name="Margaret", pin=PIN)
        page = await context.new_page()
        await page.goto(server.url + scenario["path"])
        await page.evaluate("localStorage.clear()")
        await page.reload()
        await page.wait_for_selector("h1")
        await page.bring_to_front()
        await asyncio.sleep(1.0)

        transcript: list[str] = []
        turn_times: list[float] = []
        started = time.monotonic()
        done = False
        async with PhoneCall(server.url, PHONE.format(run)) as call:
            async def hear() -> str:
                line = await call.hear(120)
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
            assert scenario["site_word"].lower() in line.lower(), line

            await say(scenario["opening"])
            t0 = time.monotonic()
            for _ in range(30):
                line = await hear()
                if not line:
                    break
                if line.rstrip().endswith("?"):
                    turn_times.append(time.monotonic() - t0)
                    await say(caller_reply(scenario["persona"], line, transcript))
                    t0 = time.monotonic()
                elif re.search(scenario["done"], line.lower()):
                    turn_times.append(time.monotonic() - t0)
                    done = True
                    break
            total = time.monotonic() - started

        await asyncio.sleep(0.8)
        checks = {"said it was done": done, **(await scenario["checks"](page))}
        if args.screenshots:
            name = f"{args.scenario}-run{run}"
            await page.screenshot(path=str(Path(args.screenshots) / f"{name}-site.png"))
            dash = await context.new_page()
            await dash.set_viewport_size({"width": 1600, "height": 950})
            await dash.goto(server.url + "/dashboard")
            await asyncio.sleep(2.5)
            await dash.screenshot(path=str(Path(args.screenshots) / f"{name}-dashboard.png"))
            await dash.close()

        db = sqlite3.connect(server.data_dir / "formline.db")
        status = db.execute("select status, steps, model_calls from browsertask order by id desc limit 1").fetchone()
        actions = db.execute("select kind, element_label, ok from browseraction where task_id = "
                             "(select max(id) from browsertask) order by id").fetchall()
        db.close()
        checks["task verified complete"] = bool(status) and status[0] == "completed"
        checks["confirmation asked before the final step"] = any(k == "confirm_request" for k, _, _ in actions)
        return {"checks": checks, "total_s": round(total, 1), "turn_s": [round(t, 1) for t in turn_times],
                "steps": status[1] if status else None, "model_calls": status[2] if status else None,
                "actions": actions}
    finally:
        await context.close()


async def main_async(args) -> int:
    scenario = SCENARIOS[args.scenario]
    failures = 0
    with Server(port=8767) as server:
        async with async_playwright() as pw:
            for run in range(1, args.runs + 1):
                print(f"\n=== {args.scenario} run {run} ===")
                try:
                    r = await one_run(pw, server, scenario, args, run)
                except Exception as e:
                    failures += 1
                    lines = [x for x in server.tail(500).splitlines() if "app.agent" in x or "ERROR" in x]
                    print(f"RUN {run} CRASHED: {e!r}\n--- agent log ---\n" + "\n".join(lines[-60:]))
                    continue
                ok = all(r["checks"].values())
                failures += not ok
                for name, passed in r["checks"].items():
                    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
                print(f"  total {r['total_s']}s, Formline time per caller turn {r['turn_s']}, "
                      f"{r['steps']} browser steps, {r['model_calls']} model calls")
                if not ok or args.verbose:
                    print("  actions:", *r["actions"], sep="\n    ")
                    print("  agent log:", *[x for x in server.tail(600).splitlines()
                                            if "app.agent" in x or "ERROR" in x][-80:], sep="\n    ")
    print(f"\n{args.runs - failures}/{args.runs} runs passed")
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", choices=sorted(SCENARIOS), default="riverbend")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--screenshots", default="", help="folder to save the site and dashboard screenshots in")
    p.add_argument("--verbose", action="store_true", help="print the action list and agent log for every run")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
