from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.channels import twilio_setup


def test_webhook_urls_strip_trailing_slash():
    assert twilio_setup.webhook_urls("https://x.ngrok-free.app/") == (
        "https://x.ngrok-free.app/twilio/messaging", "https://x.ngrok-free.app/twilio/voice")


def test_configure_updates_both_webhooks():
    client = MagicMock()
    client.incoming_phone_numbers.list.return_value = [SimpleNamespace(sid="PN123")]
    twilio_setup.configure(client, "+15550000000", "https://x.ngrok-free.app")
    client.incoming_phone_numbers.assert_called_with("PN123")
    client.incoming_phone_numbers.return_value.update.assert_called_once_with(
        sms_url="https://x.ngrok-free.app/twilio/messaging", sms_method="POST",
        voice_url="https://x.ngrok-free.app/twilio/voice", voice_method="POST")


def test_configure_unknown_number_exits():
    client = MagicMock()
    client.incoming_phone_numbers.list.return_value = []
    with pytest.raises(SystemExit):
        twilio_setup.configure(client, "+15550000000", "https://x.ngrok-free.app")


def test_main_without_credentials_does_nothing(monkeypatch, capsys):
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"):
        monkeypatch.setenv(var, "")
    from app import config

    config.get_settings.cache_clear()
    assert twilio_setup.main([]) == 1
    assert "TWILIO_ACCOUNT_SID" in capsys.readouterr().out


def test_ngrok_url_none_when_agent_not_running(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(twilio_setup.httpx, "get", boom)
    assert twilio_setup.ngrok_url() is None
