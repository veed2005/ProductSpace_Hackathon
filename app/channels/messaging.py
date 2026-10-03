"""Twilio SMS/MMS webhook. Owner: Lane B.

POST /twilio/messaging  (configure on the Twilio number)
"""

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse

from app.channels.outbound import send_sms
from app.contracts import TurnRequest
from app.core.turn import handle_turn

router = APIRouter(prefix="/twilio")


@router.post("/messaging")
async def inbound_message(request: Request) -> Response:
    form = await request.form()
    # TODO(Lane B): validate X-Twilio-Signature unless settings.twilio_validate_signatures is false.
    # TODO(Lane B): download MediaUrl0..N (authenticated) into data/media/ and pass the paths.
    phone = str(form.get("From", ""))
    text = str(form.get("Body", ""))

    result = await run_in_threadpool(handle_turn, TurnRequest(phone=phone, channel="sms", text=text))

    twiml = MessagingResponse()
    twiml.message(result.reply)
    for extra in result.followup_sms:
        await run_in_threadpool(send_sms, phone, extra)
    return Response(content=str(twiml), media_type="application/xml")
