# tests/test_main.py
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

# Mark all tests in this file as async
pytestmark = pytest.mark.asyncio


def _client() -> AsyncClient:
    """
    httpx 0.28 removed the `app=` shortcut; an ASGI app must now be passed
    through an explicit transport.
    """
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_health_check():
    """
    Tests the public health check endpoint.
    """
    async with _client() as ac:
        response = await ac.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_unknown_route_is_not_found():
    """
    An undefined route returns 404 regardless of auth mode.

    This deliberately does not assert 401: the template ships with
    AUTH_BYPASS=true, so protected routes authenticate as the dev user and a
    401 assertion fails for the wrong reason. Authorization is covered by the
    authz engine's own tests.
    """
    async with _client() as ac:
        response = await ac.get("/api/definitely-not-a-real-route")
    assert response.status_code == 404


# Additional tests would include:
# - Database integration tests
# - Authorization engine tests
# - API endpoint tests with mocked authentication
# - Model validation tests
