"""Create and fill the demo e-commerce data, PostgreSQL and MongoDB. Safe to run repeatedly.

    python -m query_pulse.demo.seed

PostgreSQL: connects as a superuser (QP_DEMO_DSN, default: the compose demo database) and:

- creates customers, products and orders with the planted problems built in;
- fills them with generated rows (sizes from QP_DEMO_CUSTOMERS / _PRODUCTS / _ORDERS);
- creates the `shop_app` role the workload runs as;
- grants the monitoring role SELECT, so query plans work in Phase 4.

Planted problems (each will be paired with a healthy control in the workload):

  P1  orders.customer_id has no index      → lookups by customer scan every order
  P2  no index on orders.created_at         → "latest orders" sorts the whole table
  P3  products searched with LIKE '%term%'  → a leading wildcard can't use a b-tree index
  P4  idx_orders_legacy_lookup is never used → a large index that only costs writes

MongoDB (QP_DEMO_MONGO_URL; Phase 6): fills `shop.orders` and `shop.products` and turns the
profiler on for `shop`, the way a user would before adding the source (Query Pulse itself
never enables it):

  M1  orders.customerId has no index       → lookups by customer scan the collection
  M2  no index on orders.placedAt          → "latest orders" sorts in memory
  M3  products.sku searched with an unanchored $regex → the sku index is scanned end to end

The demo profiles every operation (level 1, slowms 0) so the healthy controls show up in the
explorer too; a real deployment would keep slowms at its default (100 ms) or higher.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime

import psycopg
from pymongo import ASCENDING, MongoClient

logger = logging.getLogger(__name__)

DEFAULT_DSN = "postgresql://postgres:postgres@demo-postgres:5432/shop"
DEFAULT_MONGO_URL = "mongodb://mongo:27017/?directConnection=true"
MONGO_DATABASE = "shop"
# system.profile is capped (1 MB by default, a few seconds of a busy demo). 64 MB holds
# several collection cycles even at a high QP_DEMO_MONGO_RATE.
PROFILE_SIZE_BYTES = 64 * 1024 * 1024
STATUSES = ["pending", "paid", "shipped", "delivered", "cancelled"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id          bigserial PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    name        text NOT NULL,
    country     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS products (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    category    text NOT NULL,
    price       numeric(10, 2) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- Control for P3: category lookups use this index. With 40 categories each one is about
-- 2.5% of products, selective enough that the planner prefers the index (with 6 it was
-- 17%, and a sequential scan was rightly cheaper, leaving this index unused).
CREATE INDEX IF NOT EXISTS idx_products_category ON products (category);

CREATE TABLE IF NOT EXISTS orders (
    id           bigserial PRIMARY KEY,
    customer_id  bigint NOT NULL REFERENCES customers (id),  -- P1: deliberately no index
    product_id   bigint NOT NULL REFERENCES products (id),
    status       text NOT NULL,
    total        numeric(12, 2) NOT NULL,
    created_at   timestamptz NOT NULL                        -- P2: deliberately no index
);
-- P4: a large index no query uses.
CREATE INDEX IF NOT EXISTS idx_orders_legacy_lookup ON orders (total, status);
"""

FILL_CUSTOMERS = """
INSERT INTO customers (email, name, country, created_at)
SELECT 'customer' || g || '@example.com',
       'Customer ' || g,
       (ARRAY['US', 'GB', 'DE', 'IN', 'BR', 'JP', 'FR', 'CA'])[1 + mod(g, 8)],
       now() - mod(g, 730) * interval '1 day'
FROM generate_series(1, %(n)s) AS g
"""

FILL_PRODUCTS = """
INSERT INTO products (name, category, price, created_at)
SELECT (ARRAY['Classic', 'Ultra', 'Mini', 'Pro', 'Eco', 'Smart'])[1 + mod(g, 6)] || ' '
       || (ARRAY['Lamp', 'Chair', 'Kettle', 'Backpack', 'Headphones', 'Mug', 'Desk', 'Jacket'])[1 + mod(g, 8)]
       || ' ' || g,
       'category-' || (1 + mod(g, 40)),
       round((5 + random() * 495)::numeric, 2),
       now() - mod(g, 365) * interval '1 day'
FROM generate_series(1, %(n)s) AS g
"""

FILL_ORDERS = """
INSERT INTO orders (customer_id, product_id, status, total, created_at)
SELECT 1 + (random() * (%(customers)s - 1))::bigint,
       1 + (random() * (%(products)s - 1))::bigint,
       (ARRAY['pending', 'paid', 'shipped', 'delivered', 'cancelled'])[1 + (random() * 4)::int],
       round((10 + random() * 990)::numeric, 2),
       now() - random() * interval '730 days'
FROM generate_series(1, %(n)s) AS g
"""

ROLES = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'shop_app') THEN
    CREATE ROLE shop_app LOGIN PASSWORD 'shop-app';
  END IF;
END
$$;
GRANT CONNECT ON DATABASE shop TO shop_app;
GRANT USAGE ON SCHEMA public TO shop_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO shop_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO shop_app;
-- The monitoring role needs SELECT for EXPLAIN (GENERIC_PLAN) in Phase 4.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO querypulse_monitor;
"""


def _size(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def seed(dsn: str, customers: int, products: int, orders: int) -> dict[str, int]:
    """Create the schema and fill empty tables. Returns the row count per table."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SCHEMA)
        counts = {}
        for table, query, params in (
            ("customers", FILL_CUSTOMERS, {"n": customers}),
            ("products", FILL_PRODUCTS, {"n": products}),
            (
                "orders",
                FILL_ORDERS,
                {"n": orders, "customers": customers, "products": products},
            ),
        ):
            existing = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
            if existing and existing[0] == 0:
                started = time.perf_counter()
                conn.execute(query, params)
                logger.info("seeded %s in %.1fs", table, time.perf_counter() - started)
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
            counts[table] = int(row[0]) if row else 0
        conn.execute(ROLES)
        # Fresh statistics so the planner (and Phase 4's plans) see realistic row counts.
        conn.execute("ANALYZE customers, products, orders")
    return counts


def seed_mongo(url: str, customers: int, products: int, orders: int) -> dict[str, int]:
    """Fill empty collections, create the control indexes, and turn the profiler on."""
    client: MongoClient[dict[str, object]] = MongoClient(url, appname="shop-seed")
    with client:
        db = client[MONGO_DATABASE]
        db.command("profile", 0)  # off while loading, so the seed isn't profiled
        if db.orders.estimated_document_count() == 0:
            started = time.perf_counter()
            now = time.time()
            batch = []
            for n in range(1, orders + 1):
                batch.append(
                    {
                        "orderNo": n,
                        "customerId": 1 + (n * 7919) % customers,  # M1: no index on this
                        "status": STATUSES[n % len(STATUSES)],
                        "total": round(10 + (n * 37) % 990 + (n % 100) / 100, 2),
                        # M2: no index on placedAt
                        "placedAt": datetime.fromtimestamp(now - (n * 104729) % (730 * 86400), UTC),
                    }
                )
                if len(batch) == 5_000:
                    db.orders.insert_many(batch, ordered=False)
                    batch = []
            if batch:
                db.orders.insert_many(batch, ordered=False)
            logger.info("seeded mongo orders in %.1fs", time.perf_counter() - started)
        if db.products.estimated_document_count() == 0:
            db.products.insert_many(
                [
                    {"sku": f"SKU-{n:06d}", "name": f"Product {n}", "price": 5 + n % 495}
                    for n in range(1, products + 1)
                ],
                ordered=False,
            )
        db.orders.create_index([("orderNo", ASCENDING)], name="orderNo_1")  # control MC2
        db.products.create_index([("sku", ASCENDING)], name="sku_1")  # M3's index, and MC4's
        # A bigger capped system.profile (it can only be recreated while profiling is off).
        stats = (
            db.command("collStats", "system.profile")
            if "system.profile" in set(db.list_collection_names())
            else {}
        )
        if int(stats.get("maxSize", 0)) < PROFILE_SIZE_BYTES:
            db.drop_collection("system.profile")
            db.create_collection("system.profile", capped=True, size=PROFILE_SIZE_BYTES)
        db.command("profile", 1, slowms=0)
        return {
            "orders": int(db.orders.estimated_document_count()),
            "products": int(db.products.estimated_document_count()),
        }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    counts = seed(
        os.environ.get("QP_DEMO_DSN", DEFAULT_DSN),
        customers=_size("QP_DEMO_CUSTOMERS", 20_000),
        products=_size("QP_DEMO_PRODUCTS", 5_000),
        orders=_size("QP_DEMO_ORDERS", 200_000),
    )
    logger.info("demo database ready: %s", counts)
    mongo_url = os.environ.get("QP_DEMO_MONGO_URL", DEFAULT_MONGO_URL)
    if mongo_url:
        mongo_counts = seed_mongo(
            mongo_url,
            customers=_size("QP_DEMO_CUSTOMERS", 20_000),
            products=_size("QP_DEMO_MONGO_PRODUCTS", 20_000),
            orders=_size("QP_DEMO_MONGO_ORDERS", 50_000),
        )
        logger.info("demo mongodb ready: %s", mongo_counts)


if __name__ == "__main__":
    main()
