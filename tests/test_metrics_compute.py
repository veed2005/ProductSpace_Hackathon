from app.core import identity
from app.dashboard.demo import seed_demo
from app.events import log_event, log_message
from app.metrics import compute_metrics


def test_empty_database_degrades_gracefully():
    m = compute_metrics()
    assert m["forms"]["completed"] == 0 and m["forms"]["faster_pct"] is None
    assert m["voice"]["avg_ms"] is None and m["verification"]["pass_rate_pct"] is None


def test_first_vs_later_form_headline():
    a = identity.create_profile("+12025550180", display_name="Ana").id
    b = identity.create_profile("+12025550181", display_name="Ben").id
    log_event("form_completed", profile_id=a, duration_s=600, turns=20, fields_total=10, fields_from_memory=0)
    log_event("form_completed", profile_id=b, duration_s=400, turns=18, fields_total=10, fields_from_memory=0)
    log_event("form_completed", profile_id=a, duration_s=150, turns=8, fields_total=10, fields_from_memory=7)
    log_event("form_completed", profile_id=None, duration_s=999, fields_total=10)  # forgotten person
    f = compute_metrics()["forms"]
    assert f["avg_first_s"] == 500 and f["avg_later_s"] == 150 and f["faster_pct"] == 70
    assert f["latest_pair"]["name"] == "Ana" and f["latest_pair"]["later_from_memory"] == 7
    assert f["completed"] == 4 and f["from_memory_pct"] == 17.5


def test_other_metrics():
    pid = identity.create_profile("+12025550182").id
    log_event("form_completed", profile_id=pid, duration_s=300, turns=10, fields_total=8, fields_from_memory=2)
    log_event("readback_correction", profile_id=pid, field_id="employer")
    log_event("form_abandoned", form_id="sample_benefits", last_field="monthly_income")
    log_event("document_explained", profile_id=pid)
    log_event("document_explained", profile_id=pid)
    log_event("form_started", profile_id=pid, from_document_id=1)
    for ms in (100, 200, 300, 400, 500, 600, 700, 800, 900, 1000):
        log_event("voice_latency", channel="voice", ms=ms)
    log_event("verification", ok=True)
    log_event("verification", ok=False)
    log_event("channel_switch", **{"from": "voice", "to": "sms"})
    log_message("+12025550182", "in", "sms", "hi")
    log_message("+12025550182", "in", "voice", "hola")
    log_message("+12025550182", "out", "sms", "hello")  # outbound doesn't count as usage
    m = compute_metrics()
    assert m["forms"]["correction_rate_pct"] == 12.5
    assert m["forms"]["dropoff"] == [{"where": "Sample Benefits Application: Monthly income", "count": 1}]
    assert m["documents"] == {"explained": 2, "led_to_form": 1, "led_to_form_pct": 50.0}
    assert m["voice"]["avg_ms"] == 550 and m["voice"]["p90_ms"] == 900
    assert m["verification"]["pass_rate_pct"] == 50.0
    assert m["channels"] == {"sms": 1, "voice": 1, "switches": 1}


def test_seeded_demo_has_a_first_form_baseline():
    seed_demo()
    f = compute_metrics()["forms"]
    assert f["first_count"] == 1 and f["avg_first_s"] == 660 and f["avg_later_s"] is None
