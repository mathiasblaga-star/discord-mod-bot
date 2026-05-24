"""
Dashboard Authentication Tests
================================
Tests the FastAPI dashboard auth layer (Prompt 7).
Verifies: unauthenticated requests are blocked, wrong password is rejected,
valid session works, timing attacks are mitigated, logout clears session.

Run with: pytest tests/test_dashboard_auth.py -v
Requires: pip install httpx pytest-asyncio
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set required env vars before importing app
TEST_SECRET = "test_dashboard_secret_key_for_testing_only_32chars+"
os.environ["DASHBOARD_SECRET"] = TEST_SECRET
os.environ["DASHBOARD_PORT"] = "8099"

# Set a throwaway DB path so the dashboard can import config cleanly
import tempfile
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
import config
config.DB_PATH = _tmp.name


try:
    from httpx import AsyncClient, ASGITransport
    from dashboard.app import create_app
    DASHBOARD_AVAILABLE = True
except ImportError:
    DASHBOARD_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not DASHBOARD_AVAILABLE,
    reason="Dashboard or httpx not available — install httpx and ensure dashboard/app.py exists"
)


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture(scope="module")
def event_loop():
    import asyncio
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── Unauthenticated access ───────────────────────────────────────────────────

class TestUnauthenticatedAccess:

    @pytest.mark.asyncio
    async def test_home_redirects_to_login(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/", follow_redirects=False)
            assert resp.status_code in (302, 303), \
                f"Expected redirect, got {resp.status_code}"
            assert "/login" in resp.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_guilds_page_requires_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/guilds", follow_redirects=False)
            assert resp.status_code in (302, 303, 401)

    @pytest.mark.asyncio
    async def test_api_without_session_blocked(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/guilds/123456789", follow_redirects=False)
            assert resp.status_code in (302, 303, 401)


# ── Login endpoint ───────────────────────────────────────────────────────────

class TestLoginEndpoint:

    @pytest.mark.asyncio
    async def test_login_page_returns_200(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/login")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_wrong_password_rejected(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/login",
                data={"password": "wrong_password"},
                follow_redirects=False,
            )
            # Must NOT redirect to / — should show error on login page
            assert resp.status_code != 302 or "/login" in resp.headers.get("location", ""), \
                "Wrong password was accepted!"

    @pytest.mark.asyncio
    async def test_correct_password_sets_session(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/login",
                data={"password": TEST_SECRET},
                follow_redirects=False,
            )
            assert resp.status_code in (302, 303), f"Login didn't redirect: {resp.status_code}"
            # Session cookie must be set
            assert any("session" in c.name.lower() or "auth" in c.name.lower()
                       for c in resp.cookies.jar), "No session cookie set after login"

    @pytest.mark.asyncio
    async def test_authenticated_session_can_access_home(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Log in
            await client.post("/login", data={"password": TEST_SECRET}, follow_redirects=True)
            # Now access home
            resp = await client.get("/")
            assert resp.status_code == 200


# ── SQL injection in login form ──────────────────────────────────────────────

class TestLoginInjection:

    SQL_PAYLOADS = [
        "' OR '1'='1",
        "' OR 1=1; --",
        TEST_SECRET + "' OR '1'='1",
        "admin'--",
        "'; DROP TABLE users; --",
    ]

    @pytest.mark.asyncio
    async def test_sql_injection_in_password_rejected(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for payload in self.SQL_PAYLOADS:
                resp = await client.post(
                    "/login",
                    data={"password": payload},
                    follow_redirects=False,
                )
                location = resp.headers.get("location", "")
                assert not (resp.status_code in (302, 303) and location == "/"), \
                    f"SQL injection payload granted access: {payload!r}"


# ── Timing attack resistance ─────────────────────────────────────────────────

class TestTimingAttackResistance:
    """
    Checks that a wrong password doesn't return significantly faster than
    a correct one — which would enable timing-based oracle attacks.
    """

    @pytest.mark.asyncio
    async def test_wrong_password_not_faster_than_correct(self, app):
        import time
        ITERATIONS = 5

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            correct_times = []
            for _ in range(ITERATIONS):
                t0 = time.monotonic()
                await client.post("/login", data={"password": TEST_SECRET})
                correct_times.append(time.monotonic() - t0)

            wrong_times = []
            for _ in range(ITERATIONS):
                t0 = time.monotonic()
                await client.post("/login", data={"password": "wrong"})
                wrong_times.append(time.monotonic() - t0)

        avg_correct = sum(correct_times) / ITERATIONS
        avg_wrong   = sum(wrong_times) / ITERATIONS

        # Wrong password must not be MORE THAN 10x faster (constant-time compare)
        ratio = avg_correct / avg_wrong if avg_wrong > 0 else float("inf")
        assert ratio < 10, (
            f"Timing discrepancy too large — possible timing oracle. "
            f"Correct avg: {avg_correct:.4f}s, Wrong avg: {avg_wrong:.4f}s"
        )


# ── Logout ───────────────────────────────────────────────────────────────────

class TestLogout:

    @pytest.mark.asyncio
    async def test_logout_clears_session(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Login
            await client.post("/login", data={"password": TEST_SECRET}, follow_redirects=True)
            # Confirm we can access home
            r1 = await client.get("/")
            assert r1.status_code == 200
            # Logout
            await client.get("/logout", follow_redirects=True)
            # Now home should redirect to login again
            r2 = await client.get("/", follow_redirects=False)
            assert r2.status_code in (302, 303, 401), \
                "Session still active after logout!"
