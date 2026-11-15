from __future__ import annotations

from query_pulse.adapters.base import Adapter
from query_pulse.adapters.mongodb import MongoAdapter
from query_pulse.adapters.postgres import PostgresAdapter

_ADAPTERS: dict[str, Adapter] = {
    PostgresAdapter.engine: PostgresAdapter(),
    MongoAdapter.engine: MongoAdapter(),
}


def adapter_for(engine: str) -> Adapter:
    try:
        return _ADAPTERS[engine]
    except KeyError:
        raise ValueError(f"No adapter for engine {engine!r}") from None
