"""Is everything ready for the live demo?

  uv run python scripts/demo_preflight.py           # run with the server and ngrok already up
  uv run python scripts/demo_preflight.py --local   # skip Twilio/ngrok checks (simulator-only rehearsal)

Prints one line per check with a fix for anything that fails. Exit code 1 if anything blocking fails.
Makes no paid API calls: the Anthropic check only looks up the configured models.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402

LOCAL = "http://localhost:8000"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    blocking: bool = True


def check_env() -> list[Check]:
    s = get_settings()
    out = [Check("LLM API key in .env (Anthropic or OpenAI)", bool(s.anthropic_api_key or s.openai_api_key),
                 fix="Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env")]
    twilio = bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_phone_number)
    out.append(Check("Twilio credentials in .env", twilio, fix="Set TWILIO_ACCOUNT_SID / _AUTH_TOKEN / _PHONE_NUMBER"))
    out.append(Check("PUBLIC_BASE_URL is an https ngrok URL",
                     s.public_base_url.startswith("https://") and "example" not in s.public_base_url,
                     s.public_base_url, "Start ngrok and put its https URL in PUBLIC_BASE_URL, then restart the server"))
    out.append(Check("Twilio signature validation is on", s.twilio_validate_signatures,
                     fix="Set TWILIO_VALIDATE_SIGNATURES=true for the demo", blocking=False))
    out.append(Check("Dev endpoint is off", not s.dev_endpoints,
                     fix="Set FORMLINE_DEV_ENDPOINTS=false (it skips Twilio auth and ngrok makes it public)"))
    out.append(Check("Dashboard is local-only", not s.dashboard_remote, fix="Set FORMLINE_DASHBOARD_REMOTE=false"))
    return out


def check_server() -> list[Check]:
    try:
        ok = httpx.get(f"{LOCAL}/health", timeout=3).status_code == 200
        dash = httpx.get(f"{LOCAL}/dashboard", timeout=3).status_code == 200
    except httpx.HTTPError as e:
        return [Check("Server running on :8000", False, str(e), "uv run uvicorn app.main:app")]
    return [Check("Server running on :8000", ok, fix="uv run uvicorn app.main:app"),
            Check("Dashboard loads locally", dash, fix="Check the server log for errors")]


def check_public() -> list[Check]:
    base = get_settings().public_base_url.rstrip("/")
    try:
        health = httpx.get(f"{base}/health", timeout=6).status_code == 200
        dash_code = httpx.get(f"{base}/api/people", timeout=6).status_code
    except httpx.HTTPError as e:
        return [Check("ngrok forwards to the server", False, str(e), "Run `ngrok http 8000` and update PUBLIC_BASE_URL")]
    return [Check("ngrok forwards to the server", health, base, "Run `ngrok http 8000` and update PUBLIC_BASE_URL"),
            Check("Dashboard is NOT reachable through ngrok", dash_code == 403, f"got HTTP {dash_code}",
                  "Set FORMLINE_DASHBOARD_REMOTE=false and restart")]


def check_twilio_webhooks() -> list[Check]:
    s = get_settings()
    if not (s.twilio_account_sid and s.twilio_auth_token):
        return []
    from twilio.rest import Client

    try:
        nums = Client(s.twilio_account_sid, s.twilio_auth_token).incoming_phone_numbers.list(
            phone_number=s.twilio_phone_number, limit=1)
    except Exception as e:
        return [Check("Twilio account reachable", False, str(e), "Check TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")]
    if not nums:
        return [Check("Twilio number belongs to this account", False, s.twilio_phone_number,
                      "Fix TWILIO_PHONE_NUMBER (E.164, e.g. +12175550123)")]
    n, base = nums[0], s.public_base_url.rstrip("/")
    return [
        Check("Twilio SMS webhook points here", n.sms_url == f"{base}/twilio/messaging", n.sms_url or "(none)",
              f"Twilio console → number → Messaging → {base}/twilio/messaging (POST)"),
        Check("Twilio voice webhook points here", n.voice_url == f"{base}/twilio/voice", n.voice_url or "(none)",
              f"Twilio console → number → Voice → {base}/twilio/voice (POST)", blocking=False),
    ]


def check_anthropic() -> list[Check]:
    s = get_settings()
    if not s.anthropic_api_key:
        return []
    from app.llm.client import get_client

    out = []
    for label, model in (("fast", s.fast_model), ("strong", s.strong_model)):
        try:
            get_client().models.retrieve(model)
            out.append(Check(f"Anthropic {label} model available", True, model))
        except Exception as e:
            out.append(Check(f"Anthropic {label} model available", False, f"{model}: {e}",
                             "Check the API key and FORMLINE_*_MODEL in .env"))
    return out


def check_forms() -> list[Check]:
    from app.dashboard.forms_api import schema_problems
    from app.engines import form_library

    forms = form_library.list_forms()
    out = []
    for need, words in (("SNAP application", ("snap", "food stamps")), ("Medicaid renewal", ("medicaid",))):
        match = next((m for m in forms if any(w in " ".join([m.form_id, m.name, *m.aliases]).lower() for w in words)), None)
        if match is None:
            out.append(Check(f"{need} form in the library", False, fix="Add it under forms/"))
            continue
        schema = form_library.load_schema(match.form_id)
        problems = schema_problems(match.form_id)
        out.append(Check(f"{need} form in the library", True, match.form_id))
        out.append(Check(f"{need} form reviewed with no problems", schema.reviewed and not problems,
                         "; ".join(problems[:2]) or ("not reviewed" if not schema.reviewed else ""),
                         "Dashboard → Forms → fix problems, then Mark reviewed", blocking=False))
    return out


def check_demo_assets() -> list[Check]:
    letter = get_settings().data_dir / "demo" / "medicaid_renewal_letter.pdf"
    return [Check("Demo letter generated", letter.exists(), str(letter),
                  "uv run python scripts/make_demo_letter.py, then print it", blocking=False)]


def run(local_only: bool) -> list[Check]:
    checks = check_env() + check_server() + check_forms() + check_demo_assets()
    if not local_only:
        checks += check_public() + check_twilio_webhooks()
    checks += check_anthropic()
    if local_only:  # Twilio/ngrok settings don't matter for a simulator rehearsal
        skip = {"Twilio credentials in .env", "PUBLIC_BASE_URL is an https ngrok URL"}
        checks = [c for c in checks if c.name not in skip]
    return checks


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # ✅/❌ on Windows consoles
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--local", action="store_true", help="skip Twilio and ngrok checks")
    checks = run(ap.parse_args().local)
    for c in checks:
        mark = "✅" if c.ok else ("❌" if c.blocking else "⚠️ ")
        print(f"{mark} {c.name}" + (f"  ({c.detail})" if c.detail else ""))
        if not c.ok and c.fix:
            print(f"     → {c.fix}")
    failed = [c for c in checks if not c.ok and c.blocking]
    print(f"\n{'READY' if not failed else f'NOT READY: {len(failed)} blocking problem(s)'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
