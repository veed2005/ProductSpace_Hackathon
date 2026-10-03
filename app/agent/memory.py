"""What the browser agent remembers about a person between calls.

It uses the canonical profile in app/memory/profile.py, the same facts the form assistant uses, so there is
one memory per person: shown on the dashboard's Memory tab and erased by "forget me".

    recall  The agent only exists after the PIN is accepted (app/agent/call.py), so saved facts reach the model
            only then. Sensitive facts are never shown. Stale ones are marked so the agent checks them first.
    learn   The model may propose `remember` items (key + value the caller said). Code checks the key is a real
            profile path and the value has the right shape; nothing is saved until the caller says yes.
            Saved with source "conversation" and ref "browser_task:<id>".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Union

from app.events import log_activity, log_event
from app.memory import profile as memory

# Never shown to the model and never saved by the agent, whatever the profile says about sensitivity.
NEVER = {"ssn_last4"}

MAX_RECALL_CHARS = 3000

_INT_FIELDS = {"household_size", "hours_per_week"}
_MONEY_FIELDS = {"monthly_income", "housing_cost", "gross_pay", "monthly_amount"}
_BOOL_FIELDS = {"disability_in_household", "is_student", "has_income", "varies", "pays_heating_cooling",
                "pays_electric", "pays_water", "pays_phone"}
_DATE_FIELDS = {"date_of_birth"}
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%d %B %Y")

_FIELD_WORDS = {"apt": "apartment", "zip": "ZIP code", "dob": "date of birth"}


@dataclass
class Proposal:
    path: str  # canonical profile path, e.g. "address.city"
    value: Any  # already converted to the stored shape
    spoken: str  # how it's read back to the caller, e.g. "your date of birth, March 14, 1988"


# ---------------------------------------------------------------- recall

def _show(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def recall(profile_id: int) -> str:
    """The person's saved facts for the agent's prompt, one per line. Empty if there are none."""
    lines = []
    for key, fact in memory.get_facts(profile_id).items():
        spec = memory.CANONICAL_KEYS.get(key)
        if key in NEVER or fact.sensitive or (spec and spec.sensitive) or fact.value in (None, "", [], {}):
            continue
        line = f"- {key}: {_show(fact.value)}"
        if not fact.fresh:
            line += (f"  [last confirmed {fact.confirmed_at:%B %Y}; may be out of date, so check it with the caller "
                     "before using it]")
        lines.append(line)
    text = "\n".join(lines)
    return text if len(text) <= MAX_RECALL_CHARS else text[:MAX_RECALL_CHARS].rsplit("\n", 1)[0]


# ---------------------------------------------------------------- learn

def _leaf(base: str, steps: list) -> str:
    return next((s for s in reversed(steps) if isinstance(s, str)), base)


def _convert(leaf: str, raw: str) -> Any:
    """The value in the shape the profile stores it, or ValueError."""
    text = raw.strip()
    if not text:
        raise ValueError("empty value")
    if leaf in _DATE_FIELDS:
        cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text)
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(cleaned, fmt).date().isoformat()
            except ValueError:
                pass
        raise ValueError(f"{raw!r} is not a date")
    if leaf in _INT_FIELDS:
        m = re.fullmatch(r"\D*(\d{1,3})\D*", text)
        if not m:
            raise ValueError(f"{raw!r} is not a whole number")
        return int(m.group(1))
    if leaf in _MONEY_FIELDS:
        m = re.search(r"\d[\d,]*(?:\.\d+)?", text)
        if not m:
            raise ValueError(f"{raw!r} is not an amount")
        return float(m.group().replace(",", ""))
    if leaf in _BOOL_FIELDS:
        t = text.casefold().rstrip(".!")
        if t in {"yes", "true", "sí", "si", "y"}:
            return True
        if t in {"no", "false", "n"}:
            return False
        raise ValueError(f"{raw!r} is not yes or no")
    if leaf == "preferred_language":
        t = text.casefold()
        if t in {"en", "english", "inglés", "ingles"}:
            return "en"
        if t in {"es", "spanish", "español", "espanol"}:
            return "es"
        raise ValueError(f"{raw!r} is not a supported language")
    if len(text) > 200:
        raise ValueError("value too long")
    return text


def _spoken(path: str, value: Any, language: str) -> str:
    base, steps = memory.parse_path(path)
    leaf = _leaf(base, steps)
    if base == "full_name":
        what = "tu nombre" if language == "es" else "your name"
    elif leaf == base:
        label = memory.FACT_LABELS.get(base, base.replace("_", " ")).lower()
        what = f"tu {label}" if language == "es" else f"your {label}"
    else:
        parent = memory.FACT_LABELS.get(base, base.replace("_", " ")).lower()
        field = _FIELD_WORDS.get(leaf, leaf.replace("_", " "))
        what = f"your {parent} {field}" if language != "es" else f"{field} de {parent}"
    if isinstance(value, bool):
        shown = ("sí" if value else "no") if language == "es" else ("yes" if value else "no")
    elif base == "date_of_birth" or leaf in _DATE_FIELDS:
        d = datetime.fromisoformat(str(value))
        shown = f"{d:%B} {d.day}, {d.year}"
    elif isinstance(value, float):
        shown = f"${value:,.0f}" if value == int(value) else f"${value:,.2f}"
    else:
        shown = str(value)
    return f"{what}, {shown}"


def check(key: str, raw_value: str, *, language: str = "en") -> Union[Proposal, str]:
    """A validated proposal, or why it can't be remembered (fed back to the model)."""
    path = (key or "").strip()
    problem = memory.validate_profile_key(path)
    if problem:
        return f"can't remember {path!r}: {problem}"
    base, steps = memory.parse_path(path)
    real = memory.VIRTUAL_KEYS.get(base, base)
    spec = memory.CANONICAL_KEYS.get(real)
    if real in NEVER or (spec and spec.sensitive):
        return f"{path!r} is sensitive and is never remembered"
    if base not in memory.VIRTUAL_KEYS and spec and spec.fields and (not steps or isinstance(steps[-1], int)):
        example = f"{base}[0].{spec.fields[0]}" if spec.is_list else f"{base}.{spec.fields[0]}"
        return f"{path!r} holds several fields; remember one at a time, like {example!r}"
    try:
        value = _convert(_leaf(base, steps), str(raw_value))
    except ValueError as e:
        return f"can't remember {path!r}: {e}"
    return Proposal(path=path, value=value, spoken=_spoken(path, value, language))


