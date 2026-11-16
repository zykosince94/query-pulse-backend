"""The rules: each one is declared once, with what it needs and how sure it can be.

A rule is data plus one pure function. `detect(rule, context)` looks only at the evidence in
the `RuleContext` (operation metrics over the window, the latest captured plans, table and
index samples) and returns `Detection`s. It never touches the database, so every rule is
unit-tested with fixture evidence, including a healthy control that must stay silent.

Two kinds of evidence, labelled differently in the UI (the blueprint's rule):
  - "plan": strong evidence, read from a captured plan (what PostgreSQL will actually do).
  - "statistics": a heuristic from counters alone (e.g. an index with zero scans).

Rules are deliberately disjoint, so one problem produces one finding: PG-SEQSCAN-HOT needs a
filter that isn't a LIKE (a LIKE scan is PG-LEADING-WILDCARD's), and a full scan feeding a
sort under a LIMIT has no filter (it's PG-SORT-NO-INDEX's).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from query_pulse.workload.plan_tree import walk_with_ancestors
from query_pulse.workload.schemas import PlanNode

Confidence = Literal["high", "medium", "low"]
EvidenceStrength = Literal["plan", "statistics"]

# --- the evidence a rule can look at ---------------------------------------------------------


@dataclass(frozen=True)
class OperationFacts:
    """One operation's activity over the rules window (sums of its observations)."""

    fingerprint_id: int
    query: str
    calls: int
    total_exec_time_ms: float
    share_of_total_time: float  # of the source's total in the window, 0..1


@dataclass(frozen=True)
class PlanFacts:
    plan_id: int
    root: PlanNode
    checked_at: datetime


@dataclass(frozen=True)
class TableFacts:
    """The latest sample of one table."""

    relation_stat_id: int
    schema_name: str
    table_name: str
    live_rows: int
    size_bytes: int
    captured_at: datetime


@dataclass(frozen=True)
class IndexSample:
    relation_stat_id: int
    captured_at: datetime
    idx_scan: int
    size_bytes: int


@dataclass(frozen=True)
class IndexHistory:
    """Every sample of one index within the unused-index window, oldest first."""

    schema_name: str
    table_name: str
    index_name: str
    is_unique: bool | None
    samples: list[IndexSample]


@dataclass
class RuleContext:
    source_id: int
    window_start: datetime
    window_end: datetime
    operations: list[OperationFacts] = field(default_factory=list)
    plans: dict[int, PlanFacts] = field(default_factory=dict)  # by fingerprint id
    tables: list[TableFacts] = field(default_factory=list)
    indexes: list[IndexHistory] = field(default_factory=list)
    unused_index_min_span: timedelta = timedelta(hours=1)

    def table(self, name: str | None) -> TableFacts | None:
        """The table a plan node names. Plans don't say the schema (without VERBOSE), so
        an ambiguous name (the same table in two schemas) gives no answer, not a guess."""
        matches = [t for t in self.tables if t.table_name == name]
        return matches[0] if len(matches) == 1 else None


# --- what a rule returns ----------------------------------------------------------------------


@dataclass(frozen=True)
class Detection:
    rule_id: str
    target_kind: Literal["operation", "index"]
    target_key: str
    title: str
    summary: str
    confidence: Confidence
    suggested_action: str
    action_sql: str | None
    facts: list[dict[str, Any]]  # {"key", "label", "value"}
    evidence: list[dict[str, Any]]  # see engine.evidence_available() for the kinds
    # The newest evidence behind this detection: a finding the user resolved reopens only
    # if the problem shows up in evidence collected *after* they resolved it.
    evidence_at: datetime
    fingerprint_id: int | None = None
    relation: str | None = None


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    description: str
    evidence_strength: EvidenceStrength
    confidence: Confidence  # the usual confidence; a detection can state its own
    thresholds: dict[str, float]
    detect: Callable[[Rule, RuleContext], list[Detection]]


# --- helpers ----------------------------------------------------------------------------------

