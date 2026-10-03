"""Talk to handle_turn from the terminal, no Twilio needed. Owner: Lane A.

  uv run python scripts/simulate.py --phone +15550001111 --channel sms
  uv run python scripts/simulate.py --channel sms --image letter.jpg

Type a message and press Enter. Commands: /image <path>  /channel sms|voice  /quit
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.contracts import TurnRequest  # noqa: E402
from app.core.turn import handle_turn  # noqa: E402
from app.db import init_db  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default="+15550001111")
    ap.add_argument("--channel", choices=["sms", "voice"], default="sms")
    ap.add_argument("--image", help="send this image with the first message")
    args = ap.parse_args()

    init_db()
    channel = args.channel
    pending_media = [args.image] if args.image else []
    print(f"Formline simulator  phone={args.phone}  channel={channel}  (/quit to exit)")

    while True:
        try:
            text = input(f"[{channel}] you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text == "/quit":
            break
        if text.startswith("/channel "):
            channel = text.split(maxsplit=1)[1]
            continue
        if text.startswith("/image "):
            pending_media.append(text.split(maxsplit=1)[1])
            text = ""
        result = handle_turn(TurnRequest(phone=args.phone, channel=channel, text=text, media_paths=pending_media))
        pending_media = []
        print(f"[{channel}] formline> {result.reply}")
        for extra in result.followup_sms:
            print(f"[sms] formline> {extra}")
        if result.end_call:
            print("(call ended)")


if __name__ == "__main__":
    main()
