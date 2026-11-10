from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from services.sources import SourceDefinition, SourceType, SOURCE_REGISTRY


def build_demo_workload_snapshot(source_id: str) -> dict[str, Any]:
    source = SOURCE_REGISTRY.get(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    if source.type == SourceType.POSTGRES:
        observations = [
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
        summary = {
            "total_queries": 2,
            "avg_duration_ms": 114.45,
            "slowest_query_ms": 142.6,
            "read_only": True,
        }
    else:
        observations = [
            {
                "statement": "db.orders.find({ customerId: ObjectId('...') })",
                "duration_ms": 116.8,
                "documents_examined": 731,
                "documents_returned": 18,
                "execution_plan": "COLLSCAN with filter on customerId",
            },
            {
                "statement": "db.invoices.find({ status: 'pending' }, { _id: 1, status: 1 })",
                "duration_ms": 83.7,
                "documents_examined": 224,
                "documents_returned": 28,
                "execution_plan": "IXSCAN on status index",
            },
        ]
        summary = {
            "total_queries": 2,
            "avg_duration_ms": 100.25,
            "slowest_query_ms": 116.8,
            "read_only": True,
        }

    return {
        "source_id": source.id,
        "source_name": source.name,
        "engine": source.type.value,
        "read_only": True,
        "status": "ok",
        "summary": summary,
        "observations": observations,
        "notes": "Demo workload preview generated without mutating database state.",
    }


def collector_status() -> dict[str, Any]:
    return {
        "status": "ready",
        "read_only": True,
        "source_count": len(SOURCE_REGISTRY),
        "engines": sorted({source.type.value for source in SOURCE_REGISTRY.values()}),
        "message": "Collector is ready to inspect workload shapes in read-only mode.",
    }
