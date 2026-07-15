"""Security header, CORS, and CSRF-posture tests (SEC-002)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from soa_api.app import api_security_headers, create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:admin"}


async def make_client(tmp_path: Path, **settings: object) -> TestClient:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/sec.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(
        ApiSettings(environment=Environment.TEST, **settings),  # type: ignore[arg-type]
        db=DatabaseSessions(engine),
        object_store=MemoryObjectStore(),
    )
    return TestClient(app, raise_server_exceptions=False)


async def test_every_response_carries_the_lockdown_headers(tmp_path: Path) -> None:
    client = await make_client(tmp_path)
    for response in (client.get("/healthz"), client.get("/nope"), client.get("/orgs/x")):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
        assert (
            response.headers["Content-Security-Policy"]
            == "default-src 'none'; frame-ancestors 'none'"
        )


async def test_hsts_is_production_only(tmp_path: Path) -> None:
    client = await make_client(tmp_path)
    assert "Strict-Transport-Security" not in client.get("/healthz").headers
    production = api_security_headers(is_production=True)
    assert production["Strict-Transport-Security"].startswith("max-age=")
    assert "Strict-Transport-Security" not in api_security_headers(is_production=False)


async def test_no_endpoint_ever_sets_a_cookie(tmp_path: Path) -> None:
    """CSRF posture: auth is header-borne, never cookies — pinned here."""
    client = await make_client(tmp_path)
    for response in (
        client.get("/healthz"),
        client.get("/me", headers=ADMIN),
        client.post("/organizations", json={"name": "N", "slug": "n"}, headers=ADMIN),
    ):
        assert "set-cookie" not in response.headers


async def test_allowlisted_origins_get_credentialed_cors(tmp_path: Path) -> None:
    client = await make_client(tmp_path, cors_allowed_origins=("https://app.example",))
    preflight = client.options(
        "/me",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.headers["access-control-allow-origin"] == "https://app.example"
    assert preflight.headers["access-control-allow-credentials"] == "true"


async def test_unlisted_origins_get_nothing(tmp_path: Path) -> None:
    client = await make_client(tmp_path, cors_allowed_origins=("https://app.example",))
    response = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


async def test_no_configuration_means_no_cors_at_all(tmp_path: Path) -> None:
    client = await make_client(tmp_path)
    response = client.get("/healthz", headers={"Origin": "https://app.example"})
    assert "access-control-allow-origin" not in response.headers


async def test_signed_dev_blobs_are_cross_origin_loadable(tmp_path: Path) -> None:
    """A signed dev blob (a page image) must carry Cross-Origin-Resource-Policy:
    cross-origin so the web viewer can load it even when the web app and the API
    are reached on different sites (localhost web vs 127.0.0.1 API). The default
    same-site policy would block it with ERR_BLOCKED_BY_RESPONSE.NotSameSite —
    the HMAC signature is the authorization, so cross-origin loading is correct."""
    from urllib.parse import urlsplit

    from soa_storage.filesystem import FilesystemObjectStore

    store = FilesystemObjectStore(root=str(tmp_path / "blobs"))
    key = "orgs/o/documents/d/page_image/p.png"
    await store.put(key, b"PNGDATA", content_type="image/png")
    signed = await store.signed_download_url(key, expires_in_seconds=300)

    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/blob.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(
        ApiSettings(environment=Environment.TEST),
        db=DatabaseSessions(engine),
        object_store=store,
    )
    client = TestClient(app, raise_server_exceptions=False)
    parts = urlsplit(signed.url)
    response = client.get(f"{parts.path}?{parts.query}")
    assert response.status_code == 200
    assert response.content == b"PNGDATA"
    assert response.headers["Cross-Origin-Resource-Policy"] == "cross-origin"


def test_wildcard_and_plain_http_origins_are_refused_at_startup() -> None:
    with pytest.raises(ValidationError, match="wildcard"):
        ApiSettings(environment=Environment.TEST, cors_allowed_origins=("*",))
    with pytest.raises(ValidationError, match="wildcard"):
        ApiSettings(environment=Environment.TEST, cors_allowed_origins=("https://*.example",))
    with pytest.raises(ValidationError, match="https://"):
        ApiSettings(environment=Environment.TEST, cors_allowed_origins=("http://app.example",))
