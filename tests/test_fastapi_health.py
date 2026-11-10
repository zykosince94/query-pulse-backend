from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_endpoint():
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_login_page_loads():
    response = client.get("/login")
    assert response.status_code == 200


def test_login_success_sets_session():
    response = client.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/me"


def test_demo_user_is_read_only_message():
    response = client.post("/login", data={"username": "demo", "password": "demo123"}, follow_redirects=False)
    assert response.status_code == 302

    me_response = client.get("/me", follow_redirects=False)
    assert me_response.status_code == 200
    assert "Demo users are read-only" in me_response.text
