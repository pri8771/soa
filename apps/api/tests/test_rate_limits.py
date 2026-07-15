"""Rate limiting and abuse control tests (SEC-003): the limiter's
window/backoff semantics, per-operation and per-identity isolation,
protected endpoints answering 429 with safe backpressure headers, and
the identity-free observability counters."""

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.services.rate_limit import SlidingWindowRateLimiter
from soa_api.settings import ApiSettings, Environment
from soa_api.test_support.runtime_config import publish_runtime_config
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:admin"}


class TestLimiter:
    def test_the_window_slides_and_denials_carry_backoff(self) -> None:
        limiter = SlidingWindowRateLimiter()
        assert limiter.check("op", "a", 2, now=0.0).allowed
        assert limiter.check("op", "a", 2, now=1.0).allowed
        denied = limiter.check("op", "a", 2, now=2.0)
        assert not denied.allowed
        assert denied.retry_after_seconds >= 1
        assert denied.headers()["Retry-After"] == str(denied.retry_after_seconds)
        # The window drains: the first event ages out after 60s.
        assert limiter.check("op", "a", 2, now=61.0).allowed

    def test_denied_requests_are_not_charged(self) -> None:
        limiter = SlidingWindowRateLimiter()
        limiter.check("op", "a", 1, now=0.0)
        for offset in (1.0, 2.0, 3.0):
            assert not limiter.check("op", "a", 1, now=offset).allowed
        # Backing off past the window recovers exactly — denials did not
        # extend the punishment.
        assert limiter.check("op", "a", 1, now=60.5).allowed

    def test_operations_and_identities_are_isolated(self) -> None:
        limiter = SlidingWindowRateLimiter()
        assert limiter.check("uploads", "a", 1, now=0.0).allowed
        assert not limiter.check("uploads", "a", 1, now=0.1).allowed
        assert limiter.check("uploads", "b", 1, now=0.2).allowed  # other identity
        assert limiter.check("replays", "a", 1, now=0.3).allowed  # other operation

    def test_enforce_raises_429_with_headers(self) -> None:
        limiter = SlidingWindowRateLimiter()
        limiter.enforce("op", "a", 1, now=0.0)
        with pytest.raises(HTTPException) as excinfo:
            limiter.enforce("op", "a", 1, now=1.0)
        assert excinfo.value.status_code == 429
        assert excinfo.value.headers is not None
        assert "Retry-After" in excinfo.value.headers

    def test_counters_are_observable_and_identity_free(self) -> None:
        limiter = SlidingWindowRateLimiter()
        limiter.check("op", "secret-identity", 1, now=0.0)
        limiter.check("op", "secret-identity", 1, now=0.1)
        snapshot = limiter.snapshot()
        assert snapshot == {"op": {"allowed": 1, "denied": 1}}
        assert "secret-identity" not in str(snapshot)


@pytest.fixture
async def harness(tmp_path: Path) -> TestClient:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/ratelimit.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(
        ApiSettings(
            environment=Environment.TEST,
            rate_limit_uploads_per_minute=2,
            rate_limit_reprocess_per_minute=1,
        ),
        db=db,
        object_store=MemoryObjectStore(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Uploads", "slug": "uploads"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201
    await publish_runtime_config(client, db, stream_slug="uploads", headers=ADMIN)
    return client


async def test_upload_sessions_hit_the_cap_with_safe_backpressure(
    harness: TestClient,
) -> None:
    client = harness
    body = {
        "filename": "po.pdf",
        "size_bytes": 1000,
        "content_type": "application/pdf",
        "sha256": "a" * 64,
    }
    responses = [
        client.post("/orgs/northstar/streams/uploads/uploads", json=body, headers=ADMIN)
        for _ in range(3)
    ]
    assert [r.status_code for r in responses[:2]] == [201, 201]
    limited = responses[2]
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert limited.headers["X-RateLimit-Remaining"] == "0"
    assert "retry" in limited.json()["error"]["message"].lower()


async def test_reprocess_is_capped_per_principal(harness: TestClient) -> None:
    client = harness
    # Nonexistent document: the limiter runs BEFORE the lookup, so the
    # second call is 429 even though both would otherwise be 404 — abuse
    # probing is throttled too.
    target = "/orgs/northstar/documents/019f0000-0000-7000-8000-000000000000/reprocess"
    assert (
        client.post(
            target, json={"mode": "current_config", "reason": "probing"}, headers=ADMIN
        ).status_code
        == 404
    )
    assert (
        client.post(
            target, json={"mode": "current_config", "reason": "probing"}, headers=ADMIN
        ).status_code
        == 429
    )


async def test_limits_are_observable_via_the_health_surface(harness: TestClient) -> None:
    client = harness
    body = {
        "filename": "po.pdf",
        "size_bytes": 1000,
        "content_type": "application/pdf",
        "sha256": "a" * 64,
    }
    for _ in range(3):
        client.post("/orgs/northstar/streams/uploads/uploads", json=body, headers=ADMIN)
    counters = client.get("/health/rate-limits").json()
    assert counters["uploads"]["allowed"] == 2
    assert counters["uploads"]["denied"] == 1
    assert "user:" not in str(counters)
