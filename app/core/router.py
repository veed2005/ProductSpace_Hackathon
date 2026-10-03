"""Intent classification. Owner: Lane A."""

from typing import Literal

Intent = Literal["fill_form", "explain_document", "status", "forget_me", "help"]


def classify_intent(text: str, *, has_media: bool = False) -> Intent:
    """Keyword placeholder. TODO(Lane A): LLM classification via llm.structured()."""
    t = text.lower()
    if has_media or any(w in t for w in ("letter", "document", "explain", "notice", "bill")):
        return "explain_document"
    if any(w in t for w in ("what have you done", "where was i", "status")):
        return "status"
    if any(w in t for w in ("forget me", "delete my info")):
        return "forget_me"
    if any(w in t for w in ("form", "apply", "application", "renew", "snap", "medicaid", "food stamps")):
        return "fill_form"
    return "help"