_IDENTIFIER = r"[a-z_][a-z0-9_$]*"
# `customer_id = $1` inside a plan filter, e.g. "(customer_id = $1)" or
# "((status = $1) AND (customer_id = $2))".
_EQUALITY = re.compile(rf"\b({_IDENTIFIER})\s*=\s*\$\d+")
# One sort key: "orders.created_at DESC", "created_at", "total NULLS FIRST".
_SORT_KEY = re.compile(
    rf"^(?:{_IDENTIFIER}\.)?({_IDENTIFIER})((?: DESC| ASC)?(?: NULLS (?:FIRST|LAST))?)$"
)
# A LIKE/ILIKE in a plan filter: PostgreSQL shows LIKE as `~~` and ILIKE as `~~*`.
_LIKE = re.compile(r"~~\*?\s+(\$\d+|'(?:[^']|'')*')")
_SCANS = ("Seq Scan", "Parallel Seq Scan")


def _fact(key: str, label: str, value: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "value": value}


def _qualified(table: TableFacts) -> str:
    return f"{table.schema_name}.{table.table_name}"


def _estimated_rows(node: PlanNode, ancestors: Sequence[PlanNode]) -> float:
    """Rows a scan is estimated to return in total. A parallel scan's estimate is per
    process, so it's scaled by the workers planned (plus the leader) of its Gather."""
    if node.node_type != "Parallel Seq Scan":
        return node.plan_rows
    for ancestor in reversed(ancestors):
        if ancestor.node_type in ("Gather", "Gather Merge"):
            workers = float(ancestor.details.get("Workers Planned", "0") or 0)
            return node.plan_rows * (workers + 1)
    return node.plan_rows


def _operation_evidence(ctx: RuleContext, op: OperationFacts) -> dict[str, Any]:
    return {
        "kind": "operation",
        "fingerprint_id": op.fingerprint_id,
        "start": ctx.window_start.isoformat(),
        "end": ctx.window_end.isoformat(),
        "label": f"{op.calls:,} calls, {op.total_exec_time_ms:,.0f} ms in the window",
    }


def _plan_evidence(plan: PlanFacts, fingerprint_id: int, node: PlanNode) -> dict[str, Any]:
    on = f" on {node.relation_name}" if node.relation_name else ""
    return {
        "kind": "plan",
        "plan_id": plan.plan_id,
        "fingerprint_id": fingerprint_id,
        "node_path": node.path,
        "label": f"Plan #{plan.plan_id}: {node.node_type}{on}",
    }


def _table_evidence(table: TableFacts) -> dict[str, Any]:
    return {
        "kind": "relation_stat",
        "relation_stat_id": table.relation_stat_id,
        "label": f"{_qualified(table)}: {table.live_rows:,} live rows",
    }


def _operation_target(op: OperationFacts) -> str:
    return f"operation:{op.fingerprint_id}"


# --- PG-SEQSCAN-HOT (planted problem P1) -------------------------------------------------------


def detect_seqscan_hot(rule: Rule, ctx: RuleContext) -> list[Detection]:
    """A hot operation whose plan reads a whole large table to keep a few rows."""
    t = rule.thresholds
    found = []
    for op in ctx.operations:
        plan = ctx.plans.get(op.fingerprint_id)
        if (
            plan is None
            or op.calls < t["min_calls"]
            or op.share_of_total_time < t["min_share_of_total_time"]
        ):
            continue
        for node, ancestors in walk_with_ancestors(plan.root):
            if node.node_type not in _SCANS or not node.filter or _LIKE.search(node.filter):
                continue
            table = ctx.table(node.relation_name)
            if table is None or table.live_rows < t["min_table_rows"]:
                continue
            rows = _estimated_rows(node, ancestors)
            selectivity = rows / table.live_rows
            if selectivity > t["max_selectivity"]:
                continue  # it keeps much of the table: a full scan can be the right plan
            columns = list(dict.fromkeys(_EQUALITY.findall(node.filter)))
            action_sql = (
                f"CREATE INDEX CONCURRENTLY ON {_qualified(table)} ({', '.join(columns)});"
                if columns
                else None
            )
            found.append(
                Detection(
                    rule_id=rule.id,
                    target_kind="operation",
                    target_key=_operation_target(op),
                    fingerprint_id=op.fingerprint_id,
                    relation=_qualified(table),
                    title=f"Sequential scan on {_qualified(table)} in a hot query",
                    summary=(
                        f"This query takes {op.share_of_total_time:.0%} of the source's "
                        f"execution time, and its plan reads all {table.live_rows:,} rows "
                        f"of {_qualified(table)} to keep about {rows:,.0f} "
                        f"(filter {node.filter}). An index on the filtered column would "
                        "let PostgreSQL find those rows directly."
                    ),
                    confidence=rule.confidence,
                    suggested_action=(
                        f"Create an index on {_qualified(table)} ({', '.join(columns)}), "
                        "then check the plan switches to an index scan. CONCURRENTLY "
                        "avoids blocking writes while it builds; it takes longer and can't "
                        "run inside a transaction."
                        if columns
                        else f"Index the column(s) used in the filter {node.filter}."
                    ),
                    action_sql=action_sql,
                    facts=[
                        _fact(
                            "share_of_total_time",
                            "Share of execution time",
                            round(op.share_of_total_time, 4),
                        ),
                        _fact("calls", "Calls in the window", op.calls),
                        _fact(
                            "total_exec_time_ms",
                            "Execution time in the window",
                            round(op.total_exec_time_ms, 1),
                        ),
                        _fact("table_rows", "Rows in the table", table.live_rows),
                        _fact("estimated_rows", "Rows the scan keeps (estimate)", round(rows, 1)),
                        _fact("selectivity", "Fraction of the table kept", round(selectivity, 6)),
                    ],
                    evidence=[
                        _operation_evidence(ctx, op),
                        _plan_evidence(plan, op.fingerprint_id, node),
                        _table_evidence(table),
                    ],
                    evidence_at=max(plan.checked_at, table.captured_at, ctx.window_end),
                )
            )
            break  # one finding per operation
    return found


