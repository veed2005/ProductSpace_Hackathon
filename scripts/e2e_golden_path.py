"""End-to-end with the real model: Chromium + extension + a demo site + a simulated phone call.

    uv run python scripts/e2e_golden_path.py                         # the golden demo (book with Dr. Smith)
    uv run python scripts/e2e_golden_path.py --scenario library      # a different site: renew a library book
    uv run python scripts/e2e_golden_path.py --scenario pdf          # questions about a 6-page PDF lease
    uv run python scripts/e2e_golden_path.py --scenario expiring_pdf # same, from an expired no-store link (like S3)
    uv run python scripts/e2e_golden_path.py --scenario letterboxd   # the real letterboxd.com: search for a film
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
from expiring_file_server import ExpiringServer  # noqa: E402
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


async def letterboxd_checks(page) -> dict:
    return {"ended on a search result or film page": any(p in page.url for p in ("/search", "/film"))}


SCENARIOS = {
    "pdf": {
        "path": "/demo/testbench/lease.pdf",
        "site_word": "Lease",
        "opening": "I don't understand this contract. Can you help me?",
        # (question, every one of these must be in the answer: alternatives separated by |)
        "questions": [
            ("Can I have a dog?", ["40|forty", "300|three hundred"]),
            ("How do I get out of my lease early?", ["60|sixty", "1,450|one month|fourteen hundred"]),
            ("Who signed it for the landlord?", ["Dana|Whitfield"]),
            ("What happens if I pay rent late?", ["75|seventy-five|seventy five"]),
        ],
    },
    "reelbox": {
        "path": "/demo/reelbox/index.html",
        "site_word": "Reelbox",
        "opening": "Look up Avengers.",
        "done": r"\b(found|here|showing|results?|opened|open)\b",
        "checks": letterboxd_checks,
        "must_type": "avengers",
        "persona": """You are Evan, a film fan. You want to look up the Avengers. If asked which one, say the \
original one from 2012. Keep answers short.""",
    },
    "reelbox_vague": {
        "path": "/demo/reelbox/index.html",
        "site_word": "Reelbox",
        "opening": "Can you look up a movie for me?",
        "done": r"\b(found|here|showing|results?|opened|open)\b",
        "checks": letterboxd_checks,
        "must_type": "paddington",
        "persona": """You are Evan, a film fan. The movie you want is Paddington 2. If asked which movie, say \
Paddington 2. Keep answers short.""",
    },
    "expiring_pdf": {
        # Served like an S3 signed link: Cache-Control: no-store, valid for 5 s. The call starts after it expired.
        "expiring": {"file": "demo_sites/testbench/lease.pdf", "valid_s": 5, "cache_control": "no-store", "wait_s": 7},
        "site_word": "Lease",
        "opening": "I don't understand this contract. Can you help me?",
        "questions": [
            ("Can I have a dog?", ["40|forty", "300|three hundred"]),
            ("Who signed it for the landlord?", ["Dana|Whitfield"]),
        ],
    },
    "letterboxd": {
        "path": "https://letterboxd.com/",
        "site_word": "Letterboxd",
        "opening": "Look up Avengers on Letterboxd.",
        "done": r"\b(found|here|showing|results?|opened|open)\b",
        "checks": letterboxd_checks,
        "must_type": "avengers",
        "persona": """You are Evan, using Letterboxd. You want to look up the Avengers films. If asked which \
one, say the first Avengers movie from 2012. If asked anything else, keep it short.""",
    },
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
        expiring = None
        if scenario.get("expiring"):
            x = scenario["expiring"]
            expiring = ExpiringServer(x["file"], port=8780 + run, valid_s=x["valid_s"], cache_control=x["cache_control"])
            expiring.__enter__()
            await page.goto(expiring.url)
            await page.bring_to_front()
            await asyncio.sleep(x["wait_s"])  # the link has expired before the call starts
            scenario = {**scenario, "path": expiring.url}
        external = scenario["path"].startswith("http")
        if not expiring:
            await page.goto(scenario["path"] if external else server.url + scenario["path"])
        if not external and not scenario["path"].endswith(".pdf"):
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
            if scenario.get("questions"):
                try:
                    return await qa_run(scenario, hear, say, transcript, server, started)
                finally:
                    if expiring:
                        expiring.__exit__(None, None, None)
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
        actions = db.execute("select kind, element_label, ok, value from browseraction where task_id = "
                             "(select max(id) from browsertask) order by id").fetchall()
        db.close()
        checks["task verified complete"] = bool(status) and status[0] == "completed"
        if scenario.get("must_type"):
            # A lookup may end by answering from the page (with a verified quote) instead of "done".
            checks["task verified complete"] = checks["task verified complete"] or any(
                k == "answer" and v for k, _, _, v in actions)
            checks[f"typed {scenario['must_type']!r} into the site's search"] = any(
                k == "type" and scenario["must_type"] in (v or "").lower() for k, _, _, v in actions)
        else:
            checks["confirmation asked before the final step"] = any(k == "confirm_request" for k, _, _, _ in actions)
        return {"checks": checks, "total_s": round(total, 1), "turn_s": [round(t, 1) for t in turn_times],
                "steps": status[1] if status else None, "model_calls": status[2] if status else None,
                "actions": actions}
    finally:
        await context.close()


async def qa_run(scenario, hear, say, transcript, server, started) -> dict:
    """Ask questions about the open document; every answer must contain the expected facts."""
    async def reply() -> tuple[str, float]:
        t = time.monotonic()
        while True:
            line = await hear()
            if line and not line.lower().startswith(("one moment", "un momento")):
                return line, time.monotonic() - t

    first, _ = await reply()  # the plain summary of the document
    checks = {"explained the document when asked": len(first.split()) >= 12 and "trouble" not in first.lower()}
    turn_times = []
    for question, needles in scenario["questions"]:
        await say(question)
        answer, secs = await reply()
        turn_times.append(secs)
        low = answer.lower().replace(",", "")
        ok = all(any(alt.lower().replace(",", "") in low for alt in n.split("|")) for n in needles)
        checks[f"answered {question!r}"] = ok
    db = sqlite3.connect(server.data_dir / "formline.db")
    rows = db.execute("select kind, element_label, ok, value from browseraction where task_id = "
                      "(select max(id) from browsertask) order by id").fetchall()
    calls = db.execute("select model_calls from browsertask order by id desc limit 1").fetchone()
    db.close()
    checks["every answer quoted the document"] = sum(1 for k, _, _, v in rows if k == "answer" and v) >= len(scenario["questions"])
    return {"checks": checks, "total_s": round(time.monotonic() - started, 1), "turn_s": [round(t, 1) for t in turn_times],
            "steps": 0, "model_calls": calls[0] if calls else None, "actions": rows}


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
