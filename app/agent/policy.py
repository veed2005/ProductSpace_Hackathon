"""Deterministic rules the model can't talk its way around.

- `validate_step`: the step must name an element on the *current* page, the action must fit it, and
  secret fields are off limits.
- `is_consequential`: a backstop that catches final buttons (book, send, pay, delete, ...) even if
  the model forgot to ask. Consequential steps only run after the caller says yes to a stored,
  fingerprinted pending action (`page_fingerprint`).
- `classify_reply` / `is_stop`: yes / no / something else, and "stop" in English or Spanish.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional
from urllib.parse import urlparse

from app.agent.decision import Step
from app.browser.protocol import PageElement, PageState

TEXT_ROLES = {"textbox", "searchbox", "combobox", "spinbutton"}
TOGGLE_ROLES = {"checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"}
SELECT_ROLES = {"select", "listbox"}
SCROLL_VALUES = {"up", "down", "top", "bottom"}
TAB_TARGET = re.compile(r"^(previous|T\d+)$", re.IGNORECASE)
NEEDS_ELEMENT = {"click", "type", "clear", "select", "check", "uncheck", "press_enter", "focus"}


def validate_step(step: Step, page: PageState) -> Optional[str]:
    """None if the step may run on this page, else a short reason (fed back to the model)."""
    if step.action in NEEDS_ELEMENT:
        el = page.control(step.element_id)
        if el is None:
            return f"{step.element_id!r} is not an element on the current page; use an id from the current snapshot"
        if step.action != "focus" and not el.enabled:
            return f"[{el.id}] {el.label!r} is disabled; something else must be done first"
        if step.action in ("type", "clear"):
            if el.sensitive or el.role == "password":
                return f"[{el.id}] is a private field (password, card, SSN, code); ask the person to type it themselves"
            if el.role not in TEXT_ROLES:
                return f"[{el.id}] is a {el.role}, not a text field"
            if step.action == "type" and not (step.value or "").strip():
                return "type needs a value"
        if step.action == "select" and el.role not in SELECT_ROLES:
            return f"[{el.id}] is a {el.role}; for custom lists click it, then click the option"
        if step.action == "select" and not step.value:
            return "select needs the option text as value"
        if step.action in ("check", "uncheck") and el.role not in TOGGLE_ROLES:
            return f"[{el.id}] is a {el.role}, not a checkbox or option"
        if step.action == "uncheck" and el.role == "radio":
            return "a radio option can't be unchecked; choose a different option instead"
        if step.action == "press_enter" and el.role not in TEXT_ROLES:
            return "press_enter only works in a text field"
    elif step.action == "search":
        if not (step.value or "").strip():
            return "search needs the words to search for in value"
        if len(step.value) > 200:
            return "search words are too long"
        if step.element_id:
            el = page.control(step.element_id)
            if el is None:
                return f"{step.element_id!r} is not an element on the current page; leave element_id null to use the site search"
            if el.role not in TEXT_ROLES or el.sensitive:
                return f"[{el.id}] is a {el.role}, not a search box; leave element_id null to use the site search"
    elif step.action == "scroll":
        if (step.value or "down").lower() not in SCROLL_VALUES:
            return "scroll value must be up, down, top or bottom"
    elif step.action == "new_tab":
        if not (step.value or "").strip():
            return "new_tab needs the words to search the web for in value (a site's name, or what the caller wants)"
        if len(step.value) > 200:
            return "new_tab search words are too long"
    elif step.action == "switch_tab":
        if not TAB_TARGET.match((step.value or "").strip()):
            return 'switch_tab value must be "previous" or a tab handle from the tab list, like T2'
    return None


# ---------------------------------------------------------------- consequential actions

# Final verbs: these buttons do something that's hard to undo.
_STRONG = re.compile(
    r"\b(submit|confirm|book|place (?:my |the |your )?order|pay|purchase|buy|send|delete|remove|sign and submit|"
    r"transfer|check ?out|finali[sz]e|unsubscribe|deactivate|close (?:my |your )?account|"
    r"cancel (?:my |this |the |your )?(?:appointment|visit|order|subscription|reservation|booking|plan|account|request))\b",
    re.IGNORECASE)
# Verbs that are final only on a review/confirmation step.
_WEAK = re.compile(r"\b(schedule|renew|request|save|update|apply|finish|complete|agree|accept|enroll|register|"
                   r"reserve|change|done|place)\b", re.IGNORECASE)
# "Schedule an appointment", "Send a message", "Book a visit": these start a flow, they don't finish one.
_STARTS_FLOW = re.compile(r"^\s*\w+(?: \w+)? (?:a|an|new|another)\b", re.IGNORECASE)
_NAV_SAFE = re.compile(r"^\s*(next|continue|back|previous|go back|close|search|show more|more|view|see|open|"
                       r"edit|change \w+$|add to calendar|print|help|log ?in|sign ?in|menu|home)\b", re.IGNORECASE)
_REVIEW_PAGE = re.compile(r"\b(review|confirm|summary|verify|almost done|before (?:you|we) (?:submit|schedule|send|pay))\b",
                          re.IGNORECASE)
_CONSENT = re.compile(r"\b(i agree|agree to|consent|authori[sz]e|certify|attest|accept the)\b", re.IGNORECASE)


def _is_review_page(page: PageState) -> bool:
    heads = " ".join(e.label for e in page.elements if e.role in ("heading", "dialog")) + " " + page.title
    return bool(_REVIEW_PAGE.search(heads))


def is_consequential(step: Step, page: PageState) -> bool:
    el = page.control(step.element_id)
    if el is None:
        return False
    label = el.label or ""
    if step.action in ("check",) and el.role == "checkbox" and _CONSENT.search(label):
        return True  # agreeing to terms on the person's behalf
    if step.action not in ("click", "press_enter"):
        return False  # typing and choosing options only fill in a draft
    if step.action == "press_enter":
        return _is_review_page(page)
    if el.role not in ("button", "link", "menuitem"):
        return False
    if _NAV_SAFE.search(label) or _STARTS_FLOW.search(label):
        return False
    if _STRONG.search(label):
        return True
    if _is_review_page(page) and (el.role == "button" or _WEAK.search(label)):
        return True
    return False


def page_fingerprint(page: PageState, element_id: Optional[str]) -> str:
    """What the caller agreed to: this tab, this page, this element, the text on the page (a review page's
    summary is the thing being agreed to), and every value in the form. If any of it changes before the caller's
    yes arrives, the stored action is not run and the agent looks again."""
    el = page.control(element_id)
    controls = [(e.role, e.label, e.value, e.checked, e.selected) for e in page.elements]
    raw = repr((page.tab_id, urlparse(page.url)._replace(query="").geturl(), page.doc_id,
                (el.role, el.label) if el else None, controls))
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------- caller replies

def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", (text or "").casefold()).split())


_YES = re.compile(r"^(yes|yeah|yep|yup|ya|sure|ok|okay|correct|right|that's right|go ahead|do it|please do|"
                  r"book it|send it|confirm|absolutely|definitely|of course|please|sí|si|claro|dale|correcto|"
                  r"hazlo|adelante|por favor)\b")
_NO = re.compile(r"^(no|nope|nah|don't|do not|not yet|not now|wait|hold on|cancel|stop|never ?mind|no gracias|"
                 r"espera|todavía no|todavia no|mejor no)\b")
_HEDGE = re.compile(r"\b(but|instead|actually|change|different|other|pero|mejor|cambia)\b")
_STOP = re.compile(r"^(stop|stop it|stop that|stop now|please stop|cancel|cancel that|cancel it|never ?mind|"
                   r"hold on|wait|pause|para|pare|alto|detente|espera|cancela)\b")


# Yes, no and stop in the other call languages (French, German, Hindi, Russian, Portuguese, Japanese, Italian,
# Dutch). Matched on the first word of the raw reply, because _norm drops the vowel signs of Devanagari.
_YES_WORDS = {"oui", "ouais", "ja", "jawohl", "sim", "sì", "da", "да", "हाँ", "हां", "जी", "haan", "han", "haanji"}
_NO_WORDS = {"non", "nein", "nee", "não", "nao", "нет", "nyet", "नहीं", "नही", "nahi", "nahin"}
_STOP_WORDS = {"stopp", "halt", "arrête", "arrete", "arrêtez", "arretez", "ferma", "fermati", "basta", "стоп",
               "хватит", "रुको", "रुकिए", "ruko", "rukiye"}
_YES_JA, _NO_JA, _STOP_JA = ("はい", "ええ", "お願いします"), ("いいえ", "いや", "やめ"), ("ストップ", "止め", "やめ")
_HEDGE_WORDS = {"mais", "aber", "maar", "ma", "но", "lekin", "लेकिन", "par", "पर", "でも"}
_TRIM = " \t.,!?¡¿;:।、。！？"


def _first_words(text: str) -> list[str]:
    return [w.strip(_TRIM) for w in (text or "").casefold().replace(",", " ").split() if w.strip(_TRIM)]


def _other_language_reply(text: str) -> str:
    raw = (text or "").strip(_TRIM)
    if raw.startswith(_NO_JA):
        return "no"
    if raw.startswith(_YES_JA):
        return "yes" if len(raw) <= 12 else "other"
    ws = _first_words(text)
    if not ws:
        return "other"
    if ws[0] in _NO_WORDS:
        return "no"
    # A foreign "yes" is consent only when that's all it is: short, and no "but".
    if ws[0] in _YES_WORDS and len(ws) <= 4 and not any(w in _HEDGE_WORDS for w in ws):
        return "yes"
    return "other"


def classify_reply(text: str, language: str = "en") -> str:
    """'yes', 'no', or 'other' (a new instruction, e.g. 'yes but make it Tuesday'). `language` is the call's:
    the other languages' words count only on a call held in one of them, so "SIM card" is never a Portuguese yes."""
    t = _norm(text)
    if not t:
        return "other"
    if _NO.match(t):
        return "no"
    if _YES.match(t):
        return "other" if _HEDGE.search(t) else "yes"
    return _other_language_reply(text) if language not in ("en", "es") else "other"


def is_stop(text: str) -> bool:
    if _STOP.match(_norm(text)):
        return True
    ws = _first_words(text)
    return bool(ws and ws[0] in _STOP_WORDS) or (text or "").strip(_TRIM).startswith(_STOP_JA)
