"""The receipt: an accurate, privacy-safe record of what Formline entered and did. Built only from stored task
data (answers, their audit fields, notices, workflow state), never from a model's summary of the conversation.

Secrets (PINs, passwords, codes, tokens) never appear; sensitive values are masked (privacy.py). It says
"prepared" unless a submission was verified, and lists what's missing.
"""

from __future__ import annotations

import html as html_mod
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pymupdf

from app.contracts import FormSchema
from app.formcall import privacy
from app.formcall.validate import answered, applies
from app.models import FormNotice, FormRun, Task

STATE_TEXT = {
    "prepared": ("Prepared, not submitted", "Formline filled out and checked this form. It has NOT been sent to the "
                                            "agency."),
    "submitted": ("Submitted", "The receiving system confirmed the submission."),
    "submission_failed": ("Not submitted", "Formline tried to submit this form and it failed. It has NOT been sent."),
    "submission_unverified": ("Submission not confirmed", "Formline could not verify whether this form was "
                                                          "submitted."),
    "needs_attention": ("Needs attention", "The filled form didn't pass Formline's check. It has NOT been sent."),
}
PROVENANCE = {"memory": "from your saved information", "caller": "you said", "corrected": "corrected by you",
              "derived": "worked out from your answers"}


@dataclass
class ReceiptContent:
    subject: str
    text: str
    html: str
    pdf: bytes
    reference: str


def reference_for(task: Task) -> str:
    return f"FL-{task.id:05d}-{task.started_at:%y%m%d}"


def build(*, task: Task, run: FormRun, schema: FormSchema, notices: list[FormNotice], now: Optional[datetime] = None) -> ReceiptContent:
    now = now or datetime.now(timezone.utc)
    ref = reference_for(task)
    answers = task.answers or {}
    state_title, state_line = STATE_TEXT.get(run.state, ("In progress", "This form is not finished."))
    confirmed = bool((run.review or {}).get("approved_at"))
    submission = run.submission or {}

    rows, missing, corrected = [], [], []
    for f in schema.fields:
        if not applies(f, answers) or privacy.is_secret(f):
            continue
        entry = answers.get(f.id) or {}
        if not answered(entry):
            if f.required or entry.get("source") in ("skipped", "unknown"):
                missing.append(f.label)
            continue
        value = privacy.display(f, entry["value"])
        note = PROVENANCE.get(entry.get("provenance"), "")
        if (entry.get("transformed") or entry.get("translated")) and entry.get("normalized"):
            note = f"{note}; you said {entry['normalized']}" if note else f"you said {entry['normalized']}"
        rows.append((f.group or "Other", f.label, value, note))
        for h in entry.get("history", []):
            corrected.append(f"{f.label}: changed from {h.get('value')} to {value}")

    shown_notices = [n for n in notices if n.status != "pending"]
    status_word = {"presented": "told to you", "read_exact": "exact wording read to you, then you said OK", "acknowledged": "you said OK",
                   "disagreed": "you said you don't agree", "skipped": "skipped for now"}

    # ---- text
    lines = [f"Formline receipt - {schema.name}", f"Reference: {ref}", f"Date: {now:%B %d, %Y %H:%M} UTC",
             f"Status: {state_title}. {state_line}"]
    if run.state == "submitted" and submission.get("reference"):
        lines.append(f"Submission reference: {submission['reference']}")
    lines.append(f"You confirmed the final review: {'yes' if confirmed else 'no'}")
    lines.append("")
    lines.append("What Formline entered:")
    group = None
    for g, label, value, note in rows:
        if g != group:
            lines.append(f"  {g}")
            group = g
        lines.append(f"    {label}: {value}" + (f"  ({note})" if note else ""))
    if corrected:
        lines += ["", "Corrections:"] + [f"  {c}" for c in corrected]
    if missing:
        lines += ["", "Not answered yet:"] + [f"  {m}" for m in missing]
    if shown_notices:
        lines += ["", "Important information Formline pointed out:"]
        for n in shown_notices:
            lines.append(f"  - {n.explanation} ({status_word.get(n.status, n.status)}). Form text: \"{n.quote}\"")
    lines += ["", "Formline is a free helper service. It prepares forms; a partner organization submits them. "
                  "Sensitive numbers are shown masked."]
    text = "\n".join(lines)

    # ---- html
    e = html_mod.escape
    body = [f"<h1 style='font-size:20px'>Formline receipt</h1><p><b>{e(schema.name)}</b><br>Reference: {e(ref)}<br>"
            f"{now:%B %d, %Y %H:%M} UTC</p>",
            f"<p style='padding:8px;background:#f2f2f2'><b>{e(state_title)}.</b> {e(state_line)}</p>"]
    if run.state == "submitted" and submission.get("reference"):
        body.append(f"<p>Submission reference: <b>{e(submission['reference'])}</b></p>")
    body.append(f"<p>You confirmed the final review: <b>{'yes' if confirmed else 'no'}</b></p>")
    body.append("<h2 style='font-size:16px'>What Formline entered</h2><table cellpadding='4' style='border-collapse:collapse'>")
    group = None
    for g, label, value, note in rows:
        if g != group:
            body.append(f"<tr><td colspan='3'><b>{e(g)}</b></td></tr>")
            group = g
        body.append(f"<tr><td>{e(label)}</td><td><b>{e(value)}</b></td><td style='color:#666'>{e(note)}</td></tr>")
    body.append("</table>")
    if corrected:
        body.append("<h2 style='font-size:16px'>Corrections</h2><ul>" + "".join(f"<li>{e(c)}</li>" for c in corrected) + "</ul>")
    if missing:
        body.append("<h2 style='font-size:16px'>Not answered yet</h2><ul>" + "".join(f"<li>{e(m)}</li>" for m in missing) + "</ul>")
    if shown_notices:
        body.append("<h2 style='font-size:16px'>Important information Formline pointed out</h2><ul>")
        for n in shown_notices:
            body.append(f"<li>{e(n.explanation)} <i>({e(status_word.get(n.status, n.status))})</i><br>"
                        f"<span style='color:#555'>Form text: \"{e(n.quote)}\"</span></li>")
        body.append("</ul>")
    body.append("<p style='color:#666;font-size:12px'>Formline prepares forms; a partner organization submits them. "
                "Sensitive numbers are shown masked.</p>")
    html = "<html><body style='font-family:Arial,sans-serif'>" + "".join(body) + "</body></html>"

    return ReceiptContent(subject=f"Formline receipt: {schema.name} ({state_title.lower()})", text=text, html=html,
                          pdf=_pdf(html), reference=ref)


def _pdf(html: str) -> bytes:
    """The same receipt as a PDF attachment (PyMuPDF renders the HTML)."""
    story = pymupdf.Story(html=html)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    page = pymupdf.paper_rect("letter")
    more = True
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(page + (54, 54, -54, -54))
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()
