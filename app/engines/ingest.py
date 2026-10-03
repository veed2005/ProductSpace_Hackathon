"""Turn an official PDF into a draft form schema. Owner: Lane C.

Called by scripts/ingest_form.py (CLI) and by the dashboard's "Add a new form" upload (Lane D).

The model picks the questions and writes them; code supplies everything it can determine from
the PDF (field names, Yes/No states, max lengths), checks the draft, asks once for a fix, and
then repairs whatever is still wrong so an upload never fails on a bad draft. The result is
written with reviewed=false for a person to check on the dashboard.
"""

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Literal, Optional

import pymupdf as fitz
from pydantic import BaseModel, Field

from app.contracts import FieldCondition, FormField, FormMeta, FormSchema
from app.engines import form_library
from app.llm import client as llm
from app.llm.prompts import ingest as prompts
from app.memory import profile as memory
from app.pdf.fields import OFF, PdfField, WidgetInfo, button_state, decode_name, list_fields_in

log = logging.getLogger(__name__)

FILLABLE = {"text", "checkbox", "radio", "combobox", "listbox"}
MIN_FIELDS, MAX_FIELDS = 3, 60
PAGE_TEXT_CHARS = 600  # start of each page: headings and instructions, enough for context
MAX_PAGE_TEXT = 8000
_FORM_ID = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
# Tooltips that are just the authoring tool's default name carry no meaning.
_GENERIC = re.compile(r"^(text|textfield|check ?box|check_box|radio ?button(list)?|combo ?box|list ?box|"
                      r"button|field|cell|row|table|untitled|date ?time ?field|decimal ?field)[\s_#.\-\d\[\]]*$",
                      re.IGNORECASE)
_SSN = re.compile(r"\bssn\b|social security", re.IGNORECASE)


class DraftField(BaseModel):
    """What the model proposes for one question (flat, so structured output stays simple)."""
    id: str
    label: str
    type: Literal["text", "number", "money", "date", "phone", "yes_no", "choice", "address", "ssn_last4"]
    question_hint: str
    pdf_field: Optional[str] = None
    yes_value: Optional[str] = None
    no_value: Optional[str] = None
    options: list[str] = Field(default_factory=list)
    profile_key: Optional[str] = None
    condition_field: Optional[str] = None
    condition_equals: Optional[str] = None
    required: bool = False
    sensitive: bool = False
    group: Optional[str] = None


class Draft(BaseModel):
    fields: list[DraftField]


def ingest_pdf(pdf_path: Path, *, form_id: str, name: str, aliases: list[str] | None = None,
               model: str | None = None) -> FormSchema:
    """Copy the PDF into forms/<form_id>/, generate schema.json (reviewed=false) and meta.json.

    Raises ValueError for a bad form_id, an unreadable PDF, or a PDF without fillable fields
    (the dashboard shows the message). Raises anthropic.APIError if the model call fails.
    """
    if not _FORM_ID.match(form_id):
        raise ValueError("Form id must be 2-40 lowercase letters, digits or underscores, starting with a letter")
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        raise ValueError(f"This file isn't a readable PDF ({e})") from e
    try:
        fields = [f for f in list_fields_in(doc) if f.type in FILLABLE and not f.flags & 1]
        if not fields:
            raise ValueError("This PDF has no fillable fields. It may be a scanned or flat form.")
        button_labels = _button_labels(doc)
        field_lines = [_describe(f, _field_label(doc, f), button_labels) for f in fields]
        page_text = _page_text(doc)
    finally:
        doc.close()

    by_name = {f.name: f for f in fields}
    problems: list[str] | None = None
    schema = None
    for attempt in range(2):
        draft = llm.structured(
            Draft,
            system=prompts.SYSTEM,
            messages=[{"role": "user", "content": prompts.user_text(
                name=name, field_lines=field_lines, page_text=page_text,
                profile_keys=memory.describe_keys(), problems=problems)}],
            model=model or llm.strong_model(),
            max_tokens=16000,
        )
        schema = _to_schema(draft, form_id, name, by_name, button_labels)
        problems = schema_problems(schema, by_name)
        if not problems:
            break
        log.info("ingest %s: draft %d has %d problem(s)", form_id, attempt + 1, len(problems))
    if problems:
        log.warning("ingest %s: repairing %d remaining problem(s): %s", form_id, len(problems), problems)
        schema = repair(schema, by_name)

    _write(pdf_path, schema, draft_meta(form_id, name, aliases))
    return schema


def draft_meta(form_id: str, name: str, aliases: list[str] | None = None) -> FormMeta:
    return FormMeta(form_id=form_id, name=name, aliases=aliases or [])


# ---------------------------------------------------------------- describing the PDF

def _button_labels(doc: fitz.Document) -> dict[int, str]:
    """Widget xref -> the words printed just right of a checkbox/radio button ("Yes", "No")."""
    labels = {}
    for page in doc:
        words = page.get_text("words")
        for w in page.widgets():
            if w.field_type not in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
                continue
            r, mid = w.rect, (w.rect.y0 + w.rect.y1) / 2
            right = sorted((x for x in words if abs((x[1] + x[3]) / 2 - mid) < 5 and r.x1 - 2 <= x[0] < r.x1 + 45),
                           key=lambda x: x[0])
            if right:
                labels[w.xref] = " ".join(x[4] for x in right[:3])
    return labels


