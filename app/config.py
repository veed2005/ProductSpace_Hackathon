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

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    twilio_validate_signatures: bool = True

    public_base_url: str = "http://localhost:8000"
    database_url: str = f"sqlite:///{ROOT_DIR / 'data' / 'formline.db'}"
    data_dir: Path = ROOT_DIR / "data"
    forms_dir: Path = ROOT_DIR / "forms"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
