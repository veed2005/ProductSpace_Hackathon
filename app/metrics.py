"""Section 12 metrics, computed from the events table.

Event names and payloads are the contract in docs/TEAM.md ("Metrics events"). Every metric
degrades to None when its events haven't been logged yet, so the panel works from day one.
"""

from collections import Counter, defaultdict
from datetime import timezone
from statistics import mean
from typing import Any, Optional

from sqlmodel import select

from app.db import session_scope
from app.models import Event, Message, Profile


def _pct(part: float, whole: float) -> Optional[float]:
    return round(100 * part / whole, 1) if whole else None


def _p90(values: list[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]


def _num(d: dict, key: str) -> Optional[float]:
    v = d.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _where(form_id: Optional[str], field_id: Optional[str]) -> str:
    from app.engines import form_library

    form_name, label = form_id or "A form", field_id or "the start"
    try:
        schema = form_library.load_schema(form_id)
        form_name = schema.name
        label = next((f.label for f in schema.fields if f.id == field_id), label)
    except Exception:  # unknown or deleted form: fall back to ids
        pass
    return f"{form_name}: {label}"


def compute_metrics() -> dict[str, Any]:
    with session_scope() as s:
        events = s.exec(select(Event).order_by(Event.created_at, Event.id)).all()
        inbound = s.exec(select(Message.channel).where(Message.direction == "in")).all()
        names = {p.id: p.display_name for p in s.exec(select(Profile)).all()}
    by_type: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_type[e.type].append(e)

    # ---- forms: first vs. later for the same person (headline memory metric)
    completed = by_type["form_completed"]
    per_person: dict[int, list[Event]] = defaultdict(list)
    for e in completed:
        if e.profile_id is not None and _num(e.data, "duration_s") is not None:
            per_person[e.profile_id].append(e)
    first, later, pairs = [], [], []
    for pid, evs in per_person.items():
        first.append(_num(evs[0].data, "duration_s"))
        for e in evs[1:]:
            later.append(_num(e.data, "duration_s"))
            pairs.append({
                "profile_id": pid, "name": names.get(pid) or "Someone",
                "first_form": evs[0].data.get("form_id"), "first_s": _num(evs[0].data, "duration_s"),
                "later_form": e.data.get("form_id"), "later_s": _num(e.data, "duration_s"),
                "later_from_memory": e.data.get("fields_from_memory"), "later_fields": e.data.get("fields_total"),
                "at": e.created_at.replace(tzinfo=timezone.utc).isoformat() if e.created_at.tzinfo is None
                else e.created_at.isoformat(),
            })
    avg_first = round(mean(first)) if first else None
    avg_later = round(mean(later)) if later else None
    faster = round(100 * (1 - avg_later / avg_first)) if avg_first and avg_later is not None else None
    latest_pair = max(pairs, key=lambda p: p["at"]) if pairs else None

    fields_total = sum(_num(e.data, "fields_total") or 0 for e in completed)
    fields_memory = sum(_num(e.data, "fields_from_memory") or 0 for e in completed)
    turns = [t for t in (_num(e.data, "turns") for e in completed) if t is not None]
    corrections = len(by_type["readback_correction"])

    # ---- drop-off: where unfinished forms stopped, in plain words
    dropoff = Counter(_where(e.data.get("form_id"), e.data.get("last_field")) for e in by_type["form_abandoned"])

    # ---- documents
    docs = len(by_type["document_explained"])
    docs_to_forms = sum(1 for e in by_type["form_started"] if e.data.get("from_document_id"))

    # ---- voice latency (B logs voice_latency; fall back to A's per-turn timing on voice)
    latency = [v for v in (_num(e.data, "ms") for e in by_type["voice_latency"]) if v is not None]
    if not latency:
        latency = [v for v in (_num(e.data, "latency_ms") for e in by_type["turn"] if e.channel == "voice")
                   if v is not None]

    # ---- channels: what people actually sent (always logged), plus switches
    channels = Counter(inbound)

    # ---- verification
    verifications = by_type["verification"]
    passed = sum(1 for e in verifications if e.data.get("ok"))

    return {
        "forms": {
            "completed": len(completed),
            "started": len(by_type["form_started"]),
            "abandoned": len(by_type["form_abandoned"]),
            "avg_first_s": avg_first,
            "avg_later_s": avg_later,
            "faster_pct": faster,
            "first_count": len(first),
            "later_count": len(later),
            "latest_pair": latest_pair,
            "from_memory_pct": _pct(fields_memory, fields_total),
            "avg_turns": round(mean(turns), 1) if turns else None,
            "correction_rate_pct": _pct(corrections, fields_total),
            "corrections": corrections,
            "dropoff": [{"where": k, "count": v} for k, v in dropoff.most_common(5)],
        },
        "documents": {"explained": docs, "led_to_form": docs_to_forms, "led_to_form_pct": _pct(docs_to_forms, docs)},
        "voice": {"turns_measured": len(latency), "avg_ms": round(mean(latency)) if latency else None,
                  "p90_ms": round(_p90(latency)) if latency else None},
        "channels": {"sms": channels.get("sms", 0), "voice": channels.get("voice", 0),
                     "switches": len(by_type["channel_switch"])},
        "verification": {"checked": len(verifications), "passed": passed,
                         "pass_rate_pct": _pct(passed, len(verifications))},
        "people": len(names),
    }
