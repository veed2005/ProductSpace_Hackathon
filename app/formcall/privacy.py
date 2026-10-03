"""One masking policy for everything that leaves the conversation: speech, texts, the dashboard, receipts.

    secret     PINs, passwords, codes, tokens, card numbers: never shown, spoken, or emailed at all
    sensitive  shown masked: an SSN's last four as ***-**-1234, other values as ****4321
"""

from __future__ import annotations

import re
from typing import Any, Optional

from app.contracts import FormField
from app.formcall import values
from app.memory import profile as memory

_SECRET = re.compile(r"\b(pin|password|passcode|pass code|otp|one time code|verification code|security code|"
                     r"token|cvv|cvc|card number|credit card|debit card)\b")


def is_secret(field: FormField) -> bool:
    return bool(_SECRET.search(values.fold(f"{field.id.replace('_', ' ')} {field.label}")))


def mask(field: FormField, text: str) -> str:
    digits = re.sub(r"\D", "", text or "")
    if field.type == "ssn_last4":
        return f"***-**-{digits[-4:]}" if len(digits) >= 4 else "***-**-****"
    tail = (digits or text or "")[-4:]
    return f"****{tail}" if len(digits or text or "") > 4 else "****"


def display(field: FormField, value: Any, language: str = "en", *, spoken: bool = False,
            masked: Optional[bool] = None) -> str:
    """A value as a person reads or hears it. Sensitive values are masked unless masked=False is asked for
    explicitly (only the PDF itself gets the real value)."""
    if value is None:
        return ""
    if is_secret(field):
        return "[not shown]"
    if field.sensitive or field.type == "ssn_last4":
        if masked is not False:
            return mask(field, str(value))
    if field.type == "yes_no":
        yes = str(value).strip().lower() in ("yes", "true", "1", "sí", "si")
        return ("sí" if yes else "no") if language == "es" else ("yes" if yes else "no")
    if field.type == "date":
        try:
            return values.say_date(str(value), language)
        except ValueError:
            return str(value)
    if field.type == "money":
        try:
            return values.money_text(float(value))
        except (TypeError, ValueError):
            return str(value)
    if field.type == "phone":
        digits = re.sub(r"\D", "", str(value))[-10:]
        if len(digits) == 10:
            return values.say_phone("+1" + digits) if spoken else f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if isinstance(value, (dict, list)):
        return memory.format_value(field.profile_key or "value", value)
    return str(value)
