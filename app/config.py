from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    anthropic_api_key: str = ""
    fast_model: str = Field("claude-haiku-4-5", alias="FORMLINE_FAST_MODEL")
    strong_model: str = Field("claude-opus-5-5", alias="FORMLINE_STRONG_MODEL")
    # Model for drafting form schemas on upload; empty means strong_model. Set it to a faster
    # model if "Add a new form" is too slow for the stage demo.
    ingest_model: str = Field("", alias="FORMLINE_INGEST_MODEL")

    # OpenAI is used when it's the only key set, or when FORMLINE_LLM_PROVIDER=openai.
    openai_api_key: str = ""
    llm_provider: str = Field("", alias="FORMLINE_LLM_PROVIDER")  # "", "anthropic", or "openai"
    openai_fast_model: str = Field("gpt-4.1-mini", alias="FORMLINE_OPENAI_FAST_MODEL")
    openai_strong_model: str = Field("gpt-4.1", alias="FORMLINE_OPENAI_STRONG_MODEL")
    # Model for browser-agent decisions. Empty: gpt-5.4-mini on OpenAI (most accurate at the same latency in
    # scripts/agent_bench.py), the fast model on Anthropic.
    agent_model: str = Field("", alias="FORMLINE_AGENT_MODEL")
    # Reasoning effort for OpenAI reasoning models (gpt-5.x, o-series): none | minimal | low | medium.
    openai_reasoning: str = Field("low", alias="FORMLINE_OPENAI_REASONING")
    # Send a screenshot to the model when a page's text snapshot is nearly empty (canvas apps, image-only
    # pages). Off by default: a screenshot skips the in-browser redaction of secrets.
    vision_fallback: bool = Field(False, alias="FORMLINE_VISION_FALLBACK")

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    twilio_validate_signatures: bool = True
    # Record every call (both sides) for demo backup footage; the greeting announces it.
    record_calls: bool = Field(False, alias="FORMLINE_RECORD_CALLS")

    public_base_url: str = "http://localhost:8000"
    database_url: str = f"sqlite:///{ROOT_DIR / 'data' / 'formline.db'}"
    data_dir: Path = ROOT_DIR / "data"
    forms_dir: Path = ROOT_DIR / "forms"
    log_level: str = "INFO"
    # Enables POST /dev/turn so scripts/simulate.py --server can drive the running app.
    # Never enable on a publicly reachable server: it skips Twilio auth entirely.
    dev_endpoints: bool = Field(False, alias="FORMLINE_DEV_ENDPOINTS")
    # The dashboard and its API answer only requests made on this machine (not via ngrok).
    # Set true only on a trusted network: it exposes personal data and the demo reset button.
    dashboard_remote: bool = Field(False, alias="FORMLINE_DASHBOARD_REMOTE")


@lru_cache
def get_settings() -> Settings:
    return Settings()
