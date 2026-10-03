"""Thin wrapper over the Anthropic SDK. Shared by every lane.

Call these through the module (`from app.llm import client as llm; llm.structured(...)`)
so tests can monkeypatch `llm.structured` / `llm.text` without an API key.
"""

import base64
import mimetypes
from functools import lru_cache
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from app.config import get_settings

T = TypeVar("T", bound=BaseModel)

# Models that accept the server-side refusal fallback (routes a declined request to
# another model inside the same call instead of failing the turn).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


@lru_cache
def get_client() -> anthropic.Anthropic:
    key = get_settings().anthropic_api_key
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def fast_model() -> str:
    return get_settings().fast_model


def strong_model() -> str:
    return get_settings().strong_model


def image_block(path: str | Path) -> dict:
    """Content block for a local image (e.g. a downloaded MMS photo)."""
    media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.standard_b64encode(Path(path).read_bytes()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def structured(output: type[T], *, system: str, messages: list[dict], model: str | None = None,
               max_tokens: int = 4000) -> T:
    """Get a validated Pydantic object back. Raises anthropic.APIError on API failure."""
    model = model or fast_model()
    kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=messages,
                  output_format=output)
    if model in _FALLBACK_MODELS:
        resp = get_client().beta.messages.parse(betas=[_FALLBACK_BETA], fallbacks="default", **kwargs)
    else:
        resp = get_client().messages.parse(**kwargs)
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise ValueError(f"model returned no structured output (stop_reason={resp.stop_reason})")
    return resp.parsed_output


def text(*, system: str, messages: list[dict], model: str | None = None, max_tokens: int = 1000) -> str:
    """Plain text completion (e.g. a reply to read aloud)."""
    resp = get_client().messages.create(model=model or fast_model(), max_tokens=max_tokens,
                                        system=system, messages=messages)
    return "".join(b.text for b in resp.content if b.type == "text").strip()
