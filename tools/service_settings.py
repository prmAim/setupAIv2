"""Validated configuration for local service checks; no implicit cloud endpoint."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="aim_setupaiv2", min_length=1)
    app_env: Literal["development", "staging", "production"] = "development"
    llm_provider: Literal["openai", "deepseek"] = "openai"
    llm_model: str = Field(default="gpt-6-luna", min_length=1)
    openai_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    langfuse_base_url: Literal["http://localhost:3000", "http://127.0.0.1:3000"] = "http://localhost:3000"
    langfuse_public_key: SecretStr
    langfuse_secret_key: SecretStr

    @field_validator("langfuse_public_key", "langfuse_secret_key")
    @classmethod
    def require_observability_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Run tools/init_local_observability.py first")
        return value

    def model_credentials(self) -> tuple[str, SecretStr]:
        if self.llm_provider == "openai":
            endpoint, key = "https://api.openai.com/v1", self.openai_api_key
        else:
            endpoint, key = "https://api.deepseek.com", self.deepseek_api_key
            if self.llm_model.startswith("gpt-"):
                raise ValueError("Set LLM_MODEL to a DeepSeek model before switching providers")
        if not key.get_secret_value().strip():
            raise ValueError(f"Set {self.llm_provider.upper()}_API_KEY locally in .env")
        return endpoint, key
