"""Sending email receipts through a provider, so the workflow never knows how mail is delivered.

    outbox  (default) writes a .eml file to data/outbox/. Nothing leaves the laptop; delivered=False, so Formline
            says the receipt was saved, not emailed.
    smtp    sends through an SMTP server (SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, FORMLINE_EMAIL_FROM).

Formline says "I emailed it" only when a provider returns delivered=True.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Optional, Protocol

from app.config import get_settings

log = logging.getLogger(__name__)


@dataclass
class Email:
    to: str
    subject: str
    text: str
    html: str
    attachments: list[tuple[str, bytes, str]] = field(default_factory=list)  # (filename, data, mime type)


@dataclass
class SendResult:
    ok: bool  # the provider accepted it
    delivered: bool  # it actually went out to the recipient's mail server (False for the outbox)
    ref: Optional[str] = None  # message id or file path
    error: Optional[str] = None


class EmailProvider(Protocol):
    name: str

    def send(self, email: Email) -> SendResult: ...


def build_message(email: Email) -> EmailMessage:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = s.email_from
    msg["To"] = email.to
    msg["Subject"] = email.subject
    msg["Message-ID"] = make_msgid(domain="formline.local")
    msg.set_content(email.text)
    msg.add_alternative(email.html, subtype="html")
    for filename, data, mime in email.attachments:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


class OutboxProvider:
    name = "outbox"

    def send(self, email: Email) -> SendResult:
        outbox = get_settings().data_dir / "outbox"
        try:
            outbox.mkdir(parents=True, exist_ok=True)
            path = outbox / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}.eml"
            path.write_bytes(bytes(build_message(email)))
            return SendResult(ok=True, delivered=False, ref=str(path))
        except OSError as e:
            return SendResult(ok=False, delivered=False, error=str(e))


class SmtpProvider:
    name = "smtp"

    def send(self, email: Email) -> SendResult:
        s = get_settings()
        if not s.smtp_host:
            return SendResult(ok=False, delivered=False, error="SMTP_HOST is not set")
        msg = build_message(email)
        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
                if s.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if s.smtp_username:
                    smtp.login(s.smtp_username, s.smtp_password)
                refused = smtp.send_message(msg)
            if refused:
                return SendResult(ok=False, delivered=False, error=f"refused: {', '.join(refused)}")
            return SendResult(ok=True, delivered=True, ref=msg["Message-ID"])
        except (smtplib.SMTPException, OSError) as e:
            log.warning("receipt email failed: %s", e)
            return SendResult(ok=False, delivered=False, error=str(e)[:300])


_override: Optional[EmailProvider] = None


def get_provider() -> EmailProvider:
    if _override is not None:
        return _override
    return SmtpProvider() if get_settings().email_provider.lower() == "smtp" else OutboxProvider()


def set_provider(provider: Optional[EmailProvider]) -> None:
    """Tests and demos swap in a fake provider."""
    global _override
    _override = provider
