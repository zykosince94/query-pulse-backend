"""Real PostgreSQL and MongoDB servers in throwaway containers (testcontainers).

These tests need Docker. Without it they're skipped locally, but CI always has Docker, so
they always run there. Containers are shared per module to keep the suite fast.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from pymongo import MongoClient

docker = pytest.importorskip("docker")


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
    except Exception:  # noqa: BLE001 - any failure means no usable Docker
        return False
    return True


if not _docker_available():
    pytest.skip("Docker isn't available", allow_module_level=True)

from testcontainers.community.mongodb import MongoDbContainer  # noqa: E402
from testcontainers.community.postgres import PostgresContainer  # noqa: E402


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this folder `integration` (a conftest's `pytestmark` isn't applied
    to test modules, so `-m integration` used to select nothing)."""
    for item in items:
        if "tests/integration/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


@dataclass(frozen=True)
class Server:
    host: str
    port: int


def _host(container: PostgresContainer | MongoDbContainer) -> str:
    # "localhost" can resolve to ::1 first, which Docker on Windows doesn't answer quickly.
    host = container.get_container_host_ip()
    return "127.0.0.1" if host == "localhost" else host


def _superuser_sql(container: PostgresContainer, dbname: str, *statements: str) -> None:
    with psycopg.connect(
        host=_host(container),
        port=int(container.get_exposed_port(5432)),
        user=container.username,
        password=container.password,
        dbname=dbname,
        autocommit=True,
    ) as conn:
        for statement in statements:
            conn.execute(statement)  # fixed test SQL


@pytest.fixture(scope="session")
def pg_preloaded() -> Iterator[Server]:
    """PostgreSQL 16 with pg_stat_statements preloaded, set up for each permission scenario:

    - database `full`  : extension created
    - database `noext` : extension NOT created
    - role `monitor`   : has pg_monitor
    - role `plain`     : no pg_monitor
    """
    container = PostgresContainer("postgres:16", driver=None).with_command(
        "postgres -c shared_preload_libraries=pg_stat_statements"
    )
    with container:
        db = container.dbname
        _superuser_sql(
            container,
            db,
            "CREATE DATABASE full_db",
            "CREATE DATABASE noext",
            "CREATE ROLE monitor LOGIN PASSWORD 'monitor-pass'",
            "GRANT pg_monitor TO monitor",
            "CREATE ROLE plain LOGIN PASSWORD 'plain-pass'",
        )
        _superuser_sql(container, "full_db", "CREATE EXTENSION pg_stat_statements")
        yield Server(_host(container), int(container.get_exposed_port(5432)))


@pytest.fixture(scope="session")
def pg_plain() -> Iterator[Server]:
    """PostgreSQL 16 without pg_stat_statements loaded at all."""
    with PostgresContainer("postgres:16", driver=None) as container:
        _superuser_sql(
            container,
            container.dbname,
            "CREATE ROLE monitor LOGIN PASSWORD 'monitor-pass'",
            "GRANT pg_monitor TO monitor",
        )
        yield Server(_host(container), int(container.get_exposed_port(5432)))


@dataclass(frozen=True)
class MongoServer(Server):
    root_url: str
    root_user: str
    root_password: str


@pytest.fixture(scope="session")
def mongo() -> Iterator[MongoServer]:
    """MongoDB 7 with auth, plus users for each permission scenario:

    - `monitor` : clusterMonitor + read on `shop`
    - `reader`  : read on `shop` only (no clusterMonitor)
    """
    with MongoDbContainer("mongo:7") as container:
        host = _host(container)
        port = int(container.get_exposed_port(27017))
        root_url = (
            f"mongodb://{container.username}:{container.password}@{host}:{port}/"
            "?authSource=admin&directConnection=true"
        )
        client: MongoClient[dict[str, Any]] = MongoClient(root_url)
        with client:
            admin = client.admin
            admin.command(
                "createUser",
                "monitor",
                pwd="monitor-pass",
                roles=[{"role": "clusterMonitor", "db": "admin"}, {"role": "read", "db": "shop"}],
            )
            admin.command(
                "createUser", "reader", pwd="reader-pass", roles=[{"role": "read", "db": "shop"}]
            )
            client.shop.orders.insert_one({"customerId": 1})
        yield MongoServer(host, port, root_url, container.username, container.password)


@pytest.fixture(scope="session")
def pg_tls() -> Iterator[Server]:
    """PostgreSQL 16 with TLS on, using the image's self-signed "snakeoil" certificate.

    Self-signed means: encryption works ("require"), but certificate verification
    ("verify-full") must fail, because no trusted CA signed it.
    """
    container = PostgresContainer("postgres:16", driver=None).with_command(
        "postgres -c ssl=on "
        "-c ssl_cert_file=/etc/ssl/certs/ssl-cert-snakeoil.pem "
        "-c ssl_key_file=/etc/ssl/private/ssl-cert-snakeoil.key"
    )
    with container:
        _superuser_sql(
            container,
            container.dbname,
            "CREATE ROLE monitor LOGIN PASSWORD 'monitor-pass'",
            "GRANT pg_monitor TO monitor",
        )
        yield Server(_host(container), int(container.get_exposed_port(5432)))


# --- collector targets (Phase 3) --------------------------------------------------------------


@dataclass(frozen=True)
class PgTarget(Server):
    """A monitored PostgreSQL for collector tests, with superuser access for setting up."""

    superuser: str
    superpassword: str
    container: PostgresContainer

    def superuser_dsn(self, dbname: str = "collect_db") -> str:
        return (
            f"host={self.host} port={self.port} dbname={dbname} "
            f"user={self.superuser} password={self.superpassword}"
        )


def _collector_target(*extra_settings: str) -> Iterator[PgTarget]:
    command = "postgres -c shared_preload_libraries=pg_stat_statements " + " ".join(
        f"-c {s}" for s in extra_settings
    )
    with PostgresContainer("postgres:16", driver=None).with_command(command) as container:
        _superuser_sql(
            container,
            container.dbname,
            "CREATE DATABASE collect_db",
            "CREATE ROLE monitor LOGIN PASSWORD 'monitor-pass'",
            "GRANT pg_monitor TO monitor",
        )
        _superuser_sql(
            container,
            "collect_db",
            "CREATE EXTENSION pg_stat_statements",
            "CREATE TABLE widgets (id bigserial PRIMARY KEY, name text)",
            "INSERT INTO widgets (name) SELECT 'w' || g FROM generate_series(1, 1000) g",
            "ANALYZE widgets",
        )
        yield PgTarget(
            _host(container),
            int(container.get_exposed_port(5432)),
            container.username,
            container.password,
            container,
        )


@pytest.fixture(scope="session")
def pg_collect() -> Iterator[PgTarget]:
    yield from _collector_target()


@pytest.fixture(scope="session")
def pg_small_max() -> Iterator[PgTarget]:
    """pg_stat_statements.max at its minimum (100), so evictions are easy to trigger."""
    yield from _collector_target("pg_stat_statements.max=100")


@pytest.fixture
def pg_pausable() -> Iterator[PgTarget]:
    """Its own container, so pausing it (to simulate an outage) can't affect other tests."""
    yield from _collector_target()