# --- PG-SORT-NO-INDEX (P2) ---------------------------------------------------------------------


def detect_sort_no_index(rule: Rule, ctx: RuleContext) -> list[Detection]:
    """A LIMIT over a Sort over a full scan: the whole table is sorted to keep a few rows."""
    t = rule.thresholds
    found = []
    for op in ctx.operations:
        plan = ctx.plans.get(op.fingerprint_id)
        if plan is None or op.calls < t["min_calls"]:
            continue
        for node, ancestors in walk_with_ancestors(plan.root):
            if node.node_type not in _SCANS:
                continue
            # Walking up from the scan: the nearest Sort, with a Limit above it (Gathers in
            # between are just parallelism).
            positions = [i for i, a in enumerate(ancestors) if a.node_type == "Sort"]
            if not positions or not ancestors[positions[-1]].sort_key:
                continue
            sort = ancestors[positions[-1]]
            above_sort = ancestors[: positions[-1]]
            limit = next((a for a in reversed(above_sort) if a.node_type == "Limit"), None)
            if limit is None:
                continue
            table = ctx.table(node.relation_name)
            if table is None or table.live_rows < t["min_table_rows"]:
                continue
            keys = [_SORT_KEY.match(k.strip()) for k in sort.sort_key]
            index_keys = [f"{m.group(1)}{m.group(2)}" for m in keys if m is not None]
            action_sql = (
                f"CREATE INDEX CONCURRENTLY ON {_qualified(table)} ({', '.join(index_keys)});"
                if index_keys and len(index_keys) == len(keys)
                else None
            )
            keys_text = ", ".join(sort.sort_key)
            found.append(
                Detection(
                    rule_id=rule.id,
                    target_kind="operation",
                    target_key=_operation_target(op),
                    fingerprint_id=op.fingerprint_id,
                    relation=_qualified(table),
                    title=f"Sorts all of {_qualified(table)} to return the first rows",
                    summary=(
                        f"The plan reads every row of {_qualified(table)} "
                        f"({table.live_rows:,}) and sorts them by {keys_text}, only for the "
                        "LIMIT to keep the first few. An index in the sort order would let "
                        "PostgreSQL read just those rows, already in order."
                    ),
                    confidence=rule.confidence,
                    suggested_action=(
                        f"Create an index on {_qualified(table)} in the sort order "
                        f"({', '.join(index_keys)}), then check the plan becomes an index "
                        "scan under the Limit, with no Sort."
                        if action_sql
                        else f"Index {_qualified(table)} in the sort order ({keys_text})."
                    ),
                    action_sql=action_sql,
                    facts=[
                        _fact("calls", "Calls in the window", op.calls),
                        _fact(
                            "total_exec_time_ms",
                            "Execution time in the window",
                            round(op.total_exec_time_ms, 1),
                        ),
                        _fact("table_rows", "Rows in the table", table.live_rows),
                        # With LIMIT $1 the planner doesn't know the limit and guesses (10%
                        # of the rows), so this is labelled as the estimate it is.
                        _fact(
                            "limit_rows",
                            "Rows under the Limit (planner's estimate)",
                            round(limit.plan_rows, 1),
                        ),
                        _fact("sort_key", "Sort key", keys_text),
                    ],
                    evidence=[
                        _operation_evidence(ctx, op),
                        _plan_evidence(plan, op.fingerprint_id, sort),
                        _table_evidence(table),
                    ],
                    evidence_at=max(plan.checked_at, table.captured_at, ctx.window_end),
                )
            )
            break
    return found


# --- PG-LEADING-WILDCARD (P3) ------------------------------------------------------------------


def detect_leading_wildcard(rule: Rule, ctx: RuleContext) -> list[Detection]:
    """A LIKE/ILIKE that PostgreSQL can only answer by scanning the table.

    pg_stat_statements replaces literals with $n, so the pattern itself is usually not
    visible: `name LIKE $1` could be 'abc%' (an anchored prefix) or '%abc%'. When the plan
    shows a literal starting with %, the cause is certain (high confidence). When it's a
    parameter, the scan is certain but the leading wildcard is inferred (medium), and the
    finding says so.
    """
    t = rule.thresholds
    found = []
    for op in ctx.operations:
        plan = ctx.plans.get(op.fingerprint_id)
        if plan is None or op.calls < t["min_calls"]:
            continue
        for node, _ in walk_with_ancestors(plan.root):
            if node.node_type not in _SCANS or not node.filter:
                continue
            match = _LIKE.search(node.filter)
            if match is None:
                continue
            pattern = match.group(1)
            if pattern.startswith("'") and not pattern.startswith("'%"):
                continue  # an anchored literal ('abc%') can use a suitable b-tree index
            table = ctx.table(node.relation_name)
            if table is None or table.live_rows < t["min_table_rows"]:
                continue
            visible = pattern.startswith("'%")
            column = re.search(rf"({_IDENTIFIER})\s+~~", node.filter)
            col = column.group(1) if column else None
            found.append(
                Detection(
                    rule_id=rule.id,
                    target_kind="operation",
                    target_key=_operation_target(op),
                    fingerprint_id=op.fingerprint_id,
                    relation=_qualified(table),
                    title=f"Pattern search scans all of {_qualified(table)}",
                    summary=(
                        f"The plan reads every row of {_qualified(table)} "
                        f"({table.live_rows:,}) to test the pattern ({node.filter}). "
                        + (
                            "The pattern starts with %, which a b-tree index can't use."
                            if visible
                            else "The pattern is a parameter, so its value isn't recorded; "
                            "searches like '%term%' (a leading wildcard) can't use a b-tree "
                            "index, which matches this scan."
                        )
                    ),
                    confidence="high" if visible else rule.confidence,
                    suggested_action=(
                        "If the search patterns start with %, a trigram index (pg_trgm) "
                        f"lets PostgreSQL search {col or 'the column'} without a full scan. "
                        "It's larger than a b-tree and slows writes a little. If patterns "
                        "are always anchored ('term%'), a b-tree with text_pattern_ops is "
                        "enough instead."
                    ),
                    action_sql=(
                        "CREATE EXTENSION IF NOT EXISTS pg_trgm;\n"
                        f"CREATE INDEX CONCURRENTLY ON {_qualified(table)} "
                        f"USING gin ({col} gin_trgm_ops);"
                        if col
                        else None
                    ),
                    facts=[
                        _fact("calls", "Calls in the window", op.calls),
                        _fact(
                            "total_exec_time_ms",
                            "Execution time in the window",
                            round(op.total_exec_time_ms, 1),
                        ),
                        _fact("table_rows", "Rows in the table", table.live_rows),
                        _fact("pattern", "Pattern", pattern),
                        _fact("pattern_visible", "Leading % visible in the plan", visible),
                    ],
                    evidence=[
                        _operation_evidence(ctx, op),
                        _plan_evidence(plan, op.fingerprint_id, node),
                        _table_evidence(table),
                    ],
                    evidence_at=max(plan.checked_at, table.captured_at, ctx.window_end),
                )
            )
            break
    return found


# --- PG-UNUSED-INDEX (P4) ----------------------------------------------------------------------


