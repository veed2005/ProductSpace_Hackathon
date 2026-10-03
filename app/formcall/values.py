"""Deterministic parsing and conversion of spoken answers. No model calls here.

The model may propose a value; these functions decide what it means: dates, amounts and their period
(with the arithmetic for weekly -> monthly), phone numbers, spelled-out letters, and spoken email addresses,
in English and Spanish.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Optional

# ---------------------------------------------------------------- text helpers


def fold(text: str) -> str:
    """Lowercase, no accents, single spaces: for comparing words, never for storing them."""
    t = unicodedata.normalize("NFKD", (text or "").casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.split())


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9ñ']+", fold(text))


# ---------------------------------------------------------------- numbers

_UNITS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiuno": 21,
    "veintiun": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26,
    "veintisiete": 27, "veintiocho": 28, "veintinueve": 29, "treinta": 30, "cuarenta": 40, "cincuenta": 50,
    "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90,
    "cien": 100, "ciento": 100, "doscientos": 200, "doscientas": 200, "trescientos": 300, "trescientas": 300,
    "cuatrocientos": 400, "cuatrocientas": 400, "quinientos": 500, "quinientas": 500, "seiscientos": 600,
    "seiscientas": 600, "setecientos": 700, "setecientas": 700, "ochocientos": 800, "ochocientas": 800,
    "novecientos": 900, "novecientas": 900,
}
_SCALES = {"hundred": 100, "thousand": 1000, "mil": 1000, "million": 1_000_000, "millon": 1_000_000,
           "millones": 1_000_000}
_FILLER = {"and", "y", "a"}


def words_to_number(text: str) -> Optional[int]:
    """'three hundred fifty' -> 350, 'trescientos cincuenta' -> 350, 'mil doscientos' -> 1200. None if the
    text has no number words. Digits are handled elsewhere."""
    total, current, seen = 0, 0, False
    for w in words(text.replace("-", " ")):
        if w in _UNITS:
            current += _UNITS[w]
            seen = True
        elif w in _SCALES:
            scale = _SCALES[w]
            seen = True
            if scale == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * scale
                current = 0
        elif w in _FILLER and seen:
            continue
        elif seen:
            break  # the number ended ("three hundred dollars a week")
    return total + current if seen else None


def parse_number(text: str) -> Optional[float]:
    """The first number in the text, from digits ('$1,300.50', '300') or words ('trescientos')."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    if m:
        try:
            return float(m.group().replace(",", ""))
        except ValueError:
            return None
    n = words_to_number(text or "")
    return float(n) if n is not None else None


def parse_int(text: str) -> Optional[int]:
    n = parse_number(text)
    return int(n) if n is not None and n == int(n) else None


def spoken_digits(text: str) -> str:
    """Digits in order, from digits or digit words: 'two one seven, five five five' -> '217555'."""
    out = []
    for token in re.findall(r"\d|[a-zñ]+", fold(text)):
        if token.isdigit():
            out.append(token)
        elif token in _UNITS and _UNITS[token] < 10:
            out.append(str(_UNITS[token]))
    return "".join(out)


# ---------------------------------------------------------------- money and periods

PERIOD_WORDS = [  # checked in order: "every two weeks" before "week"
    ("biweekly", r"\b(biweekly|bi weekly|every (two|2|other) weeks?|cada (dos|2) semanas|quincenal(mente)?|"
                 r"cada quince dias)\b"),
    ("semimonthly", r"\b(twice a month|semi ?monthly|dos veces al mes)\b"),
    ("week", r"\b(a week|per week|each week|every week|weekly|week|a la semana|por semana|cada semana|semanal(es|mente)?|"
             r"semana)\b"),
    ("month", r"\b(a month|per month|each month|every month|monthly|month|al mes|por mes|cada mes|mensual(es|mente)?|"
              r"mes)\b"),
    ("year", r"\b(a year|per year|each year|yearly|annual(ly)?|year|al ano|por ano|anual(es|mente)?|ano)\b"),
    ("hour", r"\b(an hour|per hour|hourly|hour|la hora|por hora|hora)\b"),
]

# Months per period. Weekly pay x 4.33 is the standard benefits conversion.
TO_MONTH = {"month": 1.0, "week": 4.33, "biweekly": 2.17, "semimonthly": 2.0, "year": 1 / 12}


def parse_period(text: str) -> Optional[str]:
    t = fold(text)
    for period, pattern in PERIOD_WORDS:
        if re.search(pattern, t):
            return period
    return None


def convert_amount(amount: float, from_period: str, to_period: str) -> Optional[float]:
    """Deterministic conversion between pay periods, rounded to whole dollars. None if it can't be done
    (hourly pay needs hours; a paycheck period depends on how often they're paid)."""
    if from_period == to_period:
        return round(amount, 2)
    if from_period not in TO_MONTH or to_period not in TO_MONTH:
        return None
    return float(round(amount * TO_MONTH[from_period] / TO_MONTH[to_period]))


