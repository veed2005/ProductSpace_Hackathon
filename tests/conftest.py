import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch, request):
    """Every test gets its own SQLite file."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Never reach real Twilio from tests, even with real credentials in .env. Tests that check
    # signatures turn validation back on themselves (tests/test_channels_messaging.py).
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"):
        monkeypatch.setenv(var, "")
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURES", "false")
    # The original form flow and fixed-language speech recognition, unless a test turns the phone-forms
    # workflow on itself (tests/test_formcall_*.py).
    monkeypatch.setenv("FORMLINE_PHONE_FORMS", "false")
    monkeypatch.setenv("FORMLINE_VOICE_AUTODETECT", "false")
    # Never reach a real LLM either, unless the test is marked live (those use the keys in .env).
    if request.node.get_closest_marker("live") is None:
        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FORMLINE_LLM_PROVIDER"):
            monkeypatch.setenv(var, "")

    from app import config, db

    config.get_settings.cache_clear()
    db._engine = None
    db.init_db()
    yield
    db._engine = None
    config.get_settings.cache_clear()