def detect_unused_index(rule: Rule, ctx: RuleContext) -> list[Detection]:
    """A sizeable index with zero scans in every sample across the window."""
    t = rule.thresholds
    found = []
    for index in ctx.indexes:
        if index.is_unique is not False or len(index.samples) < 2:
            continue  # UNIQUE/PRIMARY KEY indexes enforce constraints; unknown is skipped
        first, last = index.samples[0], index.samples[-1]
        if last.captured_at - first.captured_at < ctx.unused_index_min_span:
            continue  # not watched for long enough to call it unused
        if any(s.idx_scan > 0 for s in index.samples) or last.size_bytes < t["min_size_bytes"]:
            continue
        span_minutes = (last.captured_at - first.captured_at).total_seconds() / 60
        watched = (
            f"{span_minutes / 60:,.1f} hours"
            if span_minutes >= 120
            else f"{span_minutes:,.0f} minutes"
        )
        name = f"{index.schema_name}.{index.index_name}"
        size_mb = last.size_bytes / (1024 * 1024)
        found.append(
            Detection(
                rule_id=rule.id,
                target_kind="index",
                target_key=f"index:{name}",
                relation=name,
                title=f"Index {name} is never used",
                summary=(
                    f"{name} (on {index.schema_name}.{index.table_name}, {size_mb:,.1f} MB) "
                    f"had zero index scans in every sample over {watched}. It "
                    "still costs disk space and slows every write to the table. Check before "
                    "dropping: these counters cover this server only (not replicas, which "
                    "may use it), and they restart from zero after a statistics reset."
                ),
                confidence=rule.confidence,
                suggested_action=(
                    "Confirm the index is unused on every replica and since the last "
                    "statistics reset, then drop it. DROP INDEX CONCURRENTLY avoids blocking "
                    "queries. Keep its definition so it can be recreated."
                ),
                action_sql=f"DROP INDEX CONCURRENTLY {name};",
                facts=[
                    _fact("size_bytes", "Index size", last.size_bytes),
                    _fact("idx_scan", "Index scans", last.idx_scan),
                    _fact("observed_minutes", "Watched for (minutes)", round(span_minutes, 1)),
                    _fact("samples", "Samples", len(index.samples)),
                ],
                evidence=[
                    {
                        "kind": "relation_stat",
                        "relation_stat_id": first.relation_stat_id,
                        "label": f"{name}: 0 scans at the first sample",
                    },
                    {
                        "kind": "relation_stat",
                        "relation_stat_id": last.relation_stat_id,
                        "label": f"{name}: 0 scans at the latest sample",
                    },
                ],
                evidence_at=last.captured_at,
            )
        )
    return found


# --- the catalogue -----------------------------------------------------------------------------

RULES: tuple[Rule, ...] = (
    Rule(
        id="PG-SEQSCAN-HOT",
        title="Sequential scan in a hot query",
        description=(
            "An operation with a large share of execution time whose plan scans a whole large "
            "table with a selective filter: an index on the filtered column is likely missing."
        ),
        evidence_strength="plan",
        confidence="high",
        thresholds={
            "min_share_of_total_time": 0.10,
            "min_calls": 10,
            "min_table_rows": 10_000,
            "max_selectivity": 0.05,
        },
        detect=detect_seqscan_hot,
    ),
    Rule(
        id="PG-SORT-NO-INDEX",
        title="Sort without a supporting index",
        description=(
            "The plan sorts a full scan of a large table under a LIMIT: an index in the sort "
            "order would return the first rows directly."
        ),
        evidence_strength="plan",
        confidence="high",
        thresholds={"min_calls": 10, "min_table_rows": 10_000},
        detect=detect_sort_no_index,
    ),
    Rule(
        id="PG-LEADING-WILDCARD",
        title="Leading-wildcard pattern search",
        description=(
            "A LIKE or ILIKE answered by scanning the table; a leading % can't use a b-tree "
            "index (a trigram index can)."
        ),
        evidence_strength="plan",
        confidence="medium",
        thresholds={"min_calls": 10, "min_table_rows": 1_000},
        detect=detect_leading_wildcard,
    ),
    Rule(
        id="PG-UNUSED-INDEX",
        title="Unused index",
        description=(
            "A non-unique index of meaningful size with zero scans throughout the window. "
            "Heuristic: replicas and statistics resets aren't visible."
        ),
        evidence_strength="statistics",
        confidence="medium",
        thresholds={"min_size_bytes": 1024 * 1024},
        detect=detect_unused_index,
    ),
)

RULES_BY_ID = {rule.id: rule for rule in RULES}


def run_rules(ctx: RuleContext, rules: Sequence[Rule] = RULES) -> list[Detection]:
    return [detection for rule in rules for detection in rule.detect(rule, ctx)]
