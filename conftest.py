"""Shared pytest fixtures for the whole test suite.

This file lives at the project root so every test under any directory picks
it up automatically. Keep fixtures minimal and reusable — test-specific setup
belongs in a fixture next to the test, not here.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def api_client() -> APIClient:
    """Unauthenticated DRF client. Use for public endpoints (signup, healthz)."""
    return APIClient()


@pytest.fixture
def user(db):
    """A regular authenticated user. `db` enables DB access for this test."""
    User = get_user_model()
    return User.objects.create_user(
        email="alice@example.com",
        password="testpass123",  # pragma: allowlist secret
        first_name="Alice",
        last_name="Smith",
    )


@pytest.fixture
def authed_client(api_client: APIClient, user) -> APIClient:
    """API client pre-authenticated as `user`. JWT issued via rest_framework_simplejwt."""
    from rest_framework_simplejwt.tokens import RefreshToken

    token = RefreshToken.for_user(user)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return api_client
