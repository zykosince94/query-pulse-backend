from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from query_pulse.accounts.dependencies import SessionDep, Writer, current_user
from query_pulse.db import utcnow
from query_pulse.findings.engine import evidence_available
from query_pulse.findings.models import Finding, FindingStatus
from query_pulse.findings.rules import RULES, RULES_BY_ID
from query_pulse.findings.schemas import (
    EvidenceOut,
    FindingCounts,
    FindingFact,
    FindingList,
    FindingOut,
    FindingStatusName,
    FindingStatusUpdate,
    RuleOut,
)
from query_pulse.sources.models import DataSource, SourceEngine
from query_pulse.workload.models import Fingerprint
from query_pulse.workload.service import as_utc

router = APIRouter(prefix="/api", tags=["findings"], dependencies=[Depends(current_user)])


async def _to_out(
    session: AsyncSession,
    finding: Finding,
    source_name: str,
    engine: SourceEngine,
    query: str | None,
) -> FindingOut:
    rule = RULES_BY_ID.get(finding.rule_id)
    evidence: list[EvidenceOut] = []
    for item in finding.evidence:
        fields: dict[str, Any] = {
            k: item.get(k)
            for k in ("kind", "label", "fingerprint_id", "plan_id", "node_path", "relation_stat_id")
        }
        fields["start"] = item.get("start")
        fields["end"] = item.get("end")
        evidence.append(EvidenceOut(**fields, available=await evidence_available(session, item)))
    return FindingOut(
        id=finding.id,
        source_id=finding.source_id,
        source_name=source_name,
        engine=engine,
        rule_id=finding.rule_id,
        rule_title=rule.title if rule else finding.rule_id,
        target_kind=finding.target_kind,
        target_key=finding.target_key,
        fingerprint_id=finding.fingerprint_id,
        relation=finding.relation,
        query=query,
        title=finding.title,
        summary=finding.summary,
        confidence=finding.confidence,
        evidence_strength=finding.evidence_strength,
        suggested_action=finding.suggested_action,
        action_sql=finding.action_sql,
        facts=[FindingFact(**fact) for fact in finding.facts],
        evidence=evidence,
        status=finding.status,
        status_changed_by=finding.status_changed_by,
        status_changed_at=as_utc(finding.status_changed_at),
        first_seen_at=as_utc(finding.first_seen_at),
        last_seen_at=as_utc(finding.last_seen_at),
        resolved_at=as_utc(finding.resolved_at) if finding.resolved_at else None,
    )


def _with_names() -> Any:
    return (
        select(Finding, DataSource.name, DataSource.engine, Fingerprint.query)
        .join(DataSource, DataSource.id == Finding.source_id)
        .outerjoin(Fingerprint, Fingerprint.id == Finding.fingerprint_id)
    )


_CONFIDENCE_ORDER = case({"high": 0, "medium": 1, "low": 2}, value=Finding.confidence, else_=3)


@router.get("/findings", response_model=FindingList, operation_id="listFindings")
async def list_findings(
    session: SessionDep,
    status_filter: Annotated[
        FindingStatusName | None,
        Query(alias="status", description="Only findings with this status (default: all)"),
    ] = None,
    source_id: Annotated[int | None, Query(ge=1)] = None,
    rule_id: Annotated[str | None, Query(max_length=64)] = None,
    operation_id: Annotated[
        int | None, Query(ge=1, description="Only findings about this operation")
    ] = None,
) -> FindingList:
    """Findings, most confident and most recently seen first, with counts per status."""
    filters = []
    if source_id is not None:
        filters.append(Finding.source_id == source_id)
    if rule_id is not None:
        filters.append(Finding.rule_id == rule_id)
    if operation_id is not None:
        filters.append(Finding.fingerprint_id == operation_id)

    counts = dict(
        (row[0], row[1])
        for row in await session.execute(
            select(Finding.status, func.count()).where(*filters).group_by(Finding.status)
        )
    )
    query = _with_names().where(*filters)
    if status_filter is not None:
        query = query.where(Finding.status == status_filter)
    rows = await session.execute(
        query.order_by(_CONFIDENCE_ORDER, Finding.last_seen_at.desc(), Finding.id)
    )
    return FindingList(
        counts=FindingCounts(
            open=counts.get(FindingStatus.OPEN.value, 0),
            resolved=counts.get(FindingStatus.RESOLVED.value, 0),
            dismissed=counts.get(FindingStatus.DISMISSED.value, 0),
        ),
        findings=[await _to_out(session, f, name, engine, q) for f, name, engine, q in rows],
    )


async def _load(
    session: AsyncSession, finding_id: int
) -> tuple[Finding, str, SourceEngine, str | None]:
    row = (await session.execute(_with_names().where(Finding.id == finding_id))).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Finding not found")
    return row[0], row[1], row[2], row[3]


@router.get("/findings/{finding_id}", response_model=FindingOut, operation_id="getFinding")
async def get_finding(finding_id: int, session: SessionDep) -> FindingOut:
    return await _to_out(session, *await _load(session, finding_id))


@router.patch("/findings/{finding_id}", response_model=FindingOut, operation_id="updateFinding")
async def update_finding(
    finding_id: int, payload: FindingStatusUpdate, session: SessionDep, _: Writer
) -> FindingOut:
    """Dismiss, resolve or reopen a finding.

    A dismissed finding stays dismissed however often its rule fires again. A resolved one
    reopens if the problem shows up in evidence collected after it was resolved.
    """
    finding, source_name, engine, query = await _load(session, finding_id)
    if finding.status != payload.status:
        now = utcnow()
        finding.status = payload.status
        finding.status_changed_by = "user"
        finding.status_changed_at = now
        finding.resolved_at = now if payload.status == FindingStatus.RESOLVED.value else None
        await session.commit()
    return await _to_out(session, finding, source_name, engine, query)


@router.get("/rules", response_model=list[RuleOut], operation_id="listRules")
async def list_rules() -> list[RuleOut]:
    """The rule catalogue: what each rule checks, its evidence and its thresholds."""
    return [
        RuleOut(
            id=r.id,
            title=r.title,
            description=r.description,
            evidence_strength=r.evidence_strength,
            confidence=r.confidence,
            thresholds=r.thresholds,
        )
        for r in RULES
    ]
