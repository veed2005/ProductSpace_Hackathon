"""When a spoken answer must be checked with the caller before it counts. Deterministic.

    none          accept it (ordinary, confident answers; values from memory the caller already confirmed)
    read_back     say it back and wait for yes ("I heard March 14, 1988. Is that right?")
    spell         ask them to spell it (a name or street whose spelling can't be known from speech)
    double_entry  sensitive digits: ask them to say it again, never echo it

Verification status kept on each answer: unverified -> read_back -> confirmed; or inferred (accepted without a
read-back because it's ordinary and the recognizer was confident). Exact fields must be confirmed before the
final review passes, unless they were typed (SMS) or came from memory.
"""

from __future__ import annotations

from typing import Literal

from app.contracts import FormField

Mode = Literal["none", "read_back", "spell", "double_entry"]

_NAME_WORDS = ("name", "employer", "street", "email", "city", "school", "company", "nombre", "empleador")
EXACT_TYPES = {"date", "money", "phone", "address", "ssn_last4"}


def is_name_like(field: FormField) -> bool:
    text = f"{field.id} {field.label} {field.profile_key or ''}".lower()
    return field.type == "text" and any(w in text for w in _NAME_WORDS)


def is_person_name(field: FormField) -> bool:
    """A person's name (spelled by surname), not a business, street or school name."""
    text = f"{field.id} {field.label} {field.profile_key or ''}".lower()
    return field.type == "text" and ("full_name" in text or "name" in text) and not any(
        w in text for w in ("employer", "street", "school", "company", "city", "email"))


def is_exact(field: FormField) -> bool:
    if field.exact is not None:
        return field.exact
    return field.type in EXACT_TYPES or is_name_like(field) or field.sensitive


def decide(field: FormField, *, channel: str, provenance: str, confidence: float, spelling_uncertain: bool,
           transformed: bool) -> Mode:
    if provenance == "memory":
        return "none"
    if field.sensitive or field.type == "ssn_last4":
        return "double_entry" if channel == "voice" else "none"
    if transformed:
        return "read_back"  # the form gets something different from what they said: they must hear it
    if channel != "voice":
        return "none"  # typed: what they typed is what goes on the form
    if is_name_like(field):
        return "spell" if spelling_uncertain or confidence < 0.6 else "read_back"
    if field.type in ("date", "phone", "address"):
        return "read_back"
    if is_exact(field) and confidence < 0.9:
        return "read_back"
    if confidence < 0.6:
        return "read_back"
    return "none"


def status_after(mode: Mode, *, channel: str) -> str:
    """Verification status for an answer accepted with this mode and no further question."""
    if mode == "none":
        return "confirmed" if channel != "voice" else "inferred"
    return "unverified"
