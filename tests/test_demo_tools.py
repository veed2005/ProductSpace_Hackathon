import sys
from datetime import date
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import demo_preflight  # noqa: E402
import make_demo_letter  # noqa: E402


def test_demo_letter_matches_the_persona_and_is_marked_sample(tmp_path):
    pdf, png = make_demo_letter.build(date(2026, 11, 1), tmp_path)
    assert png.exists() and png.stat().st_size > 10_000
    text = pymupdf.open(pdf)[0].get_text()
    assert "ROSA MARTINEZ" in text and "77 MAPLE AVE APT 3" in text
    assert "November 1, 2026" in text
    assert "NOT A REAL GOVERNMENT NOTICE" in text


def test_preflight_env_checks_flag_unsafe_demo_settings(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("FORMLINE_DEV_ENDPOINTS", "true")
    monkeypatch.setenv("FORMLINE_DASHBOARD_REMOTE", "true")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.ngrok-free.app")
    get_settings.cache_clear()
    checks = {c.name: c for c in demo_preflight.check_env()}
    get_settings.cache_clear()
    assert not checks["Dev endpoint is off"].ok and checks["Dev endpoint is off"].blocking
    assert not checks["Dashboard is local-only"].ok
    assert not checks["PUBLIC_BASE_URL is an https ngrok URL"].ok


def test_preflight_reports_missing_demo_forms():
    checks = {c.name: c for c in demo_preflight.check_forms()}
    # The repo only has the placeholder form until the real ones are added.
    assert "SNAP application form in the library" in checks
    assert all(c.fix for c in checks.values() if not c.ok)


def test_preflight_server_check_handles_no_server(monkeypatch):
    monkeypatch.setattr(demo_preflight, "LOCAL", "http://127.0.0.1:9")  # nothing listens on port 9
    [check] = demo_preflight.check_server()
    assert not check.ok and "uvicorn" in check.fix
