"""Texting drives the browser too (for people who can't talk freely, or as the demo's text-only backup).

A text from a phone with a paired browser goes to a CallController kept per phone (dropped after
15 idle minutes). Replies are sent as separate texts, because browser work can take longer than
Twilio waits for a webhook reply. Photos still go to the letter-explaining assistant.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi.concurrency import run_in_threadpool

from app.agent.call import CallController
from app.channels.outbound import send_sms

log = logging.getLogger(__name__)

IDLE_S = 15 * 60
_controllers: dict[str, tuple[CallController, float]] = {}


async def handle(phone: str, text: str) -> bool:
    """True if this text was handled by the browser agent (the webhook then replies with no message)."""
    await _drop_idle()
    entry = _controllers.get(phone)
    if entry is None:
        async def say(message: str) -> None:
            await run_in_threadpool(send_sms, phone, message, transcript=False)

        controller: Optional[CallController] = await CallController.for_caller(phone, channel="sms", say=say)
        if controller is None:
            return False
        _controllers[phone] = (controller, time.monotonic())
        await controller.start(first_text=text)
        return True
    controller, _ = entry
    _controllers[phone] = (controller, time.monotonic())
    await controller.on_utterance(text)
    return True


async def _drop_idle() -> None:
    now = time.monotonic()
    for phone, (controller, last) in list(_controllers.items()):
        if now - last > IDLE_S:
            del _controllers[phone]
            await controller.close()


async def reset() -> None:
    """Forget every texting session (tests and demo reset)."""
    for controller, _ in list(_controllers.values()):
        await controller.close()
    _controllers.clear()
