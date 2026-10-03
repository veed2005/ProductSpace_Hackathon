"""Per-channel reply style rules passed to the LLM. Owner: Lane A."""

from app.contracts import Channel

STYLE: dict[str, str] = {
    "voice": (
        "You are speaking on a phone call. Use short spoken sentences. No lists, symbols, or "
        "markdown. Say numbers and dates naturally. Ask one question per turn. Be warm and patient."
    ),
    "sms": (
        "You are texting. One question per message, under 300 characters, no links. Offer numbered "
        "choices when helpful (Reply 1 for Yes, 2 for No) but always accept free-text answers."
    ),
}


def style_for(channel: Channel) -> str:
    return STYLE[channel]
