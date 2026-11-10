from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, SecretStr


class SourceType(StrEnum):
    POSTGRES = "postgres"
    MONGODB = "mongodb"


class SourceBase(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=2, max_length=120)
    type: SourceType
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=120)
    username: str = Field(min_length=1, max_length=120)
    ssl: bool = False
    enabled: bool = True
    description: str = ""


class SourceCreate(SourceBase):
    password: SecretStr = SecretStr("")


class SourceOut(SourceBase):
    """What the API returns. Credentials are never included, only whether one is set."""

    has_password: bool


class SourceConnectionRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    type: SourceType
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=120)
    username: str = Field(min_length=1, max_length=120)
    password: SecretStr = SecretStr("")
    ssl: bool = False


class SourceHealth(BaseModel):
    source_id: str
    status: str
    engine: SourceType
    ready: bool
    message: str
