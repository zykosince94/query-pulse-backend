"""Runtime settings, validated from the environment at startup.

Every value comes from a ``QP_``-prefixed environment variable (or a local ``.env`` file).
The app refuses to start when a required value is missing or invalid, so misconfiguration
fails loudly instead of surfacing later as a confusing runtime error.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["dev", "test", "prod"] = "dev"

    # The app's own database (users now; sources, observations and findings later).
    database_url: str = "postgresql+psycopg://querypulse:querypulse@localhost:5432/querypulse"

    # Signs the session cookie. Rotating it signs everyone out.
    session_secret: SecretStr = Field(min_length=32)
    session_https_only: bool = False
    session_max_age_seconds: int = Field(default=60 * 60 * 12, gt=0)

    # Encrypts stored source credentials (used from Phase 2). Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    fernet_key: SecretStr

    # Users created by `python -m query_pulse.accounts.bootstrap`.
    admin_username: str = Field(default="admin", min_length=1)
    admin_password: SecretStr = Field(min_length=8)
    demo_enabled: bool = False
    demo_username: str = Field(default="demo", min_length=1)
    demo_password: SecretStr | None = None

    @field_validator("fernet_key")
    @classmethod
    def _valid_fernet_key(cls, value: SecretStr) -> SecretStr:
        try:
            Fernet(value.get_secret_value().encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("QP_FERNET_KEY must be a url-safe base64 32-byte Fernet key") from exc
        return value

    @model_validator(mode="after")
    def _demo_user_needs_password(self) -> Settings:
        if self.demo_enabled and not self.demo_password:
            raise ValueError("QP_DEMO_PASSWORD is required when QP_DEMO_ENABLED is true")
        if self.demo_enabled and self.demo_username == self.admin_username:
            raise ValueError("QP_DEMO_USERNAME must differ from QP_ADMIN_USERNAME")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment
