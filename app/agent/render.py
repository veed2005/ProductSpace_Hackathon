"""Turning a page snapshot into compact text for the model, and a friendly name for the caller."""

import re
from typing import Optional
from urllib.parse import urlparse

from app.browser.pdf import describe
from app.browser.protocol import PageElement, PageState, TabInfo


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


def _changes(state: PageState, previous: Optional[PageState]) -> tuple[Optional[str], set[int]]:
    """What's new since the agent's last look: a summary line and the indexes of new items. Element ids are
    stable within a document, so a new id is a new control; text is compared by content."""
    if previous is None:
        return None, set()
    if previous.doc_id != state.doc_id:
        return "This is a different page from your last look.", set()
    old_ids = {e.id for e in previous.elements if e.id}
    old_text = {(e.role, e.label) for e in previous.elements if not e.id}
    new = {i for i, e in enumerate(state.elements) if (e.id not in old_ids if e.id else (e.role, e.label) not in old_text)}
    cur_ids = {e.id for e in state.elements if e.id}
    cur_text = {(e.role, e.label) for e in state.elements if not e.id}
    gone = len(old_ids - cur_ids) + len(old_text - cur_text)
    moved = " The address changed." if previous.url != state.url else ""
    if state.elements and len(new) > 0.7 * len(state.elements):
        return "Most of the page changed since your last look." + moved, set()
    if not new and not gone:
        return "Nothing changed since your last look." + moved, set()
    return (f"Since your last look: {len(new)} new item(s), marked with + at the start of the line; "
            f"{gone} item(s) disappeared.{moved}"), new


def page_text(state: PageState, previous: Optional[PageState] = None) -> str:
    """The page as the model reads it. With `previous` (what it saw last time), new lines are marked with +."""
    u = urlparse(state.url)
    location = (u.path or "/") + (("#" + u.fragment) if u.fragment else "")
    head = [f"Site: {site_name(state)}", f"Title: {state.title}", f"Address: {u.netloc}{location}"]
    summary, new = _changes(state, previous)
    if summary:
        head.append(summary)
    if state.dialog_open:
        head.append("A dialog is open; deal with it first.")
    if state.truncated:
        head.append("Snapshot truncated (very long page); scroll down to see the rest.")
    if state.document is not None:
        head.append(describe(state.document))
    lines = [("+ " if i in new else "") + element_line(e) for i, e in enumerate(state.elements)]
    return "\n".join(head + [""] + lines)


def tab_name(tab: TabInfo) -> str:
    """What to call a tab out loud: its site's name, or its title when there's no usable address."""
    if not tab.url:
        return tab.title or "a browser page"
    return site_name(PageState(doc_id="", url=tab.url, title=tab.title))


def tabs_text(tabs: list[TabInfo]) -> str:
    """The window's tab strip as the model reads it. Empty when there's only one tab (nothing to switch to)."""
    if len(tabs) < 2:
        return ""
    lines = []
    for t in tabs:
        marks = [m for m in ("you are here" if t.current else "", "" if t.switchable else "can't be used") if m]
        site = f"{tab_name(t)}: " if t.url else ""
        lines.append(f"{t.handle} {site}{_q(t.title)}" + (f" ({', '.join(marks)})" if marks else ""))
    return "\n".join(lines)


_LOADING = re.compile(r"^(loading|please wait|cargando|espere)\b|^(processing|one moment|un momento)[\s.…!]*$|"
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
    if len(candidates) > 1:  # "Appointments - MyChart", "Reelbox • Social film discovery": the short one;
        shortest = min(len(p) for p in candidates)  # a tie goes to the last part ("Explore • Reelbox")
        return [p for p in candidates if len(p) == shortest][-1]
    if host:
        return host
    return candidates[0] if candidates else "a web page"
