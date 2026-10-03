"""Send texts to people. Owner: Lane B.

Used by everyone who needs to reach a person outside a reply: receipts, reminders,
"reply with a photo" during a call. Without Twilio credentials it only logs, so local
development and tests work offline.
"""

import logging
from typing import Optional

from app.config import get_settings
from app.events import log_message

log = logging.getLogger(__name__)


def send_sms(to: str, body: str, *, profile_id: Optional[int] = None, task_id: Optional[int] = None) -> Optional[str]:
    """Send an SMS. Returns the Twilio message SID, or None in offline mode."""
    settings = get_settings()
    log_message(to, "out", "sms", body, profile_id=profile_id, task_id=task_id)
    if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_phone_number):
        log.info("[offline sms to %s] %s", to, body)
        return None
    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    msg = client.messages.create(to=to, from_=settings.twilio_phone_number, body=body)
    return msg.sid
