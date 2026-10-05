"""Activity log, metrics events, transcript, and live dashboard broadcast.

Every part of the app calls these. They never raise into the caller: logging must not break a
conversation.

Dashboard event types (payload is a dict, always includes "type"):
  message            {phone, profile_id, task_id, direction, channel, text, media}
  task_updated       {task_id, profile_id, form_id, status}
  field_filled       {task_id, field_id, label, value, source}   (value masked if sensitive)
  document_explained {document_id, profile_id, explanation, media_paths}
  verification       {task_id, ok, result}
  activity           {profile_id, task_id, kind, description}
  profile_updated    {profile_id, key}
"""

import asyncio
import logging
from typing import Any, Optional

from app.db import session_scope
from app.models import Activity, Event, Message

log = logging.getLogger(__name__)

_subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []


# ---------------------------------------------------------------- live broadcast

def subscribe() -> asyncio.Queue:
    """Call from an async context (e.g. the dashboard SSE endpoint)."""
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.append((asyncio.get_running_loop(), q))
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers[:] = [(loop, sq) for loop, sq in _subscribers if sq is not q]


def publish(event_type: str, **payload: Any) -> None:
    """Thread-safe: callable from sync code running in FastAPI's threadpool."""
    event = {"type": event_type, **payload}
    for loop, q in list(_subscribers):
        try:
            loop.call_soon_threadsafe(q.put_nowait, event)
        except Exception:  # closed loop or full queue: drop, never block a turn
            log.debug("dropping dashboard event for a dead subscriber")


# ---------------------------------------------------------------- persistence

def log_message(phone: str, direction: str, channel: str, text: str, *,
                profile_id: Optional[int] = None, task_id: Optional[int] = None,
                media: Optional[list[str]] = None) -> None:
    try:
        with session_scope() as s:
            s.add(Message(phone=phone, direction=direction, channel=channel, text=text,
                          profile_id=profile_id, task_id=task_id, media=media or []))
            s.commit()
    except Exception:
        log.exception("log_message failed")
    publish("message", phone=phone, profile_id=profile_id, task_id=task_id,
            direction=direction, channel=channel, text=text, media=media or [])


def log_activity(kind: str, description: str, *, profile_id: Optional[int] = None,
                 task_id: Optional[int] = None) -> None:
    try:
        with session_scope() as s:
            s.add(Activity(kind=kind, description=description, profile_id=profile_id, task_id=task_id))
            s.commit()
    except Exception:
        log.exception("log_activity failed")
    publish("activity", kind=kind, description=description, profile_id=profile_id, task_id=task_id)


def log_event(event_type: str, *, phone: Optional[str] = None, profile_id: Optional[int] = None,
              task_id: Optional[int] = None, channel: Optional[str] = None, **data: Any) -> None:
    """Metrics event. See docs/TEAM.md for the event names metrics expects."""
    try:
        with session_scope() as s:
            s.add(Event(type=event_type, phone=phone, profile_id=profile_id, task_id=task_id,
                        channel=channel, data=data))
            s.commit()
    except Exception:
        log.exception("log_event failed")
