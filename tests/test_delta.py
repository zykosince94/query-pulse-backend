"""The delta engine, table-driven: every rule in collector/delta.py has a case here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from query_pulse.collector.delta import (
    Counters,
    Snapshot,
    StatementKey,
    compute_deltas,
)

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
RESET_A = datetime(2026, 11, 1, tzinfo=UTC)
RESET_B = datetime(2026, 11, 16, 12, 0, 30, tzinfo=UTC)

Q1 = StatementKey(dbid=16384, userid=10, queryid=111, toplevel=True)
Q2 = StatementKey(dbid=16384, userid=10, queryid=222, toplevel=True)
Q3 = StatementKey(dbid=16384, userid=10, queryid=333, toplevel=True)


def c(calls: int, total: float, rows: int = 0, hit: int = 0, read: int = 0) -> Counters:
    return Counters(calls, total, rows, hit, read)


def snap(
    minute: int,
    statements: dict[StatementKey, Counters],
    *,
    stats_reset: datetime | None = RESET_A,
    dealloc: int | None = 0,
) -> Snapshot:
    return Snapshot(T0 + timedelta(minutes=minute), stats_reset, dealloc, statements)


def test_first_snapshot_is_only_a_baseline() -> None:
    result = compute_deltas(None, snap(0, {Q1: c(10, 50.0)}))
    assert result.deltas == []
    assert result.discarded == "baseline"
    assert result.interval_start is None


def test_normal_interval_subtracts_counters() -> None:
    previous = snap(0, {Q1: c(10, 50.0, rows=100, hit=40, read=4)})
    current = snap(1, {Q1: c(25, 110.0, rows=250, hit=95, read=6)})
    result = compute_deltas(previous, current)

    assert result.discarded is None
    assert (result.interval_start, result.interval_end) == (previous.taken_at, current.taken_at)
    [delta] = result.deltas
    assert delta.key == Q1
    assert delta.counters == c(15, 60.0, rows=150, hit=55, read=2)
    assert delta.mean_exec_time == pytest.approx(4.0)  # 60 ms / 15 calls


def test_idle_statement_produces_no_delta() -> None:
    previous = snap(0, {Q1: c(10, 50.0), Q2: c(3, 9.0)})
    current = snap(1, {Q1: c(10, 50.0), Q2: c(5, 15.0)})
    assert [d.key for d in compute_deltas(previous, current).deltas] == [Q2]


def test_new_statement_counts_everything_it_has() -> None:
    previous = snap(0, {Q1: c(10, 50.0)})
    current = snap(1, {Q1: c(12, 60.0), Q2: c(7, 21.0)})
    deltas = {d.key: d.counters for d in compute_deltas(previous, current).deltas}
    assert deltas == {Q1: c(2, 10.0), Q2: c(7, 21.0)}


def test_evicted_statement_is_dropped_and_reported() -> None:
    previous = snap(0, {Q1: c(10, 50.0), Q2: c(4, 8.0)}, dealloc=5)
    current = snap(1, {Q1: c(11, 55.0)}, dealloc=7)
    result = compute_deltas(previous, current)
    assert [d.key for d in result.deltas] == [Q1]
    assert result.evicted == 2


def test_evicted_statement_that_returns_restarts_from_zero() -> None:
    """Q2 was evicted and re-entered: its small counters are all from this interval."""
    previous = snap(0, {Q1: c(10, 50.0)}, dealloc=5)  # Q2 already evicted
    current = snap(1, {Q1: c(10, 50.0), Q2: c(2, 4.0)}, dealloc=5)
    assert {d.key: d.counters for d in compute_deltas(previous, current).deltas} == {Q2: c(2, 4.0)}


def test_stats_reset_change_discards_the_interval() -> None:
    previous = snap(0, {Q1: c(1000, 5000.0)}, stats_reset=RESET_A)
    # After the reset, Q1 has run 1200 times: more than before, so subtraction would give a
    # plausible-looking but wrong 200. The changed timestamp is what catches it.
    current = snap(1, {Q1: c(1200, 5400.0)}, stats_reset=RESET_B)
    result = compute_deltas(previous, current)
    assert result.deltas == []
    assert result.discarded is not None and "stats_reset" in result.discarded


@pytest.mark.parametrize(
    "after",
    [
        c(5, 60.0),  # calls went down
        c(15, 40.0),  # total time went down
        c(15, 60.0, rows=10),  # rows went down
        c(15, 60.0, rows=100, hit=1),  # buffer hits went down
    ],
)
def test_any_counter_decreasing_discards_the_interval(after: Counters) -> None:
    previous = snap(0, {Q1: c(10, 50.0, rows=100, hit=40), Q2: c(1, 1.0)})
    current = snap(1, {Q1: after, Q2: c(9, 9.0)})
    result = compute_deltas(previous, current)
    assert result.deltas == []
    assert result.discarded == "a counter decreased (statistics reset)"


def test_float_noise_in_total_time_is_not_a_reset() -> None:
    previous = snap(0, {Q1: c(10, 50.0000000001)})
    current = snap(1, {Q1: c(11, 50.0)})  # equal within float precision
    assert len(compute_deltas(previous, current).deltas) == 1


def test_evictions_are_reported_even_when_the_interval_is_discarded() -> None:
    previous = snap(0, {Q1: c(10, 50.0)}, dealloc=1)
    current = snap(1, {Q1: c(3, 5.0)}, dealloc=4)
    result = compute_deltas(previous, current)
    assert result.deltas == []
    assert result.evicted == 3


def test_dealloc_unknown_means_no_eviction_report() -> None:
    previous = snap(0, {Q1: c(10, 50.0)}, dealloc=None)
    current = snap(1, {Q1: c(11, 55.0)}, dealloc=3)
    assert compute_deltas(previous, current).evicted == 0


def test_restart_baseline_survives_serialisation() -> None:
    """A restarted collector loads the persisted snapshot; the next delta must be correct."""
    before_restart = snap(0, {Q1: c(10, 50.0, 100, 40, 4), Q3: c(1, 0.5)}, dealloc=9)
    persisted = before_restart.to_json()
    reloaded = Snapshot.from_json(persisted)
    assert reloaded == before_restart

    after_restart = snap(1, {Q1: c(110, 550.0, 1100, 440, 44), Q3: c(1, 0.5)}, dealloc=9)
    [delta] = compute_deltas(reloaded, after_restart).deltas
    assert delta.key == Q1
    assert delta.counters == c(100, 500.0, 1000, 400, 40)


def test_statement_key_round_trip() -> None:
    nested = StatementKey(dbid=5, userid=7, queryid=-8_123_456_789_012_345_678, toplevel=False)
    assert StatementKey.decode(nested.encode()) == nested
