"""The evidence bundle: everything the model may see about a finding, and nothing else.

Built by code, never by the model, from what the rules already used: the rule, the finding's
facts, the operation's metrics over the finding's window, the cited plan (node by node or stage
by stage), and the table or index samples it cites. Every item has an id ("fact:table_rows",
"plan:0.1", ...) and the model must cite those ids for each claim; the server rejects any id
that isn't here (QP-AC-27).

What is never in a bundle (QP-AC-28):
- literals: single-quoted strings and bare numbers in SQL become ?, single-quoted strings in
  prose and facts become '?'. MongoDB shapes and plans are already redacted (ADR 0010);
- credentials, and anything that locates a server: no host, port, username, database name or
  connection string. The source is described by its engine and version only.

Answers are cached by the bundle's hash, which identifies "the same question". The rules
refresh a finding every minute, so the exact counts in it drift constantly; hashing them as
they are would make every answer look stale a minute later and the cache never hit. So the
hash is taken over a *stable* view of the bundle: numbers rounded to two significant figures,
sample timestamps left out, and items named by what they are ("table:public.orders"), not by
the sample row they came from. The bundle that's sent keeps the exact numbers; an answer is
reused until the evidence changes materially (a table ten times larger, a different plan).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from query_pulse.findings.models import Finding
from query_pulse.findings.rules import RULES_BY_ID
from query_pulse.sources.models import DataSource
from query_pulse.workload import explorer, mongo_plan
from query_pulse.workload.explorer import Window
from query_pulse.workload.models import Fingerprint, Plan, RelationStat
from query_pulse.workload.plan_tree import PlanFormatError, parse_plan, walk
from query_pulse.workload.schemas import MongoPlanStage, PlanNode
from query_pulse.workload.service import as_utc

BUNDLE_VERSION = 1

# 'it''s' (with doubled quotes inside), E'...' escapes included.
_QUOTED = re.compile(r"[eE]?'(?:[^']|'')*'")
# A bare number that isn't part of an identifier, a $1 parameter or a qualified name.
_NUMBER = re.compile(r"(?<![\w$.])-?\d+(?:\.\d+)?(?![\w.])")


def redact_sql(text: str | None) -> str | None:
    """SQL with its literals replaced: 'abc' -> '?', 42 -> ?. Parameters ($1) stay."""
    if text is None:
        return None
    return _NUMBER.sub("?", _QUOTED.sub("'?'", text))


def redact_text(text: str | None) -> str | None:
    """Prose (a summary, a fact): only quoted literals go; the numbers are statistics."""
    if text is None:
        return None
    return _QUOTED.sub("'?'", text)


@dataclass(frozen=True)
class Bundle:
    document: dict[str, Any]

    @property
    def ids(self) -> set[str]:
        return {item["id"] for item in self.document["items"]}

    @property
    def text(self) -> str:
        return json.dumps(self.document, sort_keys=True, separators=(",", ":"), default=str)

    @property
    def hash(self) -> str:
        stable = json.dumps(_stable(self.document), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(stable.encode()).hexdigest()


_VOLATILE_KEYS = {"sampled_at"}


def _stable(value: Any) -> Any:
    """The bundle as the cache sees it: two significant figures, no sample timestamps."""
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value
    return float(f"{value:.2g}")


def _item(item_id: str, kind: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"id": item_id, "kind": kind, "data": {k: v for k, v in data.items() if v is not None}}


def postgres_plan_items(root: PlanNode) -> list[dict[str, Any]]:
    """One item per plan node, with the planner's estimates and redacted conditions."""
    return [
        _item(
            f"plan:{node.path}",
            "plan_node",
            {
                "node_type": node.node_type,
                "relation": node.relation_name,
                "index": node.index_name,
                "estimated_rows": node.plan_rows,
                "total_cost": node.total_cost,
                "filter": redact_sql(node.filter),
                "index_condition": redact_sql(node.index_cond),
                "join_condition": redact_sql(node.join_cond),
                "sort_key": [redact_sql(k) for k in node.sort_key] or None,
                "children": [f"plan:{c.path}" for c in node.children] or None,
            },
        )
        for node in walk(root)
    ]


def mongo_plan_items(root: MongoPlanStage) -> list[dict[str, Any]]:
    """One item per winning-plan stage (already redacted when it was stored)."""
    return [
        _item(
            f"plan:{stage.path}",
            "plan_stage",
            {
                "stage": stage.stage,
                "index": stage.index_name,
                "key_pattern": stage.key_pattern,
                "filter": stage.filter,
                "index_bounds": stage.index_bounds or None,
                "scans_whole_index": stage.full_index_scan if stage.stage == "IXSCAN" else None,
                "sort_pattern": stage.sort_pattern,
                "children": [f"plan:{c.path}" for c in stage.children] or None,
            },
        )
        for stage in mongo_plan.walk(root)
    ]


