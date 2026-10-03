"""Configurable referral guidance for high-stakes documents."""

import json
from functools import lru_cache
from pathlib import Path


@lru_cache
def _referrals() -> dict[str, str]:
    path = Path(__file__).with_name("referrals.json")
    return json.loads(path.read_text(encoding="utf-8"))


def referral_for(language: str) -> str:
    referrals = _referrals()
    return referrals.get(language, referrals["en"])