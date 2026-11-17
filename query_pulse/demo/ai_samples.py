"""Sample AI answers for the demo's planted problems (not product code).

A fresh demo stack has AI off and no key, so "Try the demo" would only ever say "AI is off".
These answers, written in advance for P1-P4 and M1-M3, let a visitor see what the assistant
does without anyone paying for a request.

They are stored like real answers, but with the provider "sample", so the UI labels them as
written in advance and never passes them off as a live model's work. Each one goes through the
same checks as a provider's answer (every citation must be an id in the finding's evidence
bundle; no destructive statements) before it is stored, and claims avoid numbers that drift
from cycle to cycle: they cite the facts instead.

`attach_samples` finds the planted problems' findings and makes sure each finding and kind has
an answer for its current evidence. The demo-workload service runs it every 15 seconds, so the
samples appear once the rules raise the findings.

There is one sample row per finding and kind. The evidence hash rounds the live numbers to two
significant figures, and counts such as calls cross those boundaries every few minutes. A new
row each time would pile up thousands a day, so the existing sample is re-pointed at the new
evidence instead, after passing the checks again (the samples cite fact ids, never their values,
so the text stays true). An answer someone asked a real provider for is never touched: samples
only fill gaps.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.ai.answers import Kind, check
from query_pulse.ai.evidence import build_bundle
from query_pulse.ai.models import AIAnswer
from query_pulse.db import create_engine, create_sessionmaker, utcnow
from query_pulse.findings.models import Finding

logger = logging.getLogger(__name__)

SAMPLE_PROVIDER = "sample"
SAMPLE_MODEL = "written in advance"
INTERVAL_SECONDS = 15


def _c(text: str, *evidence: str) -> dict[str, Any]:
    return {"text": text, "evidence": list(evidence)}


# rule id -> kind -> the answer, in the shape a provider must return (answers.RawExplanation /
# answers.RawDraft). Citation ids match the bundles in evals/bundles/, which the tests check.
SAMPLES: dict[str, dict[Kind, dict[str, Any]]] = {
    # P1: SELECT ... FROM orders WHERE customer_id = $1, with no index on customer_id.
    "PG-SEQSCAN-HOT": {
        "explain": {
            "summary": _c(
                "This lookup by customer reads the whole orders table every time it runs, and it "
                "is the most expensive statement on this source.",
                "finding",
                "plan:0",
                "fact:share_of_total_time",
            ),
            "claims": [
                _c(
                    "The plan is a sequential scan on orders with the filter customer_id = $1, so "
                    "PostgreSQL checks every row to find the few that match.",
                    "plan:0",
                ),
                _c(
                    "The table has about 200,000 rows, but the planner expects the filter to keep "
                    "only around 10 of them: a tiny fraction.",
                    "fact:table_rows",
                    "fact:estimated_rows",
                    "fact:selectivity",
                ),
                _c(
                    "It runs often and takes the largest share of this source's execution time, "
                    "so the wasted reads add up.",
                    "fact:calls",
                    "fact:share_of_total_time",
                    "operation",
                ),
                _c(
                    "The rule fires because the share, the call count, the table size and the "
                    "selectivity are all past its thresholds.",
                    "rule",
                ),
            ],
            "caveats": [
                "Plan costs and row counts are the planner's estimates; nothing was executed.",
                "The customer value is redacted, so the advice holds for typical lookups.",
            ],
        },
        "draft": {
            "summary": _c(
                "Add an index on orders (customer_id) so each lookup reads only the matching rows.",
                "plan:0",
                "table:public.orders",
            ),
            "language": "sql",
            "code": "CREATE INDEX CONCURRENTLY ON public.orders (customer_id);",
            "claims": [
                _c(
                    "With the index, the plan should switch from a sequential scan to an index "
                    "scan on customer_id; check it on the operation page afterwards.",
                    "plan:0",
                ),
                _c(
                    "CONCURRENTLY builds the index without blocking writes to orders, but it takes "
                    "longer and can't run inside a transaction.",
                    "table:public.orders",
                ),
                _c(
                    "The index costs disk space and a little extra work on every insert and "
                    "update to orders.",
                    "table:public.orders",
                ),
            ],
        },
    },
    # P2: the latest orders, ORDER BY created_at DESC LIMIT $1, with no index on created_at.
    "PG-SORT-NO-INDEX": {
        "explain": {
            "summary": _c(
                "To return the newest orders, PostgreSQL reads and sorts the whole orders table "
                "on every call, then keeps only the first few rows.",
                "finding",
                "plan:0.0.0",
            ),
            "claims": [
                _c(
                    "The plan is a Limit over a Sort on created_at DESC, fed by a sequential scan "
                    "of orders.",
                    "plan:0",
                    "plan:0.0.0",
                    "plan:0.0.0.0",
                ),
                _c(
                    "Sorting about 200,000 rows to keep a handful is most of the work.",
                    "fact:table_rows",
                    "fact:sort_key",
                ),
                _c(
                    "The statement runs often enough to be one of this source's most expensive.",
                    "fact:calls",
                    "fact:total_exec_time_ms",
                    "operation",
                ),
            ],
            "caveats": ["Plan costs are estimates; nothing was executed."],
        },
        "draft": {
            "summary": _c(
                "Index orders by created_at descending, so the newest rows can be read in order "
                "and the sort disappears.",
                "plan:0.0.0",
                "fact:sort_key",
            ),
            "language": "sql",
            "code": "CREATE INDEX CONCURRENTLY ON public.orders (created_at DESC);",
            "claims": [
                _c(
                    "With the index, the plan should become a Limit over an index scan, reading "
                    "only as many rows as the limit asks for.",
                    "plan:0",
                ),
                _c(
                    "CONCURRENTLY avoids blocking writes while the index builds; it is slower and "
                    "can't run inside a transaction.",
                    "table:public.orders",
                ),
                _c(
                    "Every insert into orders will also update this index.",
                    "table:public.orders",
                ),
            ],
        },
    },
    # P3: SELECT ... FROM products WHERE name LIKE $1, searched with a leading wildcard.
    "PG-LEADING-WILDCARD": {
        "explain": {
            "summary": _c(
                "This product search scans the whole products table, which is what a LIKE "
                "pattern starting with % forces.",
                "finding",
                "plan:0.0",
            ),
            "claims": [
                _c(
                    "The plan is a sequential scan on products with the filter name ~~ $1, the "
                    "planner's form of LIKE.",
                    "plan:0.0",
                ),
                _c(
                    "The pattern is a parameter, so its value isn't recorded; the rule suspects a "
                    "leading wildcard from how the statement behaves, not from the value.",
                    "fact:pattern",
                    "fact:pattern_visible",
                    "rule",
                ),
                _c(
                    "A normal B-tree index can't help a pattern that starts with %, because the "
                    "start of the text is unknown.",
                    "fact:pattern",
                ),
            ],
            "caveats": [
                "Confidence is medium: the pattern itself is redacted.",
                "The table is small today, so each call is cheap; the cost grows with the table.",
            ],
        },
        "draft": {
            "summary": _c(
                "Add a trigram index on products (name), which can serve LIKE patterns with a "
                "leading wildcard.",
                "plan:0.0",
                "table:public.products",
            ),
            "language": "sql",
            "code": (
                "CREATE EXTENSION IF NOT EXISTS pg_trgm;\n"
                "CREATE INDEX CONCURRENTLY ON public.products USING gin (name gin_trgm_ops);"
            ),
            "claims": [
                _c(
                    "pg_trgm splits text into three-character pieces, so a GIN index on them can "
                    "find '%term%' matches without reading every row.",
                    "fact:pattern",
                ),
                _c(
                    "Creating the extension needs the right privilege, and a trigram index is "
                    "larger and slower to update than a B-tree.",
                    "table:public.products",
                ),
                _c(
                    "If searches only ever match the start of the name, an anchored pattern "
                    "('term%') with a plain index is a cheaper fix.",
                    "plan:0.0",
                ),
            ],
        },
    },
    # P4: idx_orders_legacy_lookup, an index nothing uses.
    "PG-UNUSED-INDEX": {
        "explain": {
            "summary": _c(
                "The index idx_orders_legacy_lookup hasn't been used once while Query Pulse was "
                "watching, yet every write to orders still has to maintain it.",
                "finding",
                "fact:idx_scan",
            ),
            "claims": [
                _c(
                    "Its scan counter stayed at zero across the samples taken.",
                    "fact:idx_scan",
                    "fact:samples",
                    "fact:observed_minutes",
                ),
                _c(
                    "It takes several megabytes of disk and memory for no benefit.",
                    "fact:size_bytes",
                ),
                _c(
                    "This is a statistics heuristic: an index used only by a monthly report, or "
                    "only on a replica, would look the same.",
                    "rule",
                ),
            ],
            "caveats": [
                "Check other replicas and rare jobs before dropping it.",
                "Scan counters reset when statistics are reset.",
            ],
        },
        "draft": {
            "summary": _c(
                "Drop the unused index, after checking that nothing rare depends on it.",
                "fact:idx_scan",
            ),
            "language": "sql",
            "code": "DROP INDEX CONCURRENTLY public.idx_orders_legacy_lookup;",
            "claims": [
                _c(
                    "Dropping it saves its disk space and the work of updating it on every write.",
                    "fact:size_bytes",
                ),
                _c(
                    "CONCURRENTLY avoids blocking queries on orders while it is removed.",
                    "fact:idx_scan",
                ),
                _c(
                    "Keep its definition so it can be recreated if a rare job turns out to need it.",
                    "rule",
                ),
            ],
        },
    },
    # M1: orders found by customerId, with no index on customerId.
    "MDB-COLLSCAN": {
        "explain": {
            "summary": _c(
                "Finding orders by customerId reads every document in the collection, because no "
                "index covers customerId.",
                "finding",
                "plan:0",
            ),
            "claims": [
                _c(
                    "The plan is a COLLSCAN with the filter customerId = ?, a full collection "
                    "scan.",
                    "plan:0",
                ),
                _c(
                    "It examines thousands of documents for every one it returns, and uses no "
                    "index keys at all.",
                    "fact:examined_per_returned",
                    "fact:keys_examined",
                ),
                _c(
                    "It runs often, so this is where most of the source's time goes.",
                    "fact:calls",
                    "fact:total_exec_time_ms",
                    "operation",
                ),
            ],
            "caveats": ["The customerId value is redacted; the plan is the same for any value."],
        },
        "draft": {
            "summary": _c(
                "Index orders on customerId, so each lookup reads only that customer's orders.",
                "plan:0",
            ),
            "language": "mongosh",
            "code": 'db.getSiblingDB("shop").getCollection("orders").createIndex({ "customerId": 1 })',
            "claims": [
                _c(
                    "The plan should become an IXSCAN on customerId_1 followed by a FETCH, with "
                    "documents examined close to documents returned.",
                    "plan:0",
                    "fact:examined_per_returned",
                ),
                _c(
                    "Building the index takes time and memory on a large collection; newer "
                    "MongoDB versions don't block other operations while it builds.",
                    "operation",
                ),
                _c(
                    "Every insert and update to orders will also maintain the index.",
                    "operation",
                ),
            ],
        },
    },
    # M2: the latest orders, sorted by placedAt with no index on it.
    "MDB-BLOCKING-SORT": {
        "explain": {
            "summary": _c(
                "To return the newest orders, MongoDB scans the whole collection and sorts it in "
                "memory on every call.",
                "finding",
                "plan:0",
            ),
            "claims": [
                _c(
                    "The plan is a SORT stage over a COLLSCAN: no index provides the placedAt "
                    "order.",
                    "plan:0",
                    "plan:0.0",
                ),
                _c(
                    "It examines far more documents than it returns.",
                    "fact:docs_examined",
                    "fact:returned",
                ),
                _c(
                    "An in-memory sort is capped; on a larger collection it would spill to disk "
                    "or fail.",
                    "fact:sort_pattern",
                ),
            ],
            "caveats": ["The limit value is redacted."],
        },
        "draft": {
            "summary": _c(
                "Index orders on placedAt descending, so the newest documents are read in order "
                "and the sort goes away.",
                "plan:0",
                "fact:sort_pattern",
            ),
            "language": "mongosh",
            "code": 'db.getSiblingDB("shop").getCollection("orders").createIndex({ "placedAt": -1 })',
            "claims": [
                _c(
                    "The plan should become an IXSCAN on placedAt_-1 under the limit, with no "
                    "SORT stage.",
                    "plan:0",
                    "plan:0.0",
                ),
                _c(
                    "Writes to orders will also maintain this index.",
                    "operation",
                ),
            ],
        },
    },
    # M3: products found with an unanchored $regex on sku.
    "MDB-EXAMINED-RATIO": {
        "explain": {
            "summary": _c(
                "This sku search uses the sku index but still walks all of it, because the "
                "regular expression isn't anchored to the start of the value.",
                "finding",
                "plan:0.0",
            ),
            "claims": [
                _c(
                    "The plan is an IXSCAN on sku_1 whose bounds cover the whole index, then a "
                    "FETCH.",
                    "plan:0.0",
                    "plan:0",
                    "fact:full_index_scan",
                ),
                _c(
                    "It examines thousands of index keys for each document it returns.",
                    "fact:keys_examined",
                    "fact:examined_per_returned",
                ),
                _c(
                    "The shape uses $regex; only a pattern starting with ^ lets MongoDB narrow the "
                    "index bounds.",
                    "fact:regex",
                ),
            ],
            "caveats": [
                "The pattern is redacted, so the advice assumes it doesn't start with ^.",
            ],
        },
        "draft": {
            "summary": _c(
                "Anchor the pattern to the start of the sku, so the existing index is searched "
                "by prefix instead of read end to end.",
                "plan:0.0",
                "fact:regex",
            ),
            "language": "mongosh",
            "code": (
                'db.getSiblingDB("shop").getCollection("products")'
                '.find({ "sku": { "$regex": "^SKU-12" } })'
            ),
            "claims": [
                _c(
                    "With a ^ prefix, the IXSCAN bounds shrink to the matching range of sku_1.",
                    "plan:0.0",
                ),
                _c(
                    "This changes what the search matches: it finds skus that start with the "
                    "text, not ones that contain it anywhere.",
                    "fact:regex",
                ),
                _c(
                    "If substring search is a real need, a search index is the better tool.",
                    "rule",
                ),
            ],
        },
    },
}


async def attach_samples(session: AsyncSession) -> int:
    """Give each planted finding and kind a sample for its current evidence, if it has no answer.

    Returns how many samples were stored or re-pointed. A sample that fails the checks is logged
    and skipped, never stored (the tests keep that from happening).
    """
    findings = (await session.scalars(select(Finding).where(Finding.rule_id.in_(SAMPLES)))).all()
    stored = 0
    for finding in findings:
        bundle = await build_bundle(session, finding)
        for kind, raw in SAMPLES[finding.rule_id].items():
            answered = await session.scalar(
                select(AIAnswer.id)
                .where(
                    AIAnswer.finding_id == finding.id,
                    AIAnswer.kind == kind,
                    AIAnswer.evidence_hash == bundle.hash,
                )
                .limit(1)
            )
            if answered is not None:
                continue
            checked = check(kind, raw, bundle)
            if checked.status != "ok":
                logger.warning(
                    "demo sample rejected: rule=%s kind=%s error=%s",
                    finding.rule_id,
                    kind,
                    checked.error,
                )
                continue
            sample = await session.scalar(
                select(AIAnswer)
                .where(
                    AIAnswer.finding_id == finding.id,
                    AIAnswer.kind == kind,
                    AIAnswer.provider == SAMPLE_PROVIDER,
                )
                .order_by(AIAnswer.id.desc())
                .limit(1)
            )
            if sample is not None:
                sample.evidence_hash = bundle.hash
                sample.answer = checked.model_dump()
            else:
                session.add(
                    AIAnswer(
                        finding_id=finding.id,
                        kind=kind,
                        evidence_hash=bundle.hash,
                        provider=SAMPLE_PROVIDER,
                        model=SAMPLE_MODEL,
                        answer=checked.model_dump(),
                        created_at=utcnow(),
                    )
                )
            stored += 1
    await session.commit()
    return stored


async def run_forever(
    maker: async_sessionmaker[AsyncSession], interval: float = INTERVAL_SECONDS
) -> None:
    """Keep the samples attached. The app database may not be ready yet: just try again."""
    while True:
        try:
            async with maker() as session:
                stored = await attach_samples(session)
            if stored:
                logger.info("demo samples: stored or refreshed %s sample AI answers", stored)
        except (SQLAlchemyError, OSError) as exc:
            logger.warning("demo samples: app database not ready (%s)", type(exc).__name__)
        await asyncio.sleep(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    url = os.environ["QP_DEMO_APP_DATABASE_URL"]
    asyncio.run(run_forever(create_sessionmaker(create_engine(url))))


if __name__ == "__main__":
    main()
