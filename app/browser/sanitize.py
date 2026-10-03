"""Server-side scrub of page snapshots (the extension scrubs first; this is the second layer).

Anything that looks like a secret never reaches the model, the database, or the dashboard:
password and sensitive field values are dropped, and SSN- or card-shaped numbers are masked in
every label, value, and text block.
"""

import re

from app.browser.protocol import PageElement, PageState

MAX_ELEMENTS = 300
MAX_TEXT = 300
MAX_LABEL = 160

_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)|(?i:(?:ssn|social security)\D{0,20})\d{9}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_SENSITIVE_NAME = re.compile(
    r"pass(word|code)|\bpin\b|ssn|social.?security|card.?number|credit.?card|cvv|cvc|security.?code|"
    r"routing|account.?number|one.?time|otp|verification.?code", re.IGNORECASE)


def mask(text: str) -> str:
    text = _SSN.sub("[redacted SSN]", text)
    return _CARD.sub("[redacted number]", text)


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_sensitive(el: PageElement) -> bool:
    return el.sensitive or el.role == "password" or el.input_type == "password" or bool(
        _SENSITIVE_NAME.search(el.label or "") or _SENSITIVE_NAME.search(el.placeholder or ""))


def sanitize_element(el: PageElement) -> PageElement:
    sensitive = is_sensitive(el)
    limit = MAX_TEXT if el.role in ("text", "heading", "alert", "dialog") else MAX_LABEL
    return el.model_copy(update={
        "label": mask(_clip(el.label, limit) or ""),
        "value": None if sensitive else (mask(_clip(el.value, MAX_TEXT)) if el.value is not None else None),
        "sensitive": sensitive,
        "description": mask(_clip(el.description, MAX_LABEL)) if el.description else None,
        "placeholder": _clip(el.placeholder, 80),
        "options": [mask(_clip(o, 80) or "") for o in el.options[:40]],
        "group": _clip(el.group, 80),
        "href": _clip(el.href, 200),
    })


def sanitize_page(state: PageState) -> PageState:
    elements = [sanitize_element(e) for e in state.elements[:MAX_ELEMENTS]]
    return state.model_copy(update={
        "elements": elements,
        "title": mask(_clip(state.title, 160) or ""),
        "url": _strip_query(state.url),
        "truncated": state.truncated or len(state.elements) > MAX_ELEMENTS,
    })


def _strip_query(url: str) -> str:
    """Query strings often carry tokens or personal data; keep origin, path and fragment."""
    if "?" not in url:
        return url
    base, rest = url.split("?", 1)
    return base + ("#" + rest.split("#", 1)[1] if "#" in rest else "")
