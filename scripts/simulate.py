"""Talk to handle_turn from the terminal, no Twilio needed.

With --image or /image, paths are sent as-is, so with --server they must exist on the server's machine.

  uv run python scripts/simulate.py --phone +15550001111 --channel sms
  uv run python scripts/simulate.py --channel sms --image letter.jpg
  uv run python scripts/simulate.py --server http://localhost:8000   # drive the running server
                                    # (needs FORMLINE_DEV_ENDPOINTS=true) so the dashboard updates live

Type a message and press Enter. Commands: /image <path>  /channel sms|voice  /quit
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.contracts import TurnRequest, TurnResult  # noqa: E402
from app.core.turn import handle_turn  # noqa: E402
from app.db import init_db  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default="+15550001111")
    ap.add_argument("--channel", choices=["sms", "voice"], default="sms")
    ap.add_argument("--image", help="send this image with the first message")
    ap.add_argument("--server", help="send turns to a running server instead of in-process")
    args = ap.parse_args()

    if args.server:
        import httpx

        def turn(req: TurnRequest) -> TurnResult:
            resp = httpx.post(f"{args.server.rstrip('/')}/dev/turn", json=req.model_dump(), timeout=120)
            resp.raise_for_status()
            return TurnResult.model_validate(resp.json())
    else:
        init_db()
        turn = handle_turn
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
        result = turn(TurnRequest(phone=args.phone, channel=channel, text=text, media_paths=pending_media))
        pending_media = []
        print(f"[{channel}] formline> {result.reply}")
        for extra in result.followup_sms:
            print(f"[sms] formline> {extra}")
        if result.end_call:
            print("(call ended)")


if __name__ == "__main__":
    main()
