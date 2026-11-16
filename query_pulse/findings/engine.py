"""The rule engine: gather evidence, run the rules, reconcile findings.

    evaluate_source()  =  build_context()  →  run_rules()  →  reconcile()

`build_context` reads what the collector stored (observations, plans, relation samples) into
plain dataclasses; the rules are pure functions over it (rules.py); `reconcile` turns
detections into finding rows with a small, explicit lifecycle:

    detected, no row          → create, open
    detected, open            → refresh evidence and last_seen_at
    detected, dismissed       → refresh evidence, STAY dismissed     (QP-AC-21)
    detected, resolved        → reopen, but only on evidence newer than the resolution
    not detected, open        → resolve automatically once unseen for resolve_after

One row per (source, rule, target), guaranteed by a unique constraint, is the de-duplication.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from query_pulse.config import Settings
from query_pulse.findings.models import Finding, FindingStatus
from query_pulse.findings.rules import (
    RULES,
    Detection,
    IndexHistory,
    IndexSample,
    OperationFacts,
    PlanFacts,
    Rule,
    RuleContext,
    TableFacts,
    run_rules,
)
from query_pulse.workload import mongo_plan
from query_pulse.workload.models import Fingerprint, Observation, Plan, RelationStat
from query_pulse.workload.plan_tree import PlanFormatError, parse_plan
from query_pulse.workload.service import as_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reconciled:
    created: int = 0
    updated: int = 0
    reopened: int = 0
    resolved: int = 0


async def build_context(
    session: AsyncSession, source_id: int, settings: Settings, now: datetime
) -> RuleContext:
    start = now - timedelta(minutes=settings.rules_window_minutes)
    ctx = RuleContext(
        source_id=source_id,
        window_start=start,
        window_end=now,
        unused_index_min_span=timedelta(seconds=settings.rules_unused_index_min_span_seconds),
    )

    # Operations: window sums per fingerprint, and each one's share of the source's total.
    calls = func.sum(Observation.calls)
    total = func.sum(Observation.total_exec_time_ms)
    rows = list(
        await session.execute(
            select(
                Fingerprint.id,
                Fingerprint.query,
                Fingerprint.engine,
                Fingerprint.namespace,
                calls.label("calls"),
                total.label("total"),
                func.sum(Observation.rows).label("rows"),
                func.sum(Observation.keys_examined).label("keys"),
                func.sum(Observation.docs_examined).label("docs"),
            )
            .join(Observation, Observation.fingerprint_id == Fingerprint.id)
            .where(
                Observation.source_id == source_id,
                Observation.interval_end > start,
                Observation.interval_end <= now,
            )
            .group_by(Fingerprint.id, Fingerprint.query, Fingerprint.engine, Fingerprint.namespace)
        )
    )
    source_total = sum(float(r.total or 0.0) for r in rows)
    ctx.operations = [
        OperationFacts(
            fingerprint_id=r.id,
            query=r.query,
            calls=int(r.calls or 0),
            total_exec_time_ms=float(r.total or 0.0),
            share_of_total_time=float(r.total or 0.0) / source_total if source_total else 0.0,
            engine=r.engine,
            namespace=r.namespace,
            rows=int(r.rows or 0),
            keys_examined=None if r.keys is None else int(r.keys),
            docs_examined=None if r.docs is None else int(r.docs),
        )
        for r in rows
    ]

    # Plans: the latest *captured* plan of each of those operations (a failed capture is
    # no evidence). Latest = greatest id: rows are append-only.
    ids = [op.fingerprint_id for op in ctx.operations]
    if ids:
        latest = (
            select(func.max(Plan.id))
            .where(Plan.fingerprint_id.in_(ids), Plan.plan.is_not(None))
            .group_by(Plan.fingerprint_id)
        )
        for plan in await session.scalars(select(Plan).where(Plan.id.in_(latest))):
            checked_at = as_utc(plan.checked_at)
            try:
                if plan.engine == "mongodb":
                    ctx.plans[plan.fingerprint_id] = PlanFacts(
                        plan_id=plan.id,
                        root=None,
                        stage_root=mongo_plan.parse_plan(plan.plan),
                        checked_at=checked_at,
                    )
                else:
                    ctx.plans[plan.fingerprint_id] = PlanFacts(
                        plan_id=plan.id, root=parse_plan(plan.plan), checked_at=checked_at
                    )
            except (PlanFormatError, mongo_plan.MongoPlanFormatError):
                continue

    # Tables: the latest sample of each.
    latest_table = (
        select(func.max(RelationStat.id))
        .where(RelationStat.source_id == source_id, RelationStat.kind == "table")
        .group_by(RelationStat.schema_name, RelationStat.table_name)
    )
    ctx.tables = [
        TableFacts(
            relation_stat_id=t.id,
            schema_name=t.schema_name,
            table_name=t.table_name,
            live_rows=int(t.live_rows or 0),
            size_bytes=int(t.size_bytes),
            captured_at=as_utc(t.captured_at),
        )
        for t in await session.scalars(
            select(RelationStat).where(RelationStat.id.in_(latest_table))
        )
    ]

    # Indexes: every sample in the unused-index window, oldest first.
    since = now - timedelta(hours=settings.rules_unused_index_window_hours)
    history: dict[tuple[str, str, str], list[RelationStat]] = defaultdict(list)
    for sample in await session.scalars(
        select(RelationStat)
        .where(
            RelationStat.source_id == source_id,
            RelationStat.kind == "index",
            RelationStat.captured_at > since,
        )
        .order_by(RelationStat.captured_at, RelationStat.id)
    ):
        history[(sample.schema_name, sample.table_name, sample.index_name or "")].append(sample)
    ctx.indexes = [
        IndexHistory(
            schema_name=schema,
            table_name=table,
            index_name=index,
            is_unique=samples[-1].is_unique,
            samples=[
                IndexSample(
                    relation_stat_id=s.id,
                    captured_at=as_utc(s.captured_at),
                    idx_scan=int(s.idx_scan or 0),
                    size_bytes=int(s.size_bytes),
                )
                for s in samples
            ],
        )
        for (schema, table, index), samples in history.items()
    ]
    return ctx


def _apply(finding: Finding, detection: Detection, rule: Rule, now: datetime) -> None:
    finding.title = detection.title
    finding.summary = detection.summary
    finding.confidence = detection.confidence
    finding.evidence_strength = rule.evidence_strength
    finding.suggested_action = detection.suggested_action
    finding.action_sql = detection.action_sql
    finding.facts = detection.facts
    finding.evidence = detection.evidence
    finding.fingerprint_id = detection.fingerprint_id
    finding.relation = detection.relation
    finding.last_seen_at = now


async def reconcile(
    session: AsyncSession,
    source_id: int,
    detections: list[Detection],
    settings: Settings,
    now: datetime,
) -> Reconciled:
    rules = {rule.id: rule for rule in RULES}
    existing = {
        (f.rule_id, f.target_key): f
        for f in await session.scalars(select(Finding).where(Finding.source_id == source_id))
    }
    created = updated = reopened = resolved = 0
    seen: set[tuple[str, str]] = set()

    for detection in detections:
        key = (detection.rule_id, detection.target_key)
        if key in seen:
            continue  # a rule reports a target once per evaluation
        seen.add(key)
        rule = rules[detection.rule_id]
        finding = existing.get(key)
        if finding is None:
            finding = Finding(
                source_id=source_id,
                rule_id=detection.rule_id,
                target_kind=detection.target_kind,
                target_key=detection.target_key,
                status=FindingStatus.OPEN.value,
                status_changed_by="rules",
                status_changed_at=now,
                first_seen_at=now,
            )
            _apply(finding, detection, rule, now)
            session.add(finding)
            created += 1
        elif finding.status == FindingStatus.RESOLVED.value:
            # Reopen only on evidence collected after the resolution: right after someone
            # adds the index, the plan on record is still the old one for a few minutes.
            resolved_at = as_utc(finding.resolved_at) if finding.resolved_at else None
            if resolved_at is None or detection.evidence_at > resolved_at:
                _apply(finding, detection, rule, now)
                finding.status = FindingStatus.OPEN.value
                finding.status_changed_by = "rules"
                finding.status_changed_at = now
                finding.resolved_at = None
                reopened += 1
        else:
            # Open or dismissed: refresh what we know, keep the status (QP-AC-21).
            _apply(finding, detection, rule, now)
            updated += 1

    # Open findings whose problem hasn't been seen for a while: resolved on their own.
    stale_before = now - timedelta(seconds=settings.finding_resolve_after_seconds)
    for key, finding in existing.items():
        if (
            key not in seen
            and finding.status == FindingStatus.OPEN.value
            and as_utc(finding.last_seen_at) < stale_before
        ):
            finding.status = FindingStatus.RESOLVED.value
            finding.status_changed_by = "rules"
            finding.status_changed_at = now
            finding.resolved_at = now
            resolved += 1

    return Reconciled(created, updated, reopened, resolved)


async def evaluate_source(
    session: AsyncSession, source_id: int, settings: Settings, now: datetime
) -> Reconciled:
    """Run every rule for one source and update its findings. The caller commits."""
    ctx = await build_context(session, source_id, settings, now)
    result = await reconcile(session, source_id, run_rules(ctx), settings, now)
    logger.info(
        "findings: source_id=%s created=%d updated=%d reopened=%d resolved=%d",
        source_id,
        result.created,
        result.updated,
        result.reopened,
        result.resolved,
    )
    return result


async def evidence_available(session: AsyncSession, item: dict[str, Any]) -> bool:
    """Does the data an evidence item points at still exist? (QP-AC-20)

    Kinds: "operation" (observations of a fingerprint in [start, end]), "plan" (a plan row),
    "relation_stat" (a table or index sample). Retention can age the data out later; the
    finding keeps its facts, and the link reports itself unavailable.
    """
    kind = item.get("kind")
    if kind == "plan":
        query = select(exists().where(Plan.id == item.get("plan_id")))
    elif kind == "relation_stat":
        query = select(exists().where(RelationStat.id == item.get("relation_stat_id")))
    elif kind == "operation":
        start = datetime.fromisoformat(str(item.get("start")))
        end = datetime.fromisoformat(str(item.get("end")))
        query = select(
            exists().where(
                Observation.fingerprint_id == item.get("fingerprint_id"),
                Observation.interval_end > start,
                Observation.interval_end <= end,
            )
        )
    else:
        return False
    return bool(await session.scalar(query))
