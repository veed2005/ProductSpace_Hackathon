"""Canonical profile facts with provenance and freshness. Owner: Lane D.

Used by the form engine (prefill) and the document engine (save case numbers, dates).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlmodel import delete, select

from app.db import session_scope
from app.events import log_activity, publish
from app.models import Profile, ProfileFact, Session

# Days until a value should be re-confirmed. None = never stale.
FRESHNESS_POLICY: dict[str, Optional[int]] = {
    "full_name": None,
    "date_of_birth": None,
    "address": 180,
    "phone": 180,
    "preferred_language": None,
    "household_members": 90,
    "employment": 30,
    "monthly_income": 30,
    "housing_cost": 90,
    "utilities": 90,
    "disability_in_household": 180,
    "case_numbers": None,
}
DEFAULT_FRESHNESS_DAYS = 90


@dataclass
class FactView:
    key: str
    value: Any
    source_type: str
    source_ref: Optional[str]
    confirmed_at: datetime
    fresh: bool
    sensitive: bool


def _policy_for(key: str) -> Optional[int]:
    base = key.split("[")[0].split(".")[0]  # household_members[0].name -> household_members
    return FRESHNESS_POLICY.get(base, DEFAULT_FRESHNESS_DAYS)


def _is_fresh(fact: ProfileFact, now: datetime) -> bool:
    if fact.freshness_days is None:
        return True
    confirmed = fact.confirmed_at
    if confirmed.tzinfo is None:  # SQLite drops tzinfo
        confirmed = confirmed.replace(tzinfo=timezone.utc)
    return now - confirmed < timedelta(days=fact.freshness_days)


def get_facts(profile_id: int) -> dict[str, FactView]:
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        rows = s.exec(select(ProfileFact).where(ProfileFact.profile_id == profile_id)).all()
    return {
        f.key: FactView(f.key, f.value, f.source_type, f.source_ref, f.confirmed_at, _is_fresh(f, now), f.sensitive)
        for f in rows
    }


def get_fact(profile_id: int, key: str) -> Optional[FactView]:
    return get_facts(profile_id).get(key)


def set_fact(profile_id: int, key: str, value: Any, *, source_type: str, source_ref: Optional[str] = None,
             sensitive: bool = False) -> None:
    """Insert or overwrite a fact and mark it confirmed now."""
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        fact = s.exec(select(ProfileFact).where(ProfileFact.profile_id == profile_id,
                                                ProfileFact.key == key)).first()
        if fact is None:
            fact = ProfileFact(profile_id=profile_id, key=key, source_type=source_type,
                               freshness_days=_policy_for(key))
        fact.value = value
        fact.source_type = source_type
        fact.source_ref = source_ref
        fact.sensitive = sensitive
        fact.confirmed_at = now
        fact.updated_at = now
        s.add(fact)
        s.commit()
    publish("profile_updated", profile_id=profile_id, key=key)


def confirm_fact(profile_id: int, key: str) -> None:
    """Person said the stored value is still right."""
    with session_scope() as s:
        fact = s.exec(select(ProfileFact).where(ProfileFact.profile_id == profile_id,
                                                ProfileFact.key == key)).first()
        if fact:
            fact.confirmed_at = datetime.now(timezone.utc)
            s.add(fact)
            s.commit()


def forget_profile(profile_id: int) -> None:
    """'Forget me': delete the person's facts and profile. Caller confirms first."""
    with session_scope() as s:
        s.exec(delete(ProfileFact).where(ProfileFact.profile_id == profile_id))
        for sess in s.exec(select(Session).where(Session.profile_id == profile_id)).all():
            sess.profile_id = None
            sess.active_task_id = None
            s.add(sess)
        # TODO(Lane D): also delete/anonymize tasks, documents, messages for this profile.
        profile = s.get(Profile, profile_id)
        if profile:
            s.delete(profile)
        s.commit()
    log_activity("profile_deleted", "Deleted a profile at the person's request")
