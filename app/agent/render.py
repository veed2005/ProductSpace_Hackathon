"""Turning a page snapshot into compact text for the model, and a friendly name for the caller."""

import re
from typing import Optional
from urllib.parse import urlparse

from app.browser.protocol import PageElement, PageState


def _q(text: Optional[str]) -> str:
    return '"' + (text or "").replace('"', "'") + '"'


def element_line(e: PageElement) -> str:
    prefix = "(dialog) " if e.in_dialog and e.role != "dialog" else ""
    if e.role == "heading":
        return f"{prefix}{'#' * min(e.level or 2, 4)} {e.label}"
    if e.role == "text":
        return f"{prefix}  {e.label}"
    if e.role == "alert":
        return f"{prefix}ALERT: {e.label}"
    if e.role == "dialog":
        return f"DIALOG OPEN: {_q(e.label)}"
    parts = [f"{prefix}[{e.id}] {e.role} {_q(e.label)}"]
    if e.group:
        parts.append(f"group={_q(e.group)}")
    if e.sensitive:
        parts.append("value=[private, never type here]")
    elif e.value is not None and e.role not in ("button", "link"):
        parts.append(f"value={_q(e.value)}")
    if e.placeholder and not e.value:
        parts.append(f"placeholder={_q(e.placeholder)}")
    if e.options:
        parts.append("options: " + " | ".join(e.options[:20]))
    if e.checked is not None:
        parts.append("checked" if e.checked else "unchecked")
    if e.selected:
        parts.append("selected")
    if e.expanded is not None:
        parts.append("expanded" if e.expanded else "collapsed")
    if not e.enabled:
        parts.append("(disabled)")
    if e.required:
        parts.append("required")
    if e.invalid:
        parts.append("INVALID")
    if e.description:
        parts.append(f"note={_q(e.description)}")
    if e.href:
        parts.append(f"-> {e.href}")
    return " ".join(parts)


def page_text(state: PageState) -> str:
    u = urlparse(state.url)
    location = (u.path or "/") + (("#" + u.fragment) if u.fragment else "")
    head = [f"Site: {site_name(state)}", f"Title: {state.title}", f"Address: {u.netloc}{location}"]
    if state.dialog_open:
        head.append("A dialog is open; deal with it first.")
    if not state.at_bottom:
        head.append("More content below (scroll down to see it).")
    if state.truncated:
        head.append("Snapshot truncated; scroll to see more.")
    return "\n".join(head + [""] + [element_line(e) for e in state.elements])


_GENERIC_TITLES = {"home", "welcome", "dashboard", "login", "sign in", "index"}


def site_name(state: PageState) -> str:
    """What to call the site out loud: the site's own name if it declares one, else the most specific
    part of the title, else a tidied host name. Never a raw URL."""
    if state.site_name:
        return state.site_name
    parts = [p.strip() for p in re.split(r"\s[-|–—:·]\s", state.title or "") if p.strip()]
    candidates = [p for p in parts if p.lower() not in _GENERIC_TITLES and len(p) <= 40]
    if candidates:
        return candidates[-1]  # "Appointments - MyChart" -> "MyChart"
    host = urlparse(state.url).hostname or ""
    host = re.sub(r"^(www|my|portal|app)\.", "", host)
    core = host.split(".")[0] if "." in host else host
    if core in ("localhost", "127") or not core:
        return "a web page"
    return core.replace("-", " ").title()
