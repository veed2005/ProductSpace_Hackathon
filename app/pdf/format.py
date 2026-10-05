"""Turn an answer into the text a paper form expects.

For the brain's completion step: {f.pdf_field: pdf_value(f, answer) for f in schema.fields ...}.
Memory stores values in machine form (ISO dates, E.164 phones, numbers, booleans, address
objects); US government forms expect 03/14/1988, (217) 555-0104, 1300, a checked box.
"""

import re
from datetime import date
from typing import Any

from app.contracts import FormField
from app.memory import profile as memory


def pdf_value(field: FormField, answer: Any) -> str:
    """The string to write into field.pdf_field for this answer."""
    if field.type == "yes_no":
        key = _yes_no(answer)
        return field.pdf_values.get(key, key)
    if isinstance(answer, (dict, list)):
        answer = memory.format_value(field.profile_key or "value", answer)  # names, addresses
    text = "" if answer is None else str(answer).strip()
    if field.type == "date":
        return _us_date(text)
    if field.type == "phone":
        return _us_phone(text)
    if field.type == "money":
        return _money(text)
    if field.type == "ssn_last4":
        digits = re.sub(r"\D", "", text)
        return digits[-4:] if len(digits) >= 4 else ""
    if field.type == "choice" and field.pdf_values:
        return field.pdf_values.get(text, text)
    return text


def _yes_no(answer: Any) -> str:
    if isinstance(answer, bool):
        return "yes" if answer else "no"
    return "yes" if str(answer).strip().lower() in ("yes", "y", "true", "sí", "si", "1") else "no"


def _us_date(text: str) -> str:
    try:
        return date.fromisoformat(text[:10]).strftime("%m/%d/%Y")
    except ValueError:
        return text  # already in the person's words ("March 1988"): leave it for read-back


def _us_phone(text: str) -> str:
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return text


def _money(text: str) -> str:
    """Whole dollars when there are no cents ("1300"), otherwise two decimals; no "$" (the
    form prints its own)."""
    cleaned = text.replace("$", "").replace(",", "").strip()
    try:
        amount = float(cleaned)
    except ValueError:
        return text
    return str(int(amount)) if amount.is_integer() else f"{amount:.2f}"