def already_known(profile_id: int, proposal: Proposal) -> bool:
    current = memory.get_value(profile_id, proposal.path)
    if current is None:
        return False
    stored = current.value
    if isinstance(stored, str) and isinstance(proposal.value, str):
        return stored.strip().casefold() == proposal.value.strip().casefold()
    return stored == proposal.value


def refresh(profile_id: int, proposal: Proposal) -> None:
    """The caller repeated what's already saved: that re-confirms it (it's fresh again). No question needed."""
    memory.confirm_fact(profile_id, proposal.path)


def save(profile_id: int, proposals: list[Proposal], *, task_id: Optional[int]) -> int:
    """Save what the caller agreed to. Returns how many were saved. Values never go in the activity log."""
    saved = []
    for p in proposals:
        try:
            memory.set_value(profile_id, p.path, p.value, source_type="conversation",
                             source_ref=f"browser_task:{task_id}" if task_id else "browser_call")
            saved.append(p.path)
        except ValueError:
            continue
    if saved:
        log_activity("memory_saved", "Remembered from a call: " + ", ".join(saved), profile_id=profile_id)
        log_event("agent_memory_saved", profile_id=profile_id, count=len(saved))
    return len(saved)


def offer_text(proposals: list[Proposal], language: str) -> str:
    items = [p.spoken for p in proposals]
    joined = items[0] if len(items) == 1 else ", ".join(items[:-1]) + (" y " if language == "es" else " and ") + items[-1]
    if language == "es":
        return f"¿Quiere que recuerde {joined} para la próxima vez?"
    return f"Would you like me to remember {joined} for next time?"
