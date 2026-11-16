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

    # The collector (`python -m query_pulse.collector`). Tests use a 2-second interval.
    collector_interval_seconds: float = Field(default=60.0, gt=0)
    collector_cycle_timeout_seconds: float = Field(default=30.0, gt=0)
    collector_max_backoff_seconds: float = Field(default=300.0, gt=0)
    collector_relation_stats_seconds: float = Field(default=300.0, gt=0)
    collector_capability_check_seconds: float = Field(default=900.0, gt=0)
    collector_refresh_seconds: float = Field(default=30.0, gt=0)  # pick up added/removed sources
    # Generic plans: every this often, re-plan each source's busiest operations (operations
    # without a plan yet are planned on the next cycle). Planning shares the cycle timeout,
    # so it gets at most a third of it.
    collector_plan_seconds: float = Field(default=300.0, gt=0)
    collector_plan_top_n: int = Field(default=20, ge=1, le=200)

    # Findings: how often each source's rules run (after a collection cycle), and over what.
    rules_interval_seconds: float = Field(default=60.0, gt=0)
    rules_window_minutes: int = Field(default=60, ge=5, le=1440)
    # An index counts as unused only if every sample over this span shows zero scans.
    rules_unused_index_min_span_seconds: float = Field(default=3600.0, gt=0)
    rules_unused_index_window_hours: int = Field(default=24, ge=1, le=24 * 31)
    # An open finding whose problem hasn't been seen for this long is resolved automatically.
    finding_resolve_after_seconds: float = Field(default=3600.0, gt=0)
    retention_days: int = Field(default=14, ge=1)

    # Logging: one JSON object per line (or readable text while developing).
    log_format: Literal["json", "text"] = "json"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Cross-site requests: state-changing requests whose Origin isn't this site are refused.
    # Comma-separated extra origins to accept, e.g. "https://qp.example.com".
    allowed_origins: str = ""

    # Sign-in: after this many failures from one client within the window, refuse for a while.
    login_max_failures: int = Field(default=10, ge=1)
    login_window_seconds: float = Field(default=300.0, gt=0)

    # Users created by `python -m query_pulse.accounts.bootstrap`.
    admin_username: str = Field(default="admin", min_length=1)
    admin_password: SecretStr = Field(min_length=8)
    demo_enabled: bool = False
    demo_username: str = Field(default="demo", min_length=1)
    demo_password: SecretStr | None = None

    @property
    def allowed_origin_set(self) -> set[str]:
        return {o.strip() for o in self.allowed_origins.split(",") if o.strip()}

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
