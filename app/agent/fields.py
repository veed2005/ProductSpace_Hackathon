"""Form fields on a web page: one question at a time, asked the way a person would, and answers typed the way
the field wants them.

- `question_problem`: an ask_user question that covers several of the page's empty fields at once, or reads a
  format hint ("MM-DD-YYYY", "###-####") aloud. The model gets one chance to reword it.
- `strip_hints`: removes format hints from anything spoken, as a last resort.
- `fit_value`: when a field shows a date format, turns what is typed ("March 14, 1988", a saved 1988-03-14) into
  that format (03-14-1988); when it shows a number pattern like (###) ###-####, a phone number is typed that
  way. Anything that doesn't fit is typed as it is.
"""

from __future__ import annotations

import datetime
import re
from typing import Optional

from app.browser.protocol import PageElement, PageState

# ---------------------------------------------------------------- format hints

MONTH_TOKENS = {"m", "mm"}
DAY_TOKENS = {"d", "dd", "jj", "tt"}  # JJ (jour), TT (Tag)
YEAR_TOKENS = {"yy", "yyyy", "aa", "aaaa", "jjjj"}  # AAAA (año, ano), JJJJ (Jahr)

_DATE_HINT = re.compile(r"(?<![A-Za-z])([MDYAJT]{1,4})([-/. ])([MDYAJT]{1,4})\2([MDYAJT]{1,4})(?![A-Za-z])",
                        re.IGNORECASE)
# "###-###-####", "(XXX) XXX-XXXX", "XXX-XX-XXXX"
_MASK_HINT = re.compile(r"(?<![\w#])\(?[#X]{2,4}\)?[ .-]?[#X]{2,4}[ .-][#X]{3,4}(?![#X\w])", re.IGNORECASE)
# The hint plus what usually wraps it: "(MM-DD-YYYY)", "[format: mm/dd/yyyy]", "in the format MM/DD/YYYY"
_WRAPPED = r"\s*(?:,\s*)?(?:(?:in )?(?:the )?format:?\s*|like\s+|as\s+)?[\(\[]?\s*(?:format:?\s*)?{hint}\s*[\)\]]?"


def _date_order(match: re.Match) -> Optional[list[str]]:
    """['MM', 'DD', 'YYYY'] in the hint's order, or None if it isn't one month, one day and one year."""
    tokens = [match.group(1), match.group(3), match.group(4)]
    kinds = []
    for t in tokens:
        low = t.lower()
        kind = "m" if low in MONTH_TOKENS else "d" if low in DAY_TOKENS else "y" if low in YEAR_TOKENS else None
        if kind is None:
            return None
        kinds.append(kind)
    return tokens if sorted(kinds) == ["d", "m", "y"] else None


def hints_in(text: str) -> list[str]:
    """Format hints written in the text, as written."""
    found = [m.group(0) for m in _DATE_HINT.finditer(text or "") if _date_order(m)]
    found += [m.group(0) for m in _MASK_HINT.finditer(text or "")]
    return found


def strip_hints(text: str) -> str:
    """The text without any format hint, so a caller never hears "M M D D Y Y Y Y"."""
    for hint in hints_in(text):
        text = re.sub(_WRAPPED.format(hint=re.escape(hint)), "", text, count=1, flags=re.IGNORECASE)
    text = re.sub(r"\s+([?.!,;:])", r"\1", text)
    return " ".join(text.split())


# ---------------------------------------------------------------- one question at a time

# A site's search box is not a form field: "What name should I search for?" is one question.
FIELD_ROLES = {"textbox", "combobox", "spinbutton", "select", "listbox"}


def _core(label: str) -> str:
    """A field's label as it might come up in a question: no format hint, no "(required)", no punctuation."""
    label = strip_hints(label or "")
    label = re.sub(r"\([^)]*\)|\[[^\]]*\]|\*|\brequired\b|\boptional\b", " ", label, flags=re.IGNORECASE)
    return " ".join(re.sub(r"[^\w\s]", " ", label.casefold()).split())


def empty_fields(page: PageState) -> list[PageElement]:
    """Fields on the page still waiting for an answer."""
    return [e for e in page.elements
            if e.id and e.role in FIELD_ROLES and e.enabled and not e.sensitive and not (e.value or "").strip()]


def fields_asked(say: str, page: PageState) -> list[str]:
    """The labels of the page's empty fields that the question mentions, longest first so "last name" isn't
    also counted as "name"."""
    spoken = f" {_core(say)} "
    hits = []
    for core in sorted({_core(e.label) for e in empty_fields(page)}, key=len, reverse=True):
        if len(core) >= 3 and f" {core} " in spoken:
            hits.append(core)
            spoken = spoken.replace(f" {core} ", " | ")
    return hits


def question_problem(say: str, page: PageState) -> Optional[str]:
    """None if the question asks for one thing in plain words, else what to fix (fed back to the model)."""
    problems = []
    asked = fields_asked(say, page)
    if len(asked) > 1 or len(re.findall(r"[?？]", say or "")) > 1:
        what = f" ({', '.join(asked)})" if len(asked) > 1 else ""
        problems.append(f"Your question asks for several things at once{what}. Ask for ONE field only, the first "
                        "one the caller needs to answer. Fill it in when they reply, then ask for the next.")
    hints = hints_in(say)
    if hints:
        problems.append(f"Your question reads a format hint aloud ({hints[0]}). Ask in plain words, like \"What's "
                        "your date of birth?\", and put their answer into that format yourself when you type it.")
    return " ".join(problems) or None


