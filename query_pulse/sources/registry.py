"""In-memory source registry (placeholder until Phase 2 stores sources in the database)."""

from __future__ import annotations

from pydantic import SecretStr

from query_pulse.sources.schemas import SourceCreate, SourceOut, SourceType


def _demo_sources() -> list[SourceCreate]:
    return [
        SourceCreate(
            id="demo-postgres",
            name="Demo PostgreSQL",
            type=SourceType.POSTGRES,
            host="demo-postgres",
            port=5432,
            database="shop",
            username="querypulse_monitor",
            password=SecretStr("monitor"),
            description="Demo PostgreSQL 16 with pg_stat_statements (compose service).",
        ),
        SourceCreate(
            id="demo-mongodb",
            name="Demo MongoDB",
            type=SourceType.MONGODB,
            host="mongo",
            port=27017,
            database="shop",
            username="monitor",
            description="Demo MongoDB single-node replica set (compose service).",
        ),
    ]


class SourceRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, SourceCreate] = {s.id: s for s in _demo_sources()}

    def list(self) -> list[SourceOut]:
        return [self._public(s) for s in self._sources.values()]

    def get(self, source_id: str) -> SourceOut | None:
        source = self._sources.get(source_id)
        return self._public(source) if source else None

    def add(self, source: SourceCreate) -> SourceOut:
        if source.id in self._sources:
            raise KeyError(source.id)
        self._sources[source.id] = source
        return self._public(source)

    @staticmethod
    def _public(source: SourceCreate) -> SourceOut:
        return SourceOut(
            **source.model_dump(exclude={"password"}),
            has_password=bool(source.password.get_secret_value()),
        )
