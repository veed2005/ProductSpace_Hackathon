"""Wipe ALL data (database tables, downloaded media, filled PDFs) and reseed the demo. Owner: Lane D.

Works while the server is running.

  uv run python scripts/reset_demo.py            # asks for confirmation
  uv run python scripts/reset_demo.py --yes --returning-phone +1217XXXXXXX
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.dashboard.demo import reset_demo  # noqa: E402
from seed_demo import phones_from_args  # noqa: E402

if __name__ == "__main__":
    phones = phones_from_args()
    if not phones.yes:  # type: ignore[attr-defined]
        answer = input(f"This deletes all data in {get_settings().database_url}. Type 'reset' to continue: ")
        if answer.strip() != "reset":
            sys.exit("Cancelled.")
    print(json.dumps(reset_demo(phones), indent=2))
