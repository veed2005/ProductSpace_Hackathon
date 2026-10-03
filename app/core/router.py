"""Intent classification. Owner: Lane A."""

from typing import Literal

from pydantic import BaseModel

from app.llm import client as llm

Intent = Literal["fill_form", "explain_document", "status", "forget_me", "help"]


class IntentClassification(BaseModel):
    intent: Intent


def classify_intent(text: str, *, has_media: bool = False) -> Intent:
    """Prefer a structured LLM classification, but fall back to keyword matching."""
    t = text.lower()
    if has_media or any(w in t for w in ("letter", "document", "explain", "notice", "bill")):
        return "explain_document"
    if any(w in t for w in ("what have you done", "where was i", "status")):
        return "status"
    if any(w in t for w in ("forget me", "delete my info")):
        return "forget_me"
    if any(w in t for w in ("form", "apply", "application", "renew", "snap", "medicaid", "food stamps")):
        return "fill_form"

    if llm.available():
        try:
            result = llm.structured(
                IntentClassification,
                system=(
                    "Classify the user's message into one of: fill_form, explain_document, status, "
                    "forget_me, help. Return only the best match."
                ),
                messages=[{"role": "user", "content": text}],
            )
            if result.intent in {"fill_form", "explain_document", "status", "forget_me", "help"}:
                return result.intent
        except Exception:
            pass
    return "help"
