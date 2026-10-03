"""Database models.

JSON columns hold flexible, schema-driven data (form answers, document results)
so the "any form" engine doesn't need a migration per form.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def json_field(default_factory=dict) -> Any:
    return Field(default_factory=default_factory, sa_column=Column(JSON, nullable=False))


class Profile(SQLModel, table=True):
    """One person. A phone number may map to several profiles (shared phones)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    display_name: Optional[str] = None
    preferred_language: str = "en"
    pin_hash: Optional[str] = None
    consent_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProfileFact(SQLModel, table=True):
    """A canonical fact about a person, with provenance and freshness."""

    __table_args__ = (UniqueConstraint("profile_id", "key"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: int = Field(foreign_key="profile.id", index=True)
    key: str  # canonical profile_key, e.g. "address", "monthly_income"
    value: Any = json_field(default_factory=lambda: None)
    source_type: str  # conversation | form | document | seed
    source_ref: Optional[str] = None  # task id, document id, etc.
    confirmed_at: datetime = Field(default_factory=utcnow)
    freshness_days: Optional[int] = None  # None = never goes stale
    sensitive: bool = False
    updated_at: datetime = Field(default_factory=utcnow)


class Form(SQLModel, table=True):
    """Index of the on-disk form library (forms/<form_id>/). Disk is the source of truth."""

    id: str = Field(primary_key=True)  # form_id slug, e.g. "snap_application"
    name: str
    aliases: list[str] = json_field(default_factory=list)
    agency: Optional[str] = None
    description: Optional[str] = None
    reviewed: bool = False
    field_count: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class Session(SQLModel, table=True):
    """Conversation state for a phone number, shared across voice and messaging."""

    id: Optional[int] = Field(default=None, primary_key=True)
    phone: str = Field(index=True, unique=True)
    profile_id: Optional[int] = Field(default=None, foreign_key="profile.id")
    state: str = "new"  # orchestrator state machine position
    pending: dict = json_field()  # what we're waiting on (e.g. which question)
    active_task_id: Optional[int] = Field(default=None, foreign_key="task.id")
    pin_verified_at: Optional[datetime] = None
    last_channel: Optional[str] = None  # sms | voice
    updated_at: datetime = Field(default_factory=utcnow)


class Task(SQLModel, table=True):
    """A unit of work: filling one form or explaining one document."""

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: int = Field(foreign_key="profile.id", index=True)
    kind: str  # fill_form | explain_document
    form_id: Optional[str] = Field(default=None, foreign_key="form.id")
    document_id: Optional[int] = Field(default=None, foreign_key="document.id")
    status: str = "active"  # active | readback | completed | needs_attention | abandoned
    # answers: {field_id: {"value": ..., "source": "memory|asked|corrected|unknown", ...}}
    answers: dict = json_field()
    current_field: Optional[str] = None
    turn_count: int = 0
    output_pdf_path: Optional[str] = None
    verification: dict = json_field()
    started_channel: Optional[str] = None
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: Optional[datetime] = None


class Message(SQLModel, table=True):
    """Transcript line, labeled by channel, for the dashboard."""

    id: Optional[int] = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    profile_id: Optional[int] = Field(default=None, foreign_key="profile.id")
    task_id: Optional[int] = Field(default=None, foreign_key="task.id")
    direction: str  # in | out
    channel: str  # sms | voice
    text: str
    media: list[str] = json_field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class Document(SQLModel, table=True):
    """A letter or document the person sent as photo(s), and its structured explanation."""

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: int = Field(foreign_key="profile.id", index=True)
    media_paths: list[str] = json_field(default_factory=list)
    document_type: Optional[str] = None
    result: dict = json_field()  # structured vision output (summary, deadlines, ...)
    related_form_id: Optional[str] = Field(default=None, foreign_key="form.id")
    confidence: Optional[float] = None
    created_at: datetime = Field(default_factory=utcnow)


class PinGuard(SQLModel, table=True):
    """Wrong-PIN attempts per profile (Lane B). A separate table, so existing databases pick it up
    without a reset. No foreign key on purpose: it holds only counts, and profile deletion
    (forget me, demo reset) never has to know about it."""

    profile_id: int = Field(primary_key=True)
    failed_attempts: int = 0
    locked_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utcnow)


class Reminder(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: int = Field(foreign_key="profile.id", index=True)
    task_id: Optional[int] = Field(default=None, foreign_key="task.id")
    document_id: Optional[int] = Field(default=None, foreign_key="document.id")
    due_at: datetime
    message: str
    status: str = "pending"  # pending | sent | canceled
    sent_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class Activity(SQLModel, table=True):
    """Human-readable log of meaningful actions. Powers run-backs and the dashboard."""

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: Optional[int] = Field(default=None, foreign_key="profile.id", index=True)
    task_id: Optional[int] = Field(default=None, foreign_key="task.id")
    kind: str  # form_started | form_completed | form_verified | document_explained | ...
    description: str
    created_at: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    """Machine-readable metrics events (Section 12)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    type: str = Field(index=True)
    phone: Optional[str] = None
    profile_id: Optional[int] = Field(default=None, foreign_key="profile.id")
    task_id: Optional[int] = Field(default=None, foreign_key="task.id")
    channel: Optional[str] = None
    data: dict = json_field()
    created_at: datetime = Field(default_factory=utcnow)
