"""Point the Twilio number's webhooks at this server. Owner: Lane B.

Rerun whenever the ngrok URL changes:

  uv run python -m app.channels.twilio_setup                 # use PUBLIC_BASE_URL from .env
  uv run python -m app.channels.twilio_setup --from-ngrok    # read the URL from the local ngrok agent
  uv run python -m app.channels.twilio_setup --url https://x.ngrok-free.app
  uv run python -m app.channels.twilio_setup --check         # show current config, change nothing
  uv run python -m app.channels.twilio_setup --from-ngrok --fallback-url https://handler.twilio.com/twiml/EH...

--fallback-url sets the number's "primary handler fails" URL for calls and texts. Point it at a
TwiML Bin (Twilio-hosted, so it works even when this laptop or ngrok is down), e.g.:
  <Response><Say>Sorry, Formline is offline right now. Please try again in a few minutes.</Say></Response>

Also reports the number's capabilities, the account type, and (on trial accounts) which phones
are verified, since a trial number can only text and call verified phones.
"""

import argparse
import sys
from typing import Optional

import httpx

from app.config import get_settings

MESSAGING_PATH = "/twilio/messaging"
VOICE_PATH = "/twilio/voice"
NGROK_API = "http://127.0.0.1:4040/api/tunnels"


def ngrok_url() -> Optional[str]:
    """The https public URL of the running local ngrok agent, or None."""
    try:
        tunnels = httpx.get(NGROK_API, timeout=2).json().get("tunnels", [])
    except (httpx.HTTPError, ValueError):
        return None
    return next((t["public_url"] for t in tunnels if t.get("public_url", "").startswith("https://")), None)


def webhook_urls(base_url: str) -> tuple[str, str]:
    base = base_url.rstrip("/")
    return base + MESSAGING_PATH, base + VOICE_PATH


def find_number(client, phone_number: str):
    matches = client.incoming_phone_numbers.list(phone_number=phone_number, limit=1)
    return matches[0] if matches else None


def configure(client, phone_number: str, base_url: str, fallback_url: Optional[str] = None):
    """Set the number's Messaging and Voice webhooks (POST), and optionally the fallback URL
    Twilio uses when those fail. Returns the updated number."""
    number = find_number(client, phone_number)
    if number is None:
        raise SystemExit(f"{phone_number} is not a number on this Twilio account (check TWILIO_PHONE_NUMBER).")
    sms_url, voice_url = webhook_urls(base_url)
    fields = dict(sms_url=sms_url, sms_method="POST", voice_url=voice_url, voice_method="POST")
    if fallback_url:
        fields.update(sms_fallback_url=fallback_url, sms_fallback_method="POST",
                      voice_fallback_url=fallback_url, voice_fallback_method="POST")
    return client.incoming_phone_numbers(number.sid).update(**fields)


def report(client, account_sid: str, phone_number: str) -> None:
    number = find_number(client, phone_number)
    if number is None:
        print(f"!! {phone_number} is not a number on this account.")
        return
    caps = number.capabilities or {}
    missing = [c for c in ("voice", "sms", "mms") if not caps.get(c)]
    print(f"Number:     {number.phone_number} ({number.friendly_name})")
    print(f"Capable of: {', '.join(c for c in ('voice', 'sms', 'mms') if caps.get(c)) or 'nothing?'}"
          + (f"   !! missing {', '.join(missing)}" if missing else ""))
    print(f"Messaging:  {number.sms_method} {number.sms_url or '(not set)'}")
    print(f"Voice:      {number.voice_method} {number.voice_url or '(not set)'}")
    print(f"Fallback:   voice {number.voice_fallback_url or '(none)'}  sms {number.sms_fallback_url or '(none)'}")
    account = client.api.accounts(account_sid).fetch()
    print(f"Account:    {account.type}")
    if account.type == "Trial":
        verified = [c.phone_number for c in client.outgoing_caller_ids.list(limit=50)]
        print("Trial accounts can only text/call verified phones. Verified: " + (", ".join(verified) or "none"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="public base URL (default: PUBLIC_BASE_URL)")
    ap.add_argument("--from-ngrok", action="store_true", help="use the running ngrok agent's https URL")
    ap.add_argument("--check", action="store_true", help="only print the current configuration")
    ap.add_argument("--fallback-url", help="URL Twilio uses when the webhooks fail (e.g. a TwiML Bin)")
    args = ap.parse_args(argv)

    s = get_settings()
    if not (s.twilio_account_sid and s.twilio_auth_token and s.twilio_phone_number):
        print("Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER in .env first.")
        return 1
    from twilio.rest import Client

    client = Client(s.twilio_account_sid, s.twilio_auth_token)
    if not args.check:
        base = args.url or (ngrok_url() if args.from_ngrok else s.public_base_url)
        if not base:
            print("No ngrok tunnel found. Start one with: ngrok http 8000")
            return 1
        if not base.startswith("https://") or "example." in base:
            print(f"Refusing to use {base!r}: Twilio needs a real public https URL.")
            return 1
        configure(client, s.twilio_phone_number, base, args.fallback_url)
        print(f"Webhooks now point at {base}")
        if base.rstrip("/") != s.public_base_url.rstrip("/"):
            print(f"!! Set PUBLIC_BASE_URL={base.rstrip('/')} in .env and restart the server "
                  "(signature validation rebuilds URLs from it).")
    report(client, s.twilio_account_sid, s.twilio_phone_number)
    return 0


if __name__ == "__main__":
    sys.exit(main())
