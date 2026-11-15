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

    # Encrypts stored source credentials. Comma-separate several keys to rotate: the first
    # encrypts, all decrypt. Generate one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    fernet_key: SecretStr

    # Monitored-database connections (test connection, capability checks).
    source_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    # Users created by `python -m query_pulse.accounts.bootstrap`.
    admin_username: str = Field(default="admin", min_length=1)
    admin_password: SecretStr = Field(min_length=8)
    demo_enabled: bool = False
    demo_username: str = Field(default="demo", min_length=1)
    demo_password: SecretStr | None = None

    @property
    def fernet_keys(self) -> list[str]:
        return [k.strip() for k in self.fernet_key.get_secret_value().split(",") if k.strip()]

    @field_validator("fernet_key")
    @classmethod
    def _valid_fernet_key(cls, value: SecretStr) -> SecretStr:
        keys = [k.strip() for k in value.get_secret_value().split(",") if k.strip()]
        try:
            if not keys:
                raise ValueError
            for key in keys:
                Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "QP_FERNET_KEY must be one or more comma-separated url-safe base64 32-byte "
                "Fernet keys"
            ) from exc
        return value

    @model_validator(mode="after")
    def _demo_user_needs_password(self) -> Settings:
        if self.demo_enabled and not self.demo_password:
            raise ValueError("QP_DEMO_PASSWORD is required when QP_DEMO_ENABLED is true")
        if self.demo_enabled and self.demo_username == self.admin_username:
            raise ValueError("QP_DEMO_USERNAME must differ from QP_ADMIN_USERNAME")
        return self

    @model_validator(mode="after")
    def _prod_needs_secure_cookies(self) -> Settings:
        # Production runs behind HTTPS; without the Secure flag the session cookie could be
        # sent over plain HTTP and read by anyone on the network path.
        if self.environment == "prod" and not self.session_https_only:
            raise ValueError("QP_SESSION_HTTPS_ONLY must be true when QP_ENVIRONMENT is prod")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment
