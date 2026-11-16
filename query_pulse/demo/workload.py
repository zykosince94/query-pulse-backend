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

MongoDB (Phase 6), at about QP_DEMO_MONGO_RATE operations per second on QP_DEMO_MONGO_URL:

  M1  orders by customerId (no index → COLLSCAN)          vs  MC1 order by _id (IDHACK)
                                                          vs  MC2 order by orderNo (indexed)
  M2  latest 20 orders by placedAt (in-memory SORT)       vs  MC3 latest 20 by _id (index order)
  M3  products by unanchored sku $regex (whole index)     vs  MC4 products by ^SKU- prefix
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
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)

DEFAULT_DSN = "postgresql://shop_app:shop-app@demo-postgres:5432/shop"
DEFAULT_MONGO_URL = "mongodb://mongo:27017/?directConnection=true"
TERMS = ["lamp", "chair", "kettle", "pack", "phone", "mug", "desk", "jacket", "ultra", "eco"]
CATEGORIES = [f"category-{n}" for n in range(1, 41)]  # matches the seed


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


@dataclass(frozen=True)
class MongoOperation:
    label: str
    run: Callable[[Any, dict[str, Any]], Any]  # (database, context) -> awaitable
    weight: int


def mongo_operations() -> list[MongoOperation]:
    def m1(db: Any, c: dict[str, Any]) -> Any:
        return db.orders.find({"customerId": random.randint(1, c["customers"])}).to_list()

    def m2(db: Any, c: dict[str, Any]) -> Any:
        return db.orders.find({}).sort("placedAt", -1).limit(20).to_list()

    def m3(db: Any, c: dict[str, Any]) -> Any:
        return db.products.find({"sku": {"$regex": f"{random.randint(0, 9999):04d}"}}).to_list()

    def mc1(db: Any, c: dict[str, Any]) -> Any:
        return db.orders.find_one({"_id": random.choice(c["ids"])})

    def mc2(db: Any, c: dict[str, Any]) -> Any:
        return db.orders.find_one({"orderNo": random.randint(1, c["orders"])})

    def mc3(db: Any, c: dict[str, Any]) -> Any:
        return db.orders.find({}).sort("_id", -1).limit(20).to_list()

    def mc4(db: Any, c: dict[str, Any]) -> Any:
        prefix = f"^SKU-0{random.randint(0, 199):03d}"
        return db.products.find({"sku": {"$regex": prefix}}).limit(10).to_list()

    return [
        MongoOperation("M1 orders by customerId", m1, 4),
        MongoOperation("M2 latest orders", m2, 2),
        MongoOperation("M3 sku regex", m3, 2),
        MongoOperation("MC1 order by _id", mc1, 6),
        MongoOperation("MC2 order by orderNo", mc2, 6),
        MongoOperation("MC3 latest by _id", mc3, 3),
        MongoOperation("MC4 sku prefix", mc4, 3),
    ]


async def run_mongo(url: str, rate: float, duration: float | None = None) -> int:
    """Run the MongoDB operations at `rate` per second. Returns how many ran."""
    operations = mongo_operations()
    weights = [o.weight for o in operations]
    executed = 0
    started = time.monotonic()
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(url, appname="shop-app")
    try:
        db = client.shop
        ids = [d["_id"] async for d in db.orders.find({}, {"_id": 1}).limit(1000)]
        context = {
            "ids": ids or [None],
            "orders": await db.orders.estimated_document_count() or 1,
            "customers": int(os.environ.get("QP_DEMO_CUSTOMERS", "20000")),
        }
        interval = 1.0 / rate
        while duration is None or time.monotonic() - started < duration:
            operation = random.choices(operations, weights)[0]
            tick = time.monotonic()
            await operation.run(db, context)
            executed += 1
            if executed % 500 == 0:
                logger.info("workload: %d mongodb operations executed", executed)
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - tick)))
    finally:
        await client.close()
    return executed


async def run_mongo_forever(url: str, rate: float) -> None:
    while True:
        try:
            await run_mongo(url, rate)
        except (PyMongoError, OSError) as exc:
            logger.warning("mongodb workload interrupted (%s); retrying in 5s", type(exc).__name__)
            await asyncio.sleep(5)


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
    mongo_url = os.environ.get("QP_DEMO_MONGO_URL", DEFAULT_MONGO_URL)
    mongo_rate = float(os.environ.get("QP_DEMO_MONGO_RATE", "10"))

    async def both() -> None:
        tasks = [run_forever(os.environ.get("QP_DEMO_WORKLOAD_DSN", DEFAULT_DSN), rate)]
        if mongo_url:
            tasks.append(run_mongo_forever(mongo_url, mongo_rate))
        await asyncio.gather(*tasks)

    asyncio.run(both())


if __name__ == "__main__":
    main()
