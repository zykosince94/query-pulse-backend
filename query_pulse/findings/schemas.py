"""API shapes for findings and the rule catalogue."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from query_pulse.sources.models import SourceEngine

FindingStatusName = Literal["open", "resolved", "dismissed"]


class FindingFact(BaseModel):
    """One number or value behind a finding, kept with it so it stays readable after the
    underlying observations age out."""

    key: str
    label: str
    value: float | int | str | bool | None


class EvidenceOut(BaseModel):
    """A link to the data a finding cites.

    - operation: observations of `fingerprint_id` between `start` and `end`
    - plan: plan `plan_id` of `fingerprint_id`, pointing at node `node_path`
    - relation_stat: table or index sample `relation_stat_id`

    `available` is false once retention has deleted what it points at.
    """

    kind: Literal["operation", "plan", "relation_stat"]
    label: str
    available: bool
    fingerprint_id: int | None = None
    plan_id: int | None = None
    node_path: str | None = None
    relation_stat_id: int | None = None
    start: datetime | None = None
    end: datetime | None = None


class FindingOut(BaseModel):
    id: int
    source_id: int
    source_name: str
    engine: SourceEngine
    rule_id: str
    rule_title: str
    target_kind: Literal["operation", "index"]
    target_key: str
    fingerprint_id: int | None
    relation: str | None
    # The statement, when the target is an operation.
    query: str | None
    title: str
    summary: str
    confidence: Literal["high", "medium", "low"]
    # "plan": strong evidence from a captured plan; "statistics": a heuristic from counters.
    evidence_strength: Literal["plan", "statistics"]
    suggested_action: str
    # To review and run by hand: SQL for PostgreSQL, a mongosh command for MongoDB. Query
    # Pulse never executes it.
    action_sql: str | None
    facts: list[FindingFact]
    evidence: list[EvidenceOut]
    status: FindingStatusName
    status_changed_by: Literal["rules", "user"]
    status_changed_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None


class FindingStatusUpdate(BaseModel):
    status: FindingStatusName


class FindingCounts(BaseModel):
    open: int
    resolved: int
    dismissed: int


class FindingList(BaseModel):
    counts: FindingCounts
    findings: list[FindingOut]


class RuleOut(BaseModel):
    id: str
    title: str
    description: str
    evidence_strength: Literal["plan", "statistics"]
    confidence: Literal["high", "medium", "low"]
    thresholds: dict[str, float]