def assemble(
    *,
    finding: Finding,
    engine: str,
    server_version: str | None,
    operation: dict[str, Any] | None,
    plan_items: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> Bundle:
    """The bundle from already-loaded pieces (pure, so it's easy to test)."""
    rule = RULES_BY_ID.get(finding.rule_id)
    items = [
        _item(
            "rule",
            "rule",
            {
                "rule_id": finding.rule_id,
                "title": rule.title if rule else finding.rule_id,
                "description": rule.description if rule else None,
                "thresholds": rule.thresholds if rule else None,
            },
        ),
        _item(
            "finding",
            "finding",
            {
                # Titles name the table or collection only. The free-text summary and suggestion
                # aren't sent: they quote plan filters, which could hold a literal; the same
                # information is in the facts and plan items, redacted and citable.
                "title": redact_sql(finding.title),
                "confidence": finding.confidence,
                "evidence_strength": finding.evidence_strength,
            },
        ),
        _item("source", "source", {"engine": engine, "server_version": server_version}),
    ]
    if operation is not None:
        items.append(_item("operation", "operation", operation))
    for fact in finding.facts:
        value = fact.get("value")
        items.append(
            _item(
                f"fact:{fact.get('key')}",
                "fact",
                {
                    "label": fact.get("label"),
                    "value": redact_text(value) if isinstance(value, str) else value,
                },
            )
        )
    items.extend(plan_items)
    items.extend(relations)
    return Bundle(
        {
            "bundle_version": BUNDLE_VERSION,
            "note": (
                "Query literals are shown as ? or '?'. Plans are estimates and nothing was "
                "executed to produce them."
            ),
            "items": items,
        }
    )


async def build_bundle(session: AsyncSession, finding: Finding) -> Bundle:
    """Load what the finding cites and assemble its bundle."""
    source = await session.get(DataSource, finding.source_id)
    engine = source.engine.value if source else "unknown"
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in finding.evidence:
        by_kind.setdefault(str(item.get("kind")), []).append(item)

    operation: dict[str, Any] | None = None
    fingerprint = (
        await session.get(Fingerprint, finding.fingerprint_id) if finding.fingerprint_id else None
    )
    if fingerprint is not None and source is not None:
        window_item = next(iter(by_kind.get("operation", [])), None)
        if window_item is not None:
            window = Window(
                as_utc(datetime.fromisoformat(str(window_item["start"]))),
                as_utc(datetime.fromisoformat(str(window_item["end"]))),
            )
            summary = await explorer.operation_summary(session, fingerprint, source, window)
            minutes = round((window.end - window.start).total_seconds() / 60)
            operation = {
                "text": fingerprint.query if engine == "mongodb" else redact_sql(fingerprint.query),
                "namespace": fingerprint.namespace,
                "window_minutes": minutes,
                "calls": summary.calls,
                "total_time_ms": round(summary.total_exec_time_ms, 1),
                "mean_time_ms": round(summary.mean_exec_time_ms, 3),
                "share_of_source_time": round(summary.share_of_total_time, 4),
                "rows_or_documents_returned": summary.rows,
                "keys_examined": summary.keys_examined,
                "documents_examined": summary.docs_examined,
            }

    plan_items: list[dict[str, Any]] = []
    plan_item = next(iter(by_kind.get("plan", [])), None)
    plan = await session.get(Plan, plan_item["plan_id"]) if plan_item else None
    if plan is not None and plan.plan is not None:
        try:
            if plan.engine == "mongodb":
                plan_items = mongo_plan_items(mongo_plan.parse_plan(plan.plan))
            else:
                plan_items = postgres_plan_items(parse_plan(plan.plan))
        except (PlanFormatError, mongo_plan.MongoPlanFormatError):
            plan_items = []

    relations: list[dict[str, Any]] = []
    stat_ids = [int(i["relation_stat_id"]) for i in by_kind.get("relation_stat", [])]
    if stat_ids:
        for stat in await session.scalars(
            select(RelationStat).where(RelationStat.id.in_(stat_ids)).order_by(RelationStat.id)
        ):
            name = f"{stat.schema_name}.{stat.index_name or stat.table_name}"
            relations.append(
                _item(
                    f"{stat.kind}:{name}",
                    stat.kind,
                    {
                        "table": f"{stat.schema_name}.{stat.table_name}",
                        "index": stat.index_name,
                        "live_rows": stat.live_rows,
                        "sequential_scans": stat.seq_scan,
                        "index_scans": stat.idx_scan,
                        "size_bytes": stat.size_bytes,
                        "unique": stat.is_unique,
                        "sampled_at": as_utc(stat.captured_at).isoformat(),
                    },
                )
            )

    return assemble(
        finding=finding,
        engine=engine,
        server_version=source.server_version if source else None,
        operation=operation,
        plan_items=plan_items,
        relations=relations,
    )
