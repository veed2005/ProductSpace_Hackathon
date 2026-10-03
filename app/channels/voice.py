"""Twilio voice + ConversationRelay. Owner: Lane B.

POST /twilio/voice       (configure on the Twilio number) -> TwiML that connects ConversationRelay
WS   /twilio/voice/relay (ConversationRelay streams transcribed speech here; we reply with text)
"""

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter(prefix="/twilio")


@router.post("/voice")
async def inbound_call() -> Response:
    # TODO(Lane B, Phase 7): return <Connect><ConversationRelay url="wss://.../twilio/voice/relay" .../>
    # and implement the websocket that calls handle_turn(TurnRequest(channel="voice", ...)).
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        "<Say>Formline voice is coming soon. Please text this number instead.</Say>"
        "</Response>"
    )
    return Response(content=twiml, media_type="application/xml")
