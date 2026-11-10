import pytest
from django.test import Client


@pytest.mark.django_db
def test_health_endpoint():
    client = Client()
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_ready_endpoint():
    client = Client()
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
