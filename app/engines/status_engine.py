"""'What have you done for me?'"""

from sqlmodel import select

from app.db import session_scope
from app.models import Activity


def recent_activity(profile_id: int, limit: int = 5) -> list[str]:
    """Most recent human-readable activity descriptions, newest first."""
    with session_scope() as s:
        rows = s.exec(
            select(Activity).where(Activity.profile_id == profile_id)
            .order_by(Activity.created_at.desc()).limit(limit)
        ).all()
    return [a.description for a in rows]
