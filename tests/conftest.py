import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient

TEST_PASSWORD = "pw"


def make_user(email: str, **extra):
    return get_user_model().objects.create_user(
        username=email.split("@")[0], email=email, password=TEST_PASSWORD, **extra
    )


@pytest.fixture(autouse=True)
def _fresh_cache(db):
    """Rate-limit counters and used challenges live in the cache; start each test clean."""
    cache.clear()


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user("tester", password=TEST_PASSWORD)


@pytest.fixture
def api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client
