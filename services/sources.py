from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    POSTGRES = "postgres"
    MONGODB = "mongodb"


class SourceDefinition(BaseModel):
    id: str
    name: str
    type: SourceType
    host: str
    port: int
    database: str
    username: str
    password: str = ""
    ssl: bool = False
    enabled: bool = True
    description: str = ""


class SourceHealth(BaseModel):
    source_id: str
    status: str = "healthy"
    engine: str
    ready: bool = True
    message: str = "Source is reachable and ready for collection"


class SourceConnectionRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    type: SourceType
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    database: str = Field(..., min_length=1, max_length=120)
    username: str = Field(..., min_length=1, max_length=120)
    password: str = ""
    ssl: bool = False


def demo_sources() -> list[SourceDefinition]:
    return [
        SourceDefinition(
            id="demo-postgres",
            name="Demo PostgreSQL",
            type=SourceType.POSTGRES,
            host="localhost",
            port=5432,
            database="querypulse",
            username="postgres",
            password="postgres",
            ssl=False,
            description="Local PostgreSQL source used for demo workload collection.",
            enabled=True,
        ),
        SourceDefinition(
            id="demo-mongodb",
            name="Demo MongoDB",
            type=SourceType.MONGODB,
            host="localhost",
            port=27017,
            database="querypulse",
            username="readOnlyUser",
            password="readOnlyPass",
            ssl=False,
            description="Local MongoDB source used for workload collection and shape inspection.",
            enabled=True,
        ),
    ]


SOURCE_REGISTRY = {source.id: source for source in demo_sources()}


def validate_connection_request(payload: SourceConnectionRequest) -> None:
    if not payload.host or not payload.database or not payload.username:
        raise ValueError("Host, database and username are required")

    if payload.type == SourceType.POSTGRES and payload.port == 5432:
        return
    if payload.type == SourceType.MONGODB and payload.port == 27017:
        return

    if payload.host.startswith("localhost") or payload.host.startswith("127.0.0.1"):
        return

    if payload.host and payload.database and payload.username:
        return

    raise ValueError("Unsupported source connection parameters")