def _field_label(doc: fitz.Document, f: PdfField) -> str:
    """The field's tooltip, or the printed words to its left (or above it) when the tooltip is generic."""
    w0 = f.widgets[0]
    page = doc[w0.page]
    tip = next((w.field_label for w in page.widgets() if w.field_name == f.name), "") or ""
    if tip.strip() and not _GENERIC.match(tip.strip()):
        return " ".join(tip.split())[:120]
    r = fitz.Rect(w0.rect)
    words = page.get_text("words")
    mid = (r.y0 + r.y1) / 2
    on_line = [x for x in words if abs((x[1] + x[3]) / 2 - mid) < 6 and x[2] <= r.x0 + 2]
    left = [x for x in on_line if x[2] > r.x0 - 220] or on_line  # nearest words, else the whole question
    if not left:
        left = [x for x in words if 0 <= r.y0 - x[3] < 14 and x[0] < r.x1 and x[2] > r.x0]
    return " ".join(x[4] for x in sorted(left, key=lambda x: (round(x[1]), x[0])))[-120:] or "(no label)"


def _describe(f: PdfField, label: str, button_labels: dict[int, str]) -> str:
    page = f.widgets[0].page + 1
    extra = ""
    if f.type == "radio":
        states = []
        for w in f.widgets:
            # The radio's own label sits right of each button; fall back to the state name.
            states.append(f"{w.on_state}={_label_for(w, button_labels)!r}")
        extra = "states: " + ", ".join(states)
    elif f.type == "checkbox":
        extra = f"states: {f.on_states[0]} (checked), Off"
    elif f.options:
        extra = "options: " + ", ".join(f.options[:15])
    elif f.max_length:
        extra = f"max {f.max_length} chars"
    return f"{f.name} | {f.type} | p{page} | {label} | {extra}".rstrip(" |")


def _label_for(w: WidgetInfo, button_labels: dict[int, str]) -> str:
    return button_labels.get(w.xref, decode_name(w.on_state or ""))


def _page_text(doc: fitz.Document) -> str:
    parts, total = [], 0
    for page in doc:
        text = " ".join(page.get_text().split())[:PAGE_TEXT_CHARS]
        if total + len(text) > MAX_PAGE_TEXT:
            break
        parts.append(f"[page {page.number + 1}] {text}")
        total += len(text)
    return "\n".join(parts)


# ---------------------------------------------------------------- draft -> schema

def _to_schema(draft: Draft, form_id: str, name: str, by_name: dict[str, PdfField],
               button_labels: dict[int, str]) -> FormSchema:
    out = []
    for d in draft.fields:
        pf = by_name.get(d.pdf_field) if d.pdf_field else None
        ftype, sensitive, validation = d.type, d.sensitive, None
        if _SSN.search(f"{d.id} {d.label} {d.pdf_field or ''}") or ftype == "ssn_last4":
            ftype, sensitive, validation = "ssn_last4", True, r"^\d{4}$"
        pdf_values: dict[str, str] = {}
        if ftype == "yes_no" and pf is not None:
            pdf_values = _yes_no_values(pf, d, button_labels) if pf.is_button else {"yes": "Yes", "no": "No"}
        out.append(FormField(
            id=d.id, label=d.label, type=ftype, required=d.required, question_hint=d.question_hint,
            pdf_field=d.pdf_field, pdf_values=pdf_values, profile_key=d.profile_key or None,
            options=d.options if ftype == "choice" else [],
            condition=FieldCondition(field=d.condition_field, equals=d.condition_equals or "yes")
            if d.condition_field else None,
            validation=validation, sensitive=sensitive, group=d.group,
            max_length=pf.max_length if pf is not None else None,
        ))
    return FormSchema(form_id=form_id, name=name, reviewed=False, fields=out)


def _yes_no_values(pf: PdfField, d: DraftField, button_labels: dict[int, str]) -> dict[str, str]:
    """Yes/No states, most reliable source first: printed labels, then the model, then layout."""
    if pf.type == "checkbox" and len(set(pf.on_states)) == 1:
        return {"yes": pf.on_states[0], "no": OFF}
    if len(pf.widgets) == 2:
        by_label = {}
        for w in pf.widgets:
            word = _label_for(w, button_labels).strip().lower()
            if word.startswith(("yes", "sí", "si")):
                by_label["yes"] = w.on_state
            elif word.startswith("no"):
                by_label["no"] = w.on_state
        if len(by_label) == 2:
            return by_label
    proposed = {"yes": d.yes_value, "no": d.no_value}
    if all(v and button_state(pf, v) is not None for v in proposed.values()) and proposed["yes"] != proposed["no"]:
        return {k: button_state(pf, v) for k, v in proposed.items()}
    if len(pf.widgets) == 2:  # "[ ] Yes [ ] No": the left button is Yes
        left, right = sorted(pf.widgets, key=lambda w: w.rect[0])
        return {"yes": left.on_state, "no": right.on_state}
    return {}


