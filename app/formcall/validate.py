"""The final review: what still blocks completion, and a short spoken summary. Deterministic.

Blocks completion: required questions without an answer, exact answers nobody confirmed, answers that fail the
schema's validation, important notices not yet presented, and a few contradictions code can see.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from app.contracts import FormField, FormSchema
from app.formcall import language as lang_mod
from app.formcall import notices as notices_mod
from app.formcall import privacy, verify
from app.formcall.values import fold


@dataclass
class Problem:
    kind: str  # missing_required | unverified | invalid | notice | contradiction
    field_id: Optional[str] = None
    notice_id: Optional[int] = None
    detail: str = ""


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return fold(str(a)) == fold(str(b))
    return a == b


def applies(field: FormField, answers: dict) -> bool:
    if field.condition is None:
        return True
    other = (answers.get(field.condition.field) or {}).get("value")
    return other is not None and _same(other, field.condition.equals)


def answered(entry: Optional[dict]) -> bool:
    return bool(entry) and entry.get("value") is not None


def problems(schema: FormSchema, answers: dict, notice_rows: list) -> list[Problem]:
    out: list[Problem] = []
    for n in notice_rows:
        if notices_mod.must_present(n) and n.status in ("pending", "presented"):
            out.append(Problem("notice", notice_id=n.id))
    for f in schema.fields:
        if not applies(f, answers):
            continue
        entry = answers.get(f.id)
        if f.required and not answered(entry):
            out.append(Problem("missing_required", f.id))
            continue
        if not answered(entry):
            continue
        if verify.is_exact(f) and entry.get("verification") in ("unverified", "read_back"):
            out.append(Problem("unverified", f.id))
        if f.validation and f.validation.startswith("^"):
            try:
                if not re.match(f.validation, str(entry["value"])):
                    out.append(Problem("invalid", f.id, f.validation))
            except re.error:
                pass
    size = next((answers[f.id]["value"] for f in schema.fields
                 if f.profile_key == "household_size" and answered(answers.get(f.id))), None)
    members = next((answers[f.id]["value"] for f in schema.fields
                    if "household_member" in f.id and f.type == "yes_no" and answered(answers.get(f.id))), None)
    try:
        if size is not None and int(size) <= 1 and str(members).lower() == "yes":
            out.append(Problem("contradiction", detail="household size is 1 but others are applying"))
    except (TypeError, ValueError):
        pass
    order = {"notice": 0, "missing_required": 1, "unverified": 2, "invalid": 3, "contradiction": 4}
    return sorted(out, key=lambda p: order[p.kind])


def fingerprint(answers: dict) -> str:
    """Changes whenever any answer value changes: a yes given before a change doesn't count after it."""
    flat = sorted((k, json.dumps(v.get("value"), sort_keys=True, default=str)) for k, v in answers.items())
    return hashlib.sha256(json.dumps(flat).encode()).hexdigest()[:16]


_CONTACT = {"name", "address", "phone", "date_of_birth", "email", "mailing_address"}


def summary(form_id: str, schema: FormSchema, answers: dict, language: str) -> str:
    """A short spoken summary: contact details as one phrase, numbers and money said explicitly."""
    parts: list[str] = []
    contact = False
    for f in schema.fields:
        entry = answers.get(f.id)
        if not applies(f, answers) or not answered(entry):
            continue
        key = (f.profile_key or f.id).split(".")[0].split("[")[0]
        if key in _CONTACT or key == "full_name" or f.type in ("address", "phone", "date") and "birth" in f.id:
            contact = True
            continue
        if f.type in ("number", "money"):
            parts.append(f"{lang_mod.label(form_id, f, language)} {privacy.display(f, entry['value'], language)}")
    if contact:
        parts.insert(0, "su nombre y sus datos de contacto" if language == "es" else "your name and contact information")
    if not parts:
        return ""
    joiner = " y " if language == "es" else ", and "
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + joiner + parts[-1]


def read_items(form_id: str, schema: FormSchema, answers: dict, language: str, group: Optional[str] = None) -> list[str]:
    items = []
    for f in schema.fields:
        entry = answers.get(f.id)
        if not applies(f, answers) or not answered(entry) or privacy.is_secret(f):
            continue
        if group and fold(group) not in fold(f.group or "") and fold(f.group or "") not in fold(group):
            continue
        items.append(f"{lang_mod.label(form_id, f, language)}: {privacy.display(f, entry['value'], language, spoken=True)}")
    return items
