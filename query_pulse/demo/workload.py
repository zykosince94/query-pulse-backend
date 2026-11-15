"""Run the demo workload forever: planted problems next to healthy control queries.

    python -m query_pulse.demo.workload

Runs as the `shop_app` role (QP_DEMO_WORKLOAD_DSN) at about QP_DEMO_RATE queries per second.
Queries are parameterised, so pg_stat_statements groups each one into a single normalised
statement (`... WHERE customer_id = $1`), exactly like a real application. Ids are always sent
as int8: psycopg otherwise picks int2/int4/int8 by the value's size, and PostgreSQL would count
each parameter type as a different statement.

Each planted problem has a control that should stay healthy:

  P1  orders by customer (no index → sequential scan)   vs  C1  customer by primary key
  P2  latest 20 orders (sort, no index)                  vs  C2  order by primary key
  P3  product name LIKE '%term%' (leading wildcard)       vs  C3  products by category (indexed)
  P4  (nothing queries idx_orders_legacy_lookup)          vs  C4  orders by primary key range
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.numeric import Int8

logger = logging.getLogger(__name__)

DEFAULT_DSN = "postgresql://shop_app:shop-app@demo-postgres:5432/shop"
TERMS = ["lamp", "chair", "kettle", "pack", "phone", "mug", "desk", "jacket", "ultra", "eco"]
CATEGORIES = ["home", "office", "kitchen", "travel", "audio", "apparel"]


@dataclass(frozen=True)
class Query:
    label: str
    sql: str
    params: Callable[[int, int, int], dict[str, Any]]
    weight: int


def queries() -> list[Query]:
    return [
        # P1: the most expensive by total time, so it should rank first (QP-AC-14).
        Query(
            "P1 orders by customer",
            "SELECT id, status, total FROM orders WHERE customer_id = %(cid)s",
            lambda c, p, o: {"cid": Int8(random.randint(1, c))},
            weight=6,
        ),
        Query(
            "P2 latest orders",
            "SELECT id, customer_id, total, created_at FROM orders "
            "ORDER BY created_at DESC LIMIT 20",
            lambda c, p, o: {},
            weight=2,
        ),
        Query(
            "P3 product search",
            "SELECT id, name, price FROM products WHERE name LIKE %(pattern)s LIMIT 50",
            lambda c, p, o: {"pattern": f"%{random.choice(TERMS)}%"},
            weight=3,
        ),
        Query(
            "C1 customer by id",
            "SELECT id, email, name FROM customers WHERE id = %(cid)s",
            lambda c, p, o: {"cid": Int8(random.randint(1, c))},
            weight=10,
        ),
        Query(
            "C2 order by id",
            "SELECT id, customer_id, status, total FROM orders WHERE id = %(oid)s",
            lambda c, p, o: {"oid": Int8(random.randint(1, o))},
            weight=10,
        ),
        Query(
            "C3 products by category",
            "SELECT id, name, price FROM products WHERE category = %(cat)s LIMIT 20",
            lambda c, p, o: {"cat": random.choice(CATEGORIES)},
            weight=6,
        ),
        Query(
            "C4 order range",
            "SELECT count(*) FROM orders WHERE id BETWEEN %(lo)s AND %(lo)s + 100",
            lambda c, p, o: {"lo": Int8(random.randint(1, max(1, o - 100)))},
            weight=4,
        ),
    ]


async def run(dsn: str, rate: float, duration: float | None = None) -> int:
    """Execute weighted random queries at `rate` per second. Returns how many ran."""
    workload = queries()
    weights = [q.weight for q in workload]
    executed = 0
    started = time.monotonic()
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        counts = await (
            await conn.execute(
                "SELECT (SELECT max(id) FROM customers), (SELECT max(id) FROM products), "
                "(SELECT max(id) FROM orders)"
            )
        ).fetchone()
        assert counts is not None  # noqa: S101
        customers, products, orders = (int(v or 1) for v in counts)
        interval = 1.0 / rate
        while duration is None or time.monotonic() - started < duration:
            query = random.choices(workload, weights)[0]
            tick = time.monotonic()
            await conn.execute(query.sql, query.params(customers, products, orders))
            executed += 1
            if executed % 500 == 0:
                logger.info("workload: %d queries executed", executed)
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - tick)))
    return executed


async def run_forever(dsn: str, rate: float) -> None:
    """Keep the workload going, reconnecting if the database restarts."""
    while True:
        try:
            await run(dsn, rate)
        except (psycopg.Error, OSError) as exc:
            logger.warning("workload interrupted (%s); retrying in 5s", type(exc).__name__)
            await asyncio.sleep(5)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    rate = float(os.environ.get("QP_DEMO_RATE", "20"))
    asyncio.run(run_forever(os.environ.get("QP_DEMO_WORKLOAD_DSN", DEFAULT_DSN), rate))


if __name__ == "__main__":
    main()
