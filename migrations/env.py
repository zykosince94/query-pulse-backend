"""Alembic environment (async engine).

URL precedence: ``sqlalchemy.url`` set programmatically (tests) > ``QP_DATABASE_URL`` > settings.
Only the database URL is needed, so migrations don't require the app's other secrets.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection

from query_pulse.accounts import models as _accounts_models  # noqa: F401  (registers tables)
from query_pulse.db import Base, create_engine

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("QP_DATABASE_URL")
    if url:
        return url
    from query_pulse.config import get_settings

    return get_settings().database_url


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
