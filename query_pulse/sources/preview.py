"""Canned workload preview (placeholder until the Phase 3 collector produces real observations)."""

from __future__ import annotations

from typing import Any

from query_pulse.sources.schemas import SourceOut, SourceType

_POSTGRES_OBSERVATIONS: list[dict[str, Any]] = [
    {
        "statement": "SELECT * FROM orders WHERE customer_id = $1",
        "duration_ms": 142.6,
        "rows_examined": 842,
        "shared_blks_hit": 410,
        "shared_blks_read": 14,
        "query_plan": "Seq Scan on orders",
    },
    {
        "statement": "SELECT id, status FROM invoices WHERE created_at > $1",
        "duration_ms": 86.3,
        "rows_examined": 242,
        "shared_blks_hit": 198,
        "shared_blks_read": 9,
        "query_plan": "Index Scan using idx_invoices_created_at",
    },
]

_MONGODB_OBSERVATIONS: list[dict[str, Any]] = [
    {
        "statement": "db.orders.find({ customerId: ? })",
        "duration_ms": 116.8,
        "documents_examined": 731,
        "documents_returned": 18,
        "execution_plan": "COLLSCAN with filter on customerId",
    },
    {
        "statement": "db.invoices.find({ status: ? }, { _id: 1, status: 1 })",
        "duration_ms": 83.7,
        "documents_examined": 224,
        "documents_returned": 28,
        "execution_plan": "IXSCAN on status index",
    },
]


def demo_workload_preview(source: SourceOut) -> dict[str, Any]:
    observations = (
        _POSTGRES_OBSERVATIONS if source.type is SourceType.POSTGRES else _MONGODB_OBSERVATIONS
    )
    durations = [o["duration_ms"] for o in observations]
    return {
        "source_id": source.id,
        "source_name": source.name,
        "engine": source.type.value,
        "read_only": True,
        "status": "ok",
        "summary": {
            "total_queries": len(observations),
            "avg_duration_ms": round(sum(durations) / len(durations), 2),
            "slowest_query_ms": max(durations),
            "read_only": True,
        },
        "observations": observations,
        "notes": "Canned demo data; real collection arrives with the Phase 3 collector.",
    }
