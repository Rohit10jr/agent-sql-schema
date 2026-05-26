"""Smoke tests for the /api/healthz/ probe endpoint."""

import pytest
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_healthz_returns_ok_when_db_reachable(api_client: APIClient) -> None:
    """Happy path: DB up → 200 OK with status=ok, db=True."""
    response = api_client.get("/api/healthz/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] is True
    assert body["db_error"] is None


def test_healthz_requires_no_auth(api_client: APIClient) -> None:
    """No JWT, no session → still 200. Probes should never auth-fail."""
    response = api_client.get("/api/healthz/")
    # The endpoint is public; whether DB is up or down, it must not 401/403.
    assert response.status_code in (200, 503)


def test_healthz_rejects_post(api_client: APIClient) -> None:
    """@require_GET — only GET allowed."""
    response = api_client.post("/api/healthz/")
    assert response.status_code == 405
