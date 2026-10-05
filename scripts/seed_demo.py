"""Create the fake demo personas. Safe to run repeatedly.

  uv run python scripts/seed_demo.py
  uv run python scripts/seed_demo.py --returning-phone +1217XXXXXXX   # map Maria to a real demo phone
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.dashboard.demo import DemoPhones, seed_demo  # noqa: E402


def phones_from_args(argv=None) -> DemoPhones:
    d = DemoPhones()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--returning-phone", default=d.returning, help="Maria Garcia (full profile, stale income)")
    ap.add_argument("--new-phone", default=d.new, help="cleared so it is a first-time caller")
    ap.add_argument("--shared-phone", default=d.shared, help="James and Denise Walker share this phone")
    ap.add_argument("--yes", action="store_true", help="(reset only) skip the confirmation prompt")
    args = ap.parse_args(argv)
    phones = DemoPhones(returning=args.returning_phone, new=args.new_phone, shared=args.shared_phone)
    phones.yes = args.yes  # type: ignore[attr-defined]
    return phones


if __name__ == "__main__":
    print(json.dumps(seed_demo(phones_from_args()), indent=2))
