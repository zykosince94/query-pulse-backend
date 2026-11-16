"""Phase 1 close-out: the Origin check, the login rate limit, and JSON logs with request IDs."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from query_pulse.accounts.ratelimit import LoginRateLimiter
from query_pulse.config import Settings
from query_pulse.log import JsonFormatter, new_request_id, request_id_var
from query_pulse.main import create_app
from tests.conftest import ADMIN_PASSWORD

# --- Origin check ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin", ["https://evil.example", "http://testserver.evil.example", "null", "not a url"]
)
def test_cross_site_writes_are_refused_before_anything_runs(
    client: TestClient, origin: str
) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
        headers={"Origin": origin},
    )
    assert response.status_code == 403
    assert "Cross-site" in response.json()["detail"]
    assert "qp_session" not in response.cookies, "no session was started"


@pytest.mark.parametrize("origin", ["http://testserver", "https://testserver", None])
def test_same_origin_and_non_browser_writes_pass(client: TestClient, origin: str | None) -> None:
    headers = {"Origin": origin} if origin else {}
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}, headers=headers
    )
    assert response.status_code == 200


def test_reads_are_not_origin_checked(login: Callable[[str], TestClient]) -> None:
    client = login("admin")
    assert client.get("/api/sources", headers={"Origin": "https://evil.example"}).status_code == 200


def test_explicitly_allowed_origins_pass(settings: Settings) -> None:
    allowed = settings.model_copy(
        update={"allowed_origins": "https://qp.example.com/, https://b.example"}
    )
    with TestClient(create_app(allowed)) as client:
        response = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
            headers={"Origin": "https://QP.example.com"},
        )
        assert response.status_code == 200


def test_signed_in_cross_site_write_is_refused(login: Callable[[str], TestClient]) -> None:
    client = login("admin")
    response = client.delete("/api/sources/1", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


# --- login rate limit ------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limiter_locks_after_max_failures_and_unlocks_after_the_window() -> None:
    clock = Clock()
    limiter = LoginRateLimiter(max_failures=3, window_seconds=60, clock=clock)
    for _ in range(3):
        assert limiter.retry_after("1.2.3.4") is None
        limiter.record_failure("1.2.3.4")
        clock.now += 1
    assert limiter.retry_after("1.2.3.4") == 57  # now 1003; the first failure ages out at 1060
    assert limiter.retry_after("5.6.7.8") is None, "other clients aren't affected"
    clock.now = 1061
    assert limiter.retry_after("1.2.3.4") is None


def test_sign_in_is_refused_after_too_many_failures(settings: Settings) -> None:
    strict = settings.model_copy(update={"login_max_failures": 3})
    with TestClient(create_app(strict)) as client:
        for _ in range(3):
            bad = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-pw"})
            assert bad.status_code == 401
        # Even the right password is refused now: guessing teaches nothing while locked.
        locked = client.post(
            "/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        )
        assert locked.status_code == 429
        assert int(locked.headers["Retry-After"]) > 0
        assert "Try again in" in locked.json()["detail"]


def test_successful_sign_ins_never_count(settings: Settings) -> None:
    strict = settings.model_copy(update={"login_max_failures": 2})
    with TestClient(create_app(strict)) as client:
        for _ in range(5):
            ok = client.post(
                "/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
            )
            assert ok.status_code == 200


# --- request IDs and JSON logs ---------------------------------------------------------------


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    generated = client.get("/health").headers["X-Request-ID"]
    assert len(generated) == 32
    assert (
        client.get("/health", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"]
        == "abc-123"
    )
    # Anything that could forge a log line or a header is replaced, not echoed.
    replaced = client.get("/health", headers={"X-Request-ID": "x" * 65}).headers["X-Request-ID"]
    assert replaced != "x" * 65 and len(replaced) == 32


@pytest.mark.parametrize("incoming", [None, "", "a b", "a\nb", "é", "x" * 65])
def test_unsafe_request_ids_are_replaced(incoming: str | None) -> None:
    assert len(new_request_id(incoming)) == 32


@pytest.fixture
def records(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    caplog.set_level(logging.INFO, logger="query_pulse.access")
    yield caplog


def test_access_log_has_request_id_fields_and_no_query_string(
    login: Callable[[str], TestClient], records: pytest.LogCaptureFixture
) -> None:
    client = login("admin")
    request_id = client.get(
        "/api/operations?q=secret-search-term", headers={"X-Request-ID": "req-42"}
    ).headers["X-Request-ID"]
    [line] = [r for r in records.records if getattr(r, "path", None) == "/api/operations"]
    assert (line.method, line.status) == ("GET", 200)  # type: ignore[attr-defined]
    assert line.duration_ms >= 0  # type: ignore[attr-defined]
    assert "secret-search-term" not in line.getMessage()

    request_id_var.set(request_id)
    entry = json.loads(JsonFormatter().format(line))
    request_id_var.set(None)
    assert entry["request_id"] == "req-42"
    assert entry["level"] == "INFO" and entry["logger"] == "query_pulse.access"
    assert entry["path"] == "/api/operations" and entry["status"] == 200
    assert "secret-search-term" not in json.dumps(entry)


def test_health_checks_are_not_access_logged(
    client: TestClient, records: pytest.LogCaptureFixture
) -> None:
    client.get("/health")
    client.get("/ready")
    assert not [
        r for r in records.records if getattr(r, "path", "").startswith(("/health", "/ready"))
    ]


def test_json_formatter_includes_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    entry = json.loads(JsonFormatter().format(record))
    assert entry["message"] == "failed" and "ValueError: boom" in entry["exception"]


# --- found by fuzzing (Phase 7) -----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/sources/2147483648",
        "/api/sources/9223372036854775808/trend",
        "/api/operations/2147483648",
        "/api/plans/9223372036854775808",
        "/api/findings/2147483648",
        "/api/findings/2147483648/ai",
    ],
)
def test_ids_beyond_the_integer_column_are_422_not_500(
    login: Callable[[str], TestClient], path: str
) -> None:
    response = login("demo").get(path)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_window_errors_have_the_documented_validation_shape(
    login: Callable[[str], TestClient],
) -> None:
    response = login("demo").get(
        "/api/operations", params={"minutes": 5, "start": "2026-01-01T00:00:00Z"}
    )
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["query", "minutes"] and "not both" in error["msg"]
