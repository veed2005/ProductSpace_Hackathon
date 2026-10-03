"""Turning a page snapshot into compact text for the model, and a friendly name for the caller."""

import re
from typing import Optional
from urllib.parse import urlparse

from app.browser.pdf import describe
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
    if state.truncated:
        head.append("Snapshot truncated (very long page); scroll down to see the rest.")
    if state.document is not None:
        head.append(describe(state.document))
    return "\n".join(head + [""] + [element_line(e) for e in state.elements])


_LOADING = re.compile(r"^(loading|please wait|cargando|espere)|^(processing|one moment|un momento)[\s.…!]*$|"
                      r"^(\w+ing)(…|\.\.\.)$", re.IGNORECASE)


def looks_loading(state: PageState) -> bool:
    """Spinner text, aria-busy, or an in-progress button ("Scheduling…"): wait before deciding."""
    if state.busy:
        return True
    for e in state.elements:
        label = (e.label or "").strip()
        if len(label) > 40:
            continue
        if e.role in ("alert", "text", "heading") and _LOADING.search(label):
            return True
        if e.role == "button" and not e.enabled and label.endswith(("…", "...")):
            return True
    return False


_GENERIC_TITLES = {"home", "welcome", "dashboard", "login", "log in", "sign in", "index", "search results"}
# Interstitials (bot checks, errors) whose titles say nothing about the site.
_INTERSTITIAL = re.compile(r"^(just a moment|attention required|access denied|please wait|loading|checking your "
                           r"browser|one moment|403|404|page not found|error)\b", re.IGNORECASE)


def _host_name(url: str) -> str:
    host = urlparse(url).hostname or ""
    host = re.sub(r"^(www|my|portal|app)\.", "", host)
    core = host.split(".")[0] if "." in host else host
    if core in ("localhost", "127") or not core or core.isdigit():
        return ""
    return core.replace("-", " ").title()


def site_name(state: PageState) -> str:
    """What to call the site out loud: the site's own name if it declares one, else the part of the title
    that names the site, else a tidied host name. Never a raw URL, never a bot-check page's title."""
    if state.site_name:
        return state.site_name
    title = (state.title or "").replace("\u200e", "").replace("\u200f", "").strip()
    host = _host_name(state.url)
    if not title or _INTERSTITIAL.search(title):
        return host or "a web page"
    parts = [p.strip() for p in re.split(r"\s[-|–—:·•]\s", title) if p.strip()]
    candidates = [p for p in parts if p.lower() not in _GENERIC_TITLES and len(p) <= 40]
    squash = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())  # noqa: E731
    for p in candidates:  # "Linky's profile • Letterboxd" on letterboxd.com -> "Letterboxd"
        if host and squash(p) and (squash(p) in squash(host) or squash(host) in squash(p)):
            return p
    if len(candidates) > 1:  # "Appointments - MyChart", "Reelbox • Social film discovery": the short one
        return min(candidates, key=len)
    if host:
        return host
    return candidates[0] if candidates else "a web page"