def money_text(amount: float, language: str = "en") -> str:
    whole = amount == int(amount)
    s = f"${amount:,.0f}" if whole else f"${amount:,.2f}"
    return s


PERIOD_SAY = {
    "en": {"week": "each week", "biweekly": "every two weeks", "semimonthly": "twice a month", "month": "each month",
           "year": "each year", "hour": "an hour", "paycheck": "each paycheck"},
    "es": {"week": "a la semana", "biweekly": "cada dos semanas", "semimonthly": "dos veces al mes", "month": "al mes",
           "year": "al año", "hour": "por hora", "paycheck": "por cheque"},
}


# ---------------------------------------------------------------- dates

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
MONTH_NAMES = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"],
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
           "noviembre", "diciembre"],
}


def _year(y: int) -> int:
    if y < 100:
        return 1900 + y if y > (date.today().year % 100) else 2000 + y
    return y


def _make(y: int, m: int, d: int) -> Optional[str]:
    try:
        return date(_year(y), m, d).isoformat()
    except ValueError:
        return None


def parse_date(text: str) -> Optional[str]:
    """ISO date from '1988-03-14', '3/14/1988', 'March 14th, 1988', '14 de marzo de 1988', '22 de julio del 79'."""
    t = fold(text)
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        return _make(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b", t)
    if m:  # US order
        return _make(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    month_re = "|".join(sorted(_MONTHS, key=len, reverse=True))
    m = re.search(rf"\b({month_re})\.? (\d{{1,2}}),? (\d{{2,4}})\b", t)  # March 14 1988
    if m:
        return _make(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
    m = re.search(rf"\b(\d{{1,2}}) (?:de )?({month_re}),? (?:de |del )?(\d{{2,4}})\b", t)  # 14 de marzo de 1988
    if m:
        return _make(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    # Day said in words: "catorce de marzo de 1988", "the fourteenth of March 1988" is rare on STT; numbers win.
    m = re.search(rf"\b([a-z]+(?: y [a-z]+)?) de ({month_re}),? (?:de |del )?(\d{{2,4}})\b", t)
    if m and words_to_number(m.group(1)):
        return _make(int(m.group(3)), _MONTHS[m.group(2)], words_to_number(m.group(1)))
    return None


def plausible_birth_date(iso: str) -> bool:
    d = date.fromisoformat(iso)
    return 1900 <= d.year and d <= date.today()


def say_date(iso: str, language: str = "en") -> str:
    d = date.fromisoformat(iso)
    month = MONTH_NAMES.get(language, MONTH_NAMES["en"])[d.month - 1]
    return f"{d.day} de {month} de {d.year}" if language == "es" else f"{month} {d.day}, {d.year}"


# ---------------------------------------------------------------- phones

def parse_phone(text: str) -> Optional[str]:
    """E.164 for a US number said or typed any way: '(217) 555-0104', 'two one seven five five five...'."""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) < 10:
        digits = spoken_digits(text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"+1{digits}" if len(digits) == 10 else None


def say_phone(e164: str) -> str:
    d = e164[-10:]
    return f"{', '.join(d[:3])}, {', '.join(d[3:6])}, {', '.join(d[6:])}"


# ---------------------------------------------------------------- spelling

_LETTER_NAMES = {
    # English letter names as speech recognition writes them
    "ay": "a", "bee": "b", "be": "b", "cee": "c", "see": "c", "sea": "c", "dee": "d", "ee": "e", "ef": "f",
    "eff": "f", "gee": "g", "aitch": "h", "eye": "i", "jay": "j", "kay": "k", "el": "l", "ell": "l", "em": "m",
    "en": "n", "oh": "o", "pee": "p", "cue": "q", "queue": "q", "ar": "r", "are": "r", "es": "s", "ess": "s",
    "tee": "t", "tea": "t", "you": "u", "vee": "v", "ex": "x", "why": "y", "zee": "z", "zed": "z",
    # Spanish
    "de": "d", "ce": "c", "efe": "f", "ge": "g", "hache": "h", "jota": "j", "ka": "k", "ele": "l", "eme": "m",
    "ene": "n", "ene con tilde": "ñ", "pe": "p", "cu": "q", "erre": "r", "ere": "r", "ese": "s", "te": "t",
    "uve": "v", "ve": "v", "equis": "x", "ye": "y", "zeta": "z", "be grande": "b", "be larga": "b",
}
_TWO_WORD_LETTERS = {"double u": "w", "doble u": "w", "doble ve": "w", "doble uve": "w", "i griega": "y",
                     "ve chica": "v", "ve corta": "v", "be grande": "b", "be larga": "b"}


def parse_spelling(text: str) -> Optional[str]:
    """Letters the caller spelled: 'D-O-U-B-E-K', 'D O U B E K', 'D as in David, O...', 'de, o, u, be, e, ka',
    'double s'. None if this doesn't look like spelling (fewer than 2 letters, or ordinary words)."""
    # "D as in David" / "d de dedo" -> "d" (a single letter, then a word of 3+ letters that only illustrates it)
    t = re.sub(r"\b([a-z])\s+(?:as in|like|como en|de)\s+[a-z]{3,}\b", r"\1", fold(text))
    for phrase, letter in _TWO_WORD_LETTERS.items():
        t = re.sub(rf"\b{phrase}\b", f" {letter} ", t)
    t = re.sub(r"\b(?:capital|mayuscula|letter|letra)\b", " ", t)
    t = t.replace("-", " ").replace(".", " ").replace(",", " ")
    out: list[str] = []
    tokens = t.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("double", "doble") and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            letter = nxt if len(nxt) == 1 else _LETTER_NAMES.get(nxt)
            if letter:
                out += [letter, letter]
                i += 2
                continue
        if len(tok) == 1 and tok.isalpha():
            out.append(tok)
        elif tok in _LETTER_NAMES:
            out.append(_LETTER_NAMES[tok])
        elif tok in ("space", "espacio"):
            out.append(" ")
        elif tok in ("apostrophe", "apostrofe"):
            out.append("'")
        elif tok in ("hyphen", "dash", "guion"):
            out.append("-")
        elif tok in ("and", "y", "then", "luego", "is", "es", "it's", "its", "it", "spelled", "se", "escribe", "that",
                     "eso", "the", "el", "la", "letters", "letras"):
            pass
        else:
            return None  # an ordinary word: not spelling
        i += 1
    letters = "".join(out).strip()
    return letters if sum(c.isalpha() for c in letters) >= 2 else None


def as_name(letters: str) -> str:
    """'doubek' -> 'Doubek', "o'neil" -> "O'Neil", 'garcia-lopez' -> 'Garcia-Lopez'."""
    return re.sub(r"(^|[\s'\-])([a-zñ])", lambda m: m.group(1) + m.group(2).upper(), letters.lower())


def spell_out(word: str) -> str:
    """'Doubek' -> 'D, O, U, B, E, K' (read aloud letter by letter)."""
    parts = []
    for c in word:
        if c.isalpha():
            parts.append(c.upper())
        elif c == "-":
            parts.append("hyphen")
        elif c == "'":
            parts.append("apostrophe")
    return ", ".join(parts)


# ---------------------------------------------------------------- email

_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)+$")
_DOMAIN_SAY = {"gmail.com": "gmail dot com", "yahoo.com": "yahoo dot com", "hotmail.com": "hotmail dot com",
               "outlook.com": "outlook dot com", "icloud.com": "icloud dot com", "aol.com": "a o l dot com"}


def parse_email(text: str) -> Optional[str]:
    """'evan dot doubek at gmail dot com', 'e v a n punto doubek arroba gmail punto com' -> evan.doubek@gmail.com."""
    t = fold(text)
    t = re.sub(r"^(it'?s|my email is|my email address is|es|mi correo es|mi correo electronico es|send it to|"
               r"envialo a)\s+", "", t)
    for spoken, char in ((r"\barroba\b", " @ "), (r"\bat sign\b", " @ "), (r"\bat\b", " @ "),
                         (r"\b(dot|punto)\b", " . "), (r"\b(underscore|guion bajo)\b", " _ "),
                         (r"\b(dash|hyphen|guion|minus)\b", " - "), (r"\bplus\b", " + ")):
        t = re.sub(spoken, char, t)
    # a sequence of single letters is a spelled word: "e v a n" -> "evan"
    compact = re.sub(r"\s+", "", t)
    compact = compact.rstrip(".")
    return compact if _EMAIL_RE.match(compact) else None


def say_email(email: str, language: str = "en") -> str:
    """Spelled back the way it's checked over the phone: 'E, V, A, N, dot, D, O, U, B, E, K, at gmail dot com'."""
    local, domain = email.split("@", 1)
    dot, at = ("punto", "arroba") if language == "es" else ("dot", "at")
    pieces = []
    for c in local:
        if c == ".":
            pieces.append(dot)
        elif c == "_":
            pieces.append("guion bajo" if language == "es" else "underscore")
        elif c == "-":
            pieces.append("guion" if language == "es" else "dash")
        else:
            pieces.append(c.upper())
    said_domain = _DOMAIN_SAY.get(domain)
    if said_domain and language == "es":
        said_domain = said_domain.replace(" dot ", " punto ")
    if not said_domain:
        said_domain = f" {dot} ".join(", ".join(ch.upper() for ch in part) for part in domain.split("."))
    return f"{', '.join(pieces)}, {at} {said_domain}"


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}•••@{domain}" if domain else "•••"


# ---------------------------------------------------------------- names


def name_from_caller(candidate: str, heard: str) -> bool:
    """True if every word of the proposed name is in what the caller actually said (accents and case aside).
    Guards against a model translating or 'correcting' a proper name."""
    said = set(words(heard))
    parts = words(candidate)
    return bool(parts) and all(p in said for p in parts)
