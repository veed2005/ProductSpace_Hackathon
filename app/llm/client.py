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


@lru_cache
def get_openai_client():
    from openai import OpenAI

    return OpenAI(api_key=get_settings().openai_api_key)


def provider() -> str:
    """'anthropic' or 'openai': an explicit FORMLINE_LLM_PROVIDER wins, otherwise whichever key is set
    (Anthropic first, so teammates with Anthropic keys see no change)."""
    s = get_settings()
    if s.llm_provider in ("anthropic", "openai"):
        return s.llm_provider
    if not s.anthropic_api_key and s.openai_api_key:
        return "openai"
    return "anthropic"


def available() -> bool:
    """True if the active provider has an API key, so callers can skip LLM fallbacks offline."""
    s = get_settings()
    return bool(s.openai_api_key if provider() == "openai" else s.anthropic_api_key)


def fast_model() -> str:
    s = get_settings()
    return s.openai_fast_model if provider() == "openai" else s.fast_model


def strong_model() -> str:
    s = get_settings()
    return s.openai_strong_model if provider() == "openai" else s.strong_model


def agent_model() -> str:
    return get_settings().agent_model or fast_model()


def _openai_model(model: str | None) -> str:
    """Callers may pass a configured Claude model name; map it to the OpenAI equivalent."""
    s = get_settings()
    if not model:
        return s.openai_fast_model
    if model.startswith("claude"):
        return s.openai_strong_model if model in (s.strong_model, s.ingest_model) else s.openai_fast_model
    return model


def _openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Anthropic-style messages (text and base64 image blocks) -> OpenAI chat messages."""
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            parts = []
            for block in content:
                if block.get("type") == "image":
                    src = block["source"]
                    parts.append({"type": "image_url",
                                  "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
                elif block.get("type") == "text":
                    parts.append({"type": "text", "text": block["text"]})
            content = parts
        out.append({"role": m["role"], "content": content})
    return out


def _openai_extra(model: str) -> dict:
    # Reasoning models spend time thinking before answering; keep it minimal for phone latency.
    if model.startswith(("gpt-5", "o3", "o4")):
        return {"reasoning_effort": "low"}
    return {}


def image_block(path: str | Path) -> dict:
    """Content block for a local image (e.g. a downloaded MMS photo)."""
    media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.standard_b64encode(Path(path).read_bytes()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def structured(output: type[T], *, system: str, messages: list[dict], model: str | None = None,
               max_tokens: int = 4000) -> T:
    """Get a validated Pydantic object back. Raises the provider's API error on failure."""
    if provider() == "openai":
        model = _openai_model(model)
        resp = get_openai_client().chat.completions.parse(
            model=model, messages=_openai_messages(system, messages), response_format=output,
            max_completion_tokens=max_tokens, **_openai_extra(model))
        msg = resp.choices[0].message
        if msg.refusal or msg.parsed is None:
            raise ValueError(f"model returned no structured output (refusal={msg.refusal!r})")
        return msg.parsed
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
    if provider() == "openai":
        model = _openai_model(model)
        resp = get_openai_client().chat.completions.create(
            model=model, messages=_openai_messages(system, messages), max_completion_tokens=max_tokens,
            **_openai_extra(model))
        return (resp.choices[0].message.content or "").strip()
    resp = get_client().messages.create(model=model or fast_model(), max_tokens=max_tokens,
                                        system=system, messages=messages)
    return "".join(b.text for b in resp.content if b.type == "text").strip()