# ---------------------------------------------------------------- checks and repair

def schema_problems(schema: FormSchema, by_name: dict[str, PdfField]) -> list[str]:
    """Everything that would make the schema unusable or wrong, phrased for the model to fix."""
    problems = []
    if not MIN_FIELDS <= len(schema.fields) <= MAX_FIELDS:
        problems.append(f"Return between {MIN_FIELDS} and {MAX_FIELDS} questions (got {len(schema.fields)}).")
    seen_ids: set[str] = set()
    seen_pdf: dict[str, str] = {}
    for f in schema.fields:
        if f.id in seen_ids:
            problems.append(f"Duplicate id {f.id!r}.")
        if f.pdf_field:
            pf = by_name.get(f.pdf_field)
            if pf is None:
                problems.append(f"{f.id}: pdf_field {f.pdf_field!r} is not in the field list.")
            elif f.pdf_field in seen_pdf:
                problems.append(f"{f.id}: pdf_field {f.pdf_field!r} is already used by {seen_pdf[f.pdf_field]!r}.")
            else:
                seen_pdf[f.pdf_field] = f.id
                problems += _value_problems(f, pf)
        if f.condition and f.condition.field not in seen_ids:
            problems.append(f"{f.id}: condition_field {f.condition.field!r} must be an earlier field's id.")
        if f.profile_key and (issue := memory.validate_profile_key(f.profile_key)):
            problems.append(f"{f.id}: profile_key {f.profile_key!r}: {issue}")
        if f.type == "choice" and not f.options:
            problems.append(f"{f.id}: a choice question needs options.")
        if not f.question_hint.strip():
            problems.append(f"{f.id}: question_hint is empty.")
        seen_ids.add(f.id)
    return problems


def _value_problems(f: FormField, pf: PdfField) -> list[str]:
    if f.type == "yes_no" and pf.is_button:
        yes, no = f.pdf_values.get("yes"), f.pdf_values.get("no")
        if not yes or not no or yes == no or button_state(pf, yes) is None or button_state(pf, no) is None:
            return [f"{f.id}: yes_value/no_value must be two different states of {pf.name!r}."]
    if f.type == "choice" and pf.type == "radio":
        bad = [o for o in f.options if button_state(pf, o) in (None, OFF)]
        if bad:
            return [f"{f.id}: options {bad} are not states of {pf.name!r}; use its exact state names."]
    if pf.is_button and f.type not in ("yes_no", "choice"):
        return [f"{f.id}: {pf.name!r} is a {pf.type}; use type yes_no or choice."]
    return []


def repair(schema: FormSchema, by_name: dict[str, PdfField]) -> FormSchema:
    """Drop or fix whatever schema_problems still reports, keeping every usable question."""
    fields: list[FormField] = []
    ids: set[str] = set()
    used_pdf: set[str] = set()
    for f in schema.fields:
        update: dict = {}
        if f.pdf_field and (f.pdf_field not in by_name or f.pdf_field in used_pdf):
            continue  # invented or duplicate box: the question has nowhere to go
        pf = by_name.get(f.pdf_field) if f.pdf_field else None
        if pf is not None and _value_problems(f, pf):
            if f.type == "choice" and pf.type == "radio":
                update["options"] = [decode_name(s) for s in pf.on_states]
            else:
                continue
        fid, n = f.id, 2
        while fid in ids:
            fid, n = f"{f.id}_{n}", n + 1
        update["id"] = fid
        if f.condition and f.condition.field not in ids:
            update["condition"] = None
        if f.profile_key and memory.validate_profile_key(f.profile_key):
            update["profile_key"] = None
        if f.type == "choice" and not f.options and "options" not in update:
            update["type"] = "text"
        if not f.question_hint.strip():
            update["question_hint"] = f"What should I put for {f.label}?"
        fields.append(f.model_copy(update=update))
        ids.add(fid)
        if f.pdf_field:
            used_pdf.add(f.pdf_field)
    if len(fields) < MIN_FIELDS:
        raise ValueError(f"Couldn't build a usable question flow from this PDF ({len(fields)} usable questions).")
    return schema.model_copy(update={"fields": fields[:MAX_FIELDS]})


# ---------------------------------------------------------------- files

def _write(pdf_path: Path, schema: FormSchema, meta: FormMeta) -> None:
    folder = form_library.form_dir(schema.form_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = form_library.pdf_path(schema.form_id)
    if Path(pdf_path).resolve() != target.resolve():
        shutil.copyfile(pdf_path, target)
    meta_path = folder / "meta.json"
    if meta_path.exists():  # re-ingesting: keep the agency/description someone wrote by hand
        old = FormMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        meta = old.model_copy(update={"name": meta.name, "aliases": meta.aliases or old.aliases})
    meta_path.write_text(json.dumps(meta.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    form_library.save_schema(schema)
