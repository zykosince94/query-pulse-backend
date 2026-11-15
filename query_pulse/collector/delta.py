"""The delta engine: turn two pg_stat_statements snapshots into per-interval activity.

pg_stat_statements keeps *cumulative* counters per statement: `calls` only goes up, from the
moment the statement entered its hash table (or since the last reset). To know what happened
in the last minute, take a snapshot now, compare it with the previous one, and subtract.

    delta = current - previous            mean time for the interval = Δtotal_time / Δcalls

The hard part is knowing when subtraction would lie:

* Reset. `pg_stat_statements_reset()` sets every counter back to 0. `current - previous` would
  then be negative (or, if traffic since the reset exceeds the old total, a smaller positive
  number that silently undercounts). Detected by `stats_reset` changing, or any counter going
  down. The interval is thrown away; the current snapshot becomes the new baseline.
* New statement. A key that wasn't in the previous snapshot entered the hash table during the
  interval, so all of its counters belong to this interval: its delta is its current value.
* Eviction. When the table is full, pg_stat_statements drops the least-used entries and bumps
  `dealloc`. A dropped statement's activity since the last snapshot is lost; if it comes back,
  it restarts from 0 and is treated as new. We can't recover the loss, but we report it, so
  results are never silently incomplete.
* Restart. The previous snapshot is persisted (CollectorState.snapshot), so a restarted
  collector compares against the last snapshot it took, not against nothing.

Everything here is pure: no database, no clock, no I/O. That's what makes each rule testable
with a table of cases (tests/test_delta.py).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any


@dataclass(frozen=True, order=True)
class StatementKey:
    """pg_stat_statements' identity for an entry.

    `queryid` alone isn't unique: the same normalised query is tracked separately per database
    (dbid), per role (userid), and for top-level vs nested execution (toplevel).
    """

    dbid: int
    userid: int
    queryid: int
    toplevel: bool

    def encode(self) -> str:
        return f"{self.dbid}:{self.userid}:{self.queryid}:{int(self.toplevel)}"

    @classmethod
    def decode(cls, text: str) -> StatementKey:
        dbid, userid, queryid, toplevel = text.split(":")
        return cls(int(dbid), int(userid), int(queryid), toplevel == "1")


@dataclass(frozen=True)
class Counters:
    """The cumulative counters we track for one statement."""

    calls: int
    total_exec_time: float  # milliseconds
    rows: int
    shared_blks_hit: int
    shared_blks_read: int

    def minus(self, earlier: Counters) -> Counters:
        return Counters(
            calls=self.calls - earlier.calls,
            total_exec_time=self.total_exec_time - earlier.total_exec_time,
            rows=self.rows - earlier.rows,
            shared_blks_hit=self.shared_blks_hit - earlier.shared_blks_hit,
            shared_blks_read=self.shared_blks_read - earlier.shared_blks_read,
        )

    def any_negative(self) -> bool:
        # A tiny tolerance for total_exec_time: it's a float and can wobble in the last bits.
        return (
            self.calls < 0
            or self.total_exec_time < -1e-6
            or self.rows < 0
            or self.shared_blks_hit < 0
            or self.shared_blks_read < 0
        )


@dataclass(frozen=True)
class Snapshot:
    """Everything read from one source at one moment."""

    taken_at: datetime
    stats_reset: datetime | None  # pg_stat_statements_info.stats_reset
    dealloc: int | None  # pg_stat_statements_info.dealloc: entries evicted so far
    statements: dict[StatementKey, Counters]

    def to_json(self) -> dict[str, Any]:
        """A JSON-safe form for CollectorState.snapshot (survives collector restarts)."""
        return {
            "taken_at": self.taken_at.isoformat(),
            "stats_reset": self.stats_reset.isoformat() if self.stats_reset else None,
            "dealloc": self.dealloc,
            "statements": {
                key.encode(): [getattr(c, f.name) for f in fields(Counters)]
                for key, c in self.statements.items()
            },
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Snapshot:
        return cls(
            taken_at=datetime.fromisoformat(data["taken_at"]),
            stats_reset=(
                datetime.fromisoformat(data["stats_reset"]) if data["stats_reset"] else None
            ),
            dealloc=data["dealloc"],
            statements={
                StatementKey.decode(key): Counters(*values)
                for key, values in data["statements"].items()
            },
        )


@dataclass(frozen=True)
class Delta:
    """One statement's activity during one interval."""

    key: StatementKey
    counters: Counters

    @property
    def mean_exec_time(self) -> float:
        return self.counters.total_exec_time / self.counters.calls


@dataclass(frozen=True)
class DeltaResult:
    interval_start: datetime | None  # None when there's no interval (first snapshot)
    interval_end: datetime
    deltas: list[Delta]
    # Why the interval was discarded, if it was: "baseline" or a reset description.
    discarded: str | None = None
    # How many statements pg_stat_statements evicted during the interval (dealloc rose).
    evicted: int = 0


def compute_deltas(previous: Snapshot | None, current: Snapshot) -> DeltaResult:
    """Compare two snapshots of the same source and return what happened in between."""
    # First snapshot ever (or the baseline was lost): nothing to subtract from yet.
    if previous is None:
        return DeltaResult(None, current.taken_at, [], discarded="baseline")

    # Evictions are reported even if the interval is otherwise discarded below.
    evicted = 0
    if previous.dealloc is not None and current.dealloc is not None:
        evicted = max(0, current.dealloc - previous.dealloc)

    # An explicit reset: pg_stat_statements records when it last happened.
    if previous.stats_reset != current.stats_reset:
        return DeltaResult(
            previous.taken_at,
            current.taken_at,
            [],
            discarded="statistics were reset (stats_reset changed)",
            evicted=evicted,
        )

    deltas: list[Delta] = []
    for key, now in current.statements.items():
        before = previous.statements.get(key)
        # New since the last snapshot: everything it has counted happened in this interval.
        change = now if before is None else now.minus(before)
        # A counter went down: the statistics were reset in a way stats_reset didn't show
        # (e.g. a partial reset of one statement). Don't trust any number from this interval.
        if change.any_negative():
            return DeltaResult(
                previous.taken_at,
                current.taken_at,
                [],
                discarded="a counter decreased (statistics reset)",
                evicted=evicted,
            )
        # Idle during the interval: nothing to record.
        if change.calls > 0:
            deltas.append(Delta(key, change))

    # Keys only in `previous` were evicted (or reset). There's nothing to emit for them.
    return DeltaResult(previous.taken_at, current.taken_at, deltas, evicted=evicted)
