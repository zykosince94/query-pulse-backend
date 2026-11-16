"""Entry point: `python -m query_pulse.collector`."""

from __future__ import annotations

import asyncio
import contextlib
import sys

from query_pulse.collector.cycle import Collector
from query_pulse.collector.supervisor import Supervisor
from query_pulse.config import get_settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import create_engine, create_sessionmaker
from query_pulse.log import configure_logging


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    collector = Collector(settings, sessionmaker, CredentialCipher(settings.fernet_keys))
    try:
        await Supervisor(settings, sessionmaker, collector).run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    settings = get_settings()
    configure_logging(settings.log_format, settings.log_level)
    if sys.platform == "win32":  # psycopg's async mode needs the selector loop on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
