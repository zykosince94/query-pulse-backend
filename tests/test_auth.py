"""QP-AC-02: the admin signs in and out."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN_PASSWORD


def test_admin_signs_in_and_sees_their_profile(login: Callable[[str], TestClient]) -> None:
    client = login("admin")
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json() == {"username": "admin", "role": "admin", "read_only": False}


def test_demo_user_is_reported_read_only(login: Callable[[str], TestClient]) -> None:
    client = login("demo")
    assert client.get("/api/auth/me").json() == {
        "username": "demo",
        "role": "demo",
        "read_only": True,
    }


def test_sign_out_ends_the_session(login: Callable[[str], TestClient]) -> None:
    client = login("admin")
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize(
    ("username", "password"),
    [("admin", "wrong-password"), ("nobody", ADMIN_PASSWORD), ("admin", "")],
)
def test_bad_credentials_are_rejected(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code in (401, 422)
    assert "qp_session" not in response.cookies


def test_wrong_username_and_wrong_password_look_identical(client: TestClient) -> None:
    wrong_user = client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    wrong_pass = client.post("/api/auth/login", json={"username": "admin", "password": "x"})
    assert wrong_user.status_code == wrong_pass.status_code == 401
    assert wrong_user.json() == wrong_pass.json()


def test_session_cookie_is_http_only(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
    )
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