# ---------------------------------------------------------------- typing in the field's format

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}


def date_format(el: Optional[PageElement]) -> Optional[tuple[list[str], str]]:
    """The date format a text field shows in its label, placeholder or description: (tokens, separator)."""
    if el is None or el.role not in ("textbox", "combobox", "spinbutton"):
        return None
    for text in (el.label, el.placeholder, el.description):
        for m in _DATE_HINT.finditer(text or ""):
            order = _date_order(m)
            if order:
                return order, m.group(2)
    return None


def _year(raw: str) -> int:
    y = int(raw)
    if len(raw) <= 2:  # "88" -> 1988, "05" -> 2005
        y += 2000 if y <= datetime.date.today().year % 100 else 1900
    return y


def _make(y: int, m: int, d: int) -> Optional[datetime.date]:
    try:
        return datetime.date(y, m, d)
    except ValueError:
        return None


def parse_date(value: str, order: list[str]) -> Optional[datetime.date]:
    """Read a date the caller said or memory holds. Numbers without a month name are read in the field's own
    month/day order first (a caller filling an MM-DD form says 03/04 meaning March 4th)."""
    text = (value or "").strip().casefold()
    kinds = ["m" if t.lower() in MONTH_TOKENS else "d" if t.lower() in DAY_TOKENS else "y" for t in order]
    m = re.fullmatch(r"(\d{4})[-/. ](\d{1,2})[-/. ](\d{1,2})", text)  # 1988-03-14
    if m:
        return _make(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    words = re.findall(r"[^\W\d_]+|\d+", text)
    named = [_MONTHS[w] for w in words if w in _MONTHS]
    numbers = [w for w in words if w.isdigit()]
    if named and len(numbers) == 2:  # "March 14th, 1988", "14 de marzo de 1988"
        y = next((i for i, n in enumerate(numbers) if len(n) == 4), 1)
        return _make(_year(numbers[y]), named[0], int(numbers[1 - y]))
    if named or not re.fullmatch(r"[\d\s/.,-]+", text):
        return None
    if len(numbers) == 1 and len(numbers[0]) == 8:  # 03141988, in the field's order
        digits, numbers = numbers[0], []
        for k in kinds:
            size = 4 if k == "y" else 2
            numbers.append(digits[:size])
            digits = digits[size:]
    if len(numbers) != 3:
        return None
    parts = dict(zip(kinds, numbers))
    if kinds[0] != "y" and len(numbers[0]) == 4:  # year first although the field puts it last
        parts = {"y": numbers[0], "m": numbers[1], "d": numbers[2]}
    elif kinds[0] == "y" and len(numbers[2]) == 4:  # year last although the field puts it first
        md = [k for k in kinds if k != "y"]
        parts = {md[0]: numbers[0], md[1]: numbers[1], "y": numbers[2]}
    y = _year(parts["y"])
    return _make(y, int(parts["m"]), int(parts["d"])) or _make(y, int(parts["d"]), int(parts["m"]))


def _fit_mask(value: str, el: PageElement) -> Optional[str]:
    """A number written into the pattern the field shows: +12175550104 into "(###) ###-####" is (217) 555-0104.
    Only when the value is nothing but a number and has exactly as many digits as the pattern (a leading
    country code 1 is dropped if that makes it fit)."""
    if el.role not in ("textbox", "combobox") or not re.fullmatch(r"[\d\s()+.\-]+", value):
        return None
    digits = re.sub(r"\D", "", value)
    for text in (el.placeholder, el.label, el.description):
        m = _MASK_HINT.search(text or "")
        if not m:
            continue
        mask = m.group(0)
        if mask.startswith("(") and ")" not in mask:  # the bracket around the hint, as in "Phone (XXX-XXX-XXXX)"
            mask = mask[1:]
        slots = len(re.findall(r"[#Xx]", mask))
        if len(digits) == slots + 1 and digits[0] == "1":
            digits = digits[1:]
        if len(digits) != slots:
            return None
        fill = iter(digits)
        return re.sub(r"[#Xx]", lambda _: next(fill), mask)
    return None


def fit_value(value: Optional[str], el: Optional[PageElement]) -> Optional[str]:
    """`value` written the way the field asks for it, or unchanged when the field shows no format or the value
    doesn't fit it."""
    if value and el is not None:
        masked = _fit_mask(value.strip(), el)
        if masked:
            return masked
    fmt = date_format(el)
    if not fmt or not value:
        return value
    order, sep = fmt
    when = parse_date(value, order)
    if when is None:
        return value
    out = []
    for t in order:
        low = t.lower()
        if low in MONTH_TOKENS:
            out.append(f"{when.month:02d}" if len(low) == 2 else str(when.month))
        elif low in DAY_TOKENS:
            out.append(f"{when.day:02d}" if len(low) == 2 else str(when.day))
        else:
            out.append(f"{when.year:04d}" if len(low) == 4 else f"{when.year % 100:02d}")
    return sep.join(out)
