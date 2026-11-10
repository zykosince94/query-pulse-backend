import pytest
from django.contrib.auth import get_user_model
from django.test import Client


@pytest.mark.django_db
def test_login_page_loads():
    client = Client()
    response = client.get("/login/")

    assert response.status_code == 200


@pytest.mark.django_db
def test_demo_user_is_read_only():
    client = Client()
    User = get_user_model()
    user = User.objects.create_user("demo", "demo@example.com", "demo123")
    client.force_login(user)

    response = client.get("/me/")
    assert response.status_code == 200
    assert "Demo users are read-only" in response.content.decode()
