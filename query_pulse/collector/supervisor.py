"""The long-running collector: one asyncio task per enabled PostgreSQL source.

    supervisor loop (every collector_refresh_seconds)
      ├─ start a task for each newly enabled source, cancel tasks for removed/disabled ones
      ├─ write the heartbeat (the API's /api/collector/status reads it)
      └─ once a day: delete data older than the retention period

    per-source task
      ├─ wait a random part of one interval first (jitter: sources don't all poll at once)
      └─ loop: collect → wait (interval when healthy, exponential backoff after failures)

One collector instance only, by design (the blueprint's single-instance rule).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.collector.backoff import jittered, next_delay
from query_pulse.collector.cycle import Collector
from query_pulse.collector.models import CollectorHeartbeat
from query_pulse.config import Settings
from query_pulse.db import utcnow
from query_pulse.sources.models import DataSource, SourceEngine

logger = logging.getLogger(__name__)

# Touched every supervisor loop; the container healthcheck checks how recent it is.
HEARTBEAT_FILE = Path("/tmp/query-pulse-collector-heartbeat")  # noqa: S108 - container-local
RETENTION_EVERY = timedelta(days=1)


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        collector: Collector,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.collector = collector
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self.started_at = utcnow()

    async def enabled_sources(self) -> set[int]:
        async with self.sessionmaker() as session:
            ids = await session.scalars(
                select(DataSource.id).where(
                    DataSource.enabled.is_(True), DataSource.engine == SourceEngine.POSTGRES
                )
            )
            return set(ids)

    async def reconcile(self) -> None:
        """Make the running tasks match the enabled sources."""
        wanted = await self.enabled_sources()
        for source_id in wanted - self.tasks.keys():
            logger.info("collector: starting source_id=%s", source_id)
            self.tasks[source_id] = asyncio.create_task(
                self.source_loop(source_id), name=f"source-{source_id}"
            )
        for source_id in self.tasks.keys() - wanted:
            logger.info("collector: stopping source_id=%s", source_id)
            self.tasks.pop(source_id).cancel()

    async def source_loop(self, source_id: int, rng: random.Random | None = None) -> None:
        rng = rng or random.Random()  # noqa: S311 - timing jitter, not security
        interval = self.settings.collector_interval_seconds
        await asyncio.sleep(rng.uniform(0, interval))
        while True:
            outcome = await self.collector.collect(source_id)
            delay = next_delay(
                interval, outcome.consecutive_failures, self.settings.collector_max_backoff_seconds
            )
            await asyncio.sleep(jittered(delay, rng))

    async def heartbeat(self) -> None:
        now = utcnow()
        async with self.sessionmaker() as session:
            beat = await session.get(CollectorHeartbeat, 1)
            if beat is None:
                beat = CollectorHeartbeat(id=1, started_at=self.started_at, beat_at=now, host="")
                session.add(beat)
            beat.started_at = self.started_at
            beat.beat_at = now
            beat.host = socket.gethostname()
            due = (
                beat.last_retention_at is None
                or now - _aware(beat.last_retention_at) >= RETENTION_EVERY
            )
            if due:
                beat.last_retention_at = now
            await session.commit()
        if due:
            await self.collector.purge_old_data()
        # Not in a container (e.g. Windows dev)? Then only the DB heartbeat matters.
        with contextlib.suppress(OSError):
            await asyncio.to_thread(HEARTBEAT_FILE.touch)

    async def run(self) -> None:
        logger.info(
            "collector: running (interval %.0fs, retention %d days)",
            self.settings.collector_interval_seconds,
            self.settings.retention_days,
        )
        try:
            while True:
                try:
                    await self.reconcile()
                    await self.heartbeat()
                except Exception:  # noqa: BLE001 - keep supervising through app-DB blips
                    logger.exception("collector: supervisor loop error")
                await asyncio.sleep(self.settings.collector_refresh_seconds)
        finally:
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; they're stored as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
