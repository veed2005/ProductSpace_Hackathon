"""Shared data contracts between lanes.

Everything that crosses a lane boundary is defined here. Changing a model in this
file affects other people's code: announce it in the team chat first, keep changes
additive (new optional fields), and never rename or remove a field without agreement.
See docs/TEAM.md.
"""

from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Channel = Literal["sms", "voice"]

# Where a form answer came from. Drives dashboard color-coding.
AnswerSource = Literal["memory", "asked", "corrected", "document", "unknown", "skipped"]


# ---------------------------------------------------------------- turns (channels <-> brain)

class TurnRequest(BaseModel):
    phone: str  # E.164, e.g. "+15550001111"
    channel: Channel
    text: str = ""
    media_paths: list[str] = Field(default_factory=list)  # local files, already downloaded


class TurnResult(BaseModel):
    reply: str  # what to say (voice) or text back (sms)
    end_call: bool = False  # voice only: hang up after speaking
    # Extra texts to send to the person's phone regardless of channel
    # (receipts, "reply with a photo of the letter" during a call).
    followup_sms: list[str] = Field(default_factory=list)
    language: str = "en"  # BCP-47-ish code for TTS voice selection, e.g. "en", "es"


# ---------------------------------------------------------------- form library (ingest -> engine)

class FieldCondition(BaseModel):
    """Ask this field only if another field's answer equals a value."""
    field: str
    equals: Any


class FormField(BaseModel):
    id: str  # stable id within the form, e.g. "applicant_name"
    label: str  # plain-language label for read-back and dashboard
    type: Literal["text", "number", "money", "date", "phone", "yes_no", "choice", "address", "ssn_last4"] = "text"
    required: bool = False
    question_hint: str  # how to ask it, plain language, e.g. "What is your full name?"
    pdf_field: Optional[str] = None  # AcroForm field name; None if not written to the PDF
    # Map an answer to the PDF value, e.g. {"yes": "On", "no": "Off"} for checkboxes.
    pdf_values: dict[str, str] = Field(default_factory=dict)
    # Canonical profile key, e.g. "full_name", "address", "household_members[0].name".
    profile_key: Optional[str] = None
    options: list[str] = Field(default_factory=list)  # for type == "choice"
    condition: Optional[FieldCondition] = None
    validation: Optional[str] = None  # regex, or a hint like "4 digits"
    sensitive: bool = False  # never echo over SMS; mask in receipts
    group: Optional[str] = None  # e.g. "Household", "Income" (for batching and read-back)
    max_length: Optional[int] = None  # chars that fit in the PDF box, if known


class FormSchema(BaseModel):
    form_id: str
    name: str
    version: int = 1
    reviewed: bool = False
    fields: list[FormField]  # in conversational order


class FormMeta(BaseModel):
    form_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)  # "food stamps", "SNAP", "EBT"
    agency: Optional[str] = None
    description: Optional[str] = None


# ---------------------------------------------------------------- documents

class Deadline(BaseModel):
    date: Optional[date] = None
    description: str  # what the deadline is for


class DocumentExplanation(BaseModel):
    document_type: str  # e.g. "benefits renewal notice", "medical bill", "unknown"
    sender: Optional[str] = None
    plain_summary: str  # 2-3 plain sentences, in English (translated at reply time)
    action_required: Optional[str] = None
    deadlines: list[Deadline] = Field(default_factory=list)
    amounts: list[str] = Field(default_factory=list)
    reference_numbers: list[str] = Field(default_factory=list)
    related_form_id: Optional[str] = None
    high_stakes: bool = False  # eviction, court, immigration -> add referral
    confidence: float = 0.0  # 0..1
    unreadable_parts: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- PDF verification

class FieldMismatch(BaseModel):
    pdf_field: str
    expected: str
    actual: str


class VerificationResult(BaseModel):
    ok: bool
    fields_checked: int = 0
    mismatches: list[FieldMismatch] = Field(default_factory=list)
    truncated: list[str] = Field(default_factory=list)  # pdf_field names
    missing_required: list[str] = Field(default_factory=list)  # field ids
