"""Application factory."""

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

import soa_api
from soa_api.auth.oidc import OidcTokenValidator, httpx_jwks_fetcher
from soa_api.dependencies import Dependencies
from soa_api.errors import CORRELATION_HEADER, register_error_handlers
from soa_api.routers import (
    analytics,
    artifacts,
    audit,
    catalogs,
    data_exports,
    documents,
    email_intake,
    exports,
    health,
    instructions,
    integrations,
    jobs,
    me,
    organizations,
    processes,
    providers,
    public_ingest,
    review,
    uploads,
)
from soa_api.services.malware import ClamAvScanner, MalwareScanner, NoopScanner
from soa_api.settings import ApiSettings, load_settings
from soa_config import SecretStore
from soa_config.logging import correlation_context
from soa_config.telemetry import Telemetry, configure_telemetry
from soa_db import DatabaseSessions, create_database_engine
from soa_storage import MemoryObjectStore, ObjectStore
from soa_storage.s3 import S3ObjectStore, S3Settings
from soa_storage.secrets_gcp import build_secret_store

logger = logging.getLogger(__name__)


def api_security_headers(*, is_production: bool) -> dict[str, str]:
    """Response headers for every API response (SEC-002).

    The API serves JSON to programmatic clients, so its CSP is total
    lockdown — nothing may embed, script, or frame an API response. HSTS
    applies only in production (local HTTP must keep working). CSRF
    posture: authentication is header-borne (Authorization / X-Dev-User),
    never cookies — the browser cannot be confused into attaching
    credentials cross-site, so no CSRF token is needed; a test pins the
    no-cookies invariant.
    """
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-site",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    }
    if is_production:
        headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return headers


def create_app(
    settings: ApiSettings | None = None,
    telemetry: Telemetry | None = None,
    oidc_validator: "OidcTokenValidator | None" = None,
    db: DatabaseSessions | None = None,
    object_store: ObjectStore | None = None,
    malware_scanner: MalwareScanner | None = None,
    secret_store: SecretStore | None = None,
) -> FastAPI:
    """Create the API application.

    Settings validate on load, so a misconfigured process fails at startup
    instead of serving with unsafe defaults.
    """
    resolved = settings if settings is not None else load_settings()
    if oidc_validator is None and resolved.oidc_issuer:
        assert resolved.oidc_audience and resolved.oidc_jwks_url  # settings validation
        oidc_validator = OidcTokenValidator(
            issuer=resolved.oidc_issuer,
            audience=resolved.oidc_audience,
            jwks_fetcher=httpx_jwks_fetcher(resolved.oidc_jwks_url),
        )
    resolved_telemetry = (
        telemetry
        if telemetry is not None
        else configure_telemetry(
            service_name=resolved.service_name,
            environment=resolved.environment.value,
            profile=resolved.telemetry_profile,
            otlp_endpoint=resolved.otlp_endpoint,
        )
    )

    app = FastAPI(
        title="SOA API",
        version=soa_api.__version__,
        docs_url="/docs" if not resolved.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not resolved.is_production else None,
    )
    resolved_db = db
    if resolved_db is None:
        resolved_db = DatabaseSessions(create_database_engine(resolved.database_url))

    resolved_store = object_store
    if resolved_store is None:
        if resolved.storage_endpoint_url:
            resolved_store = S3ObjectStore(
                S3Settings(
                    endpoint_url=resolved.storage_endpoint_url,
                    access_key=resolved.storage_access_key or "",
                    secret_key=resolved.storage_secret_key or "",
                    bucket=resolved.storage_bucket,
                    region=resolved.storage_region,
                    force_path_style=resolved.storage_force_path_style,
                    sse=resolved.storage_sse,
                    sse_kms_key_id=resolved.storage_sse_kms_key_id,
                )
            )
        elif not resolved.is_production:
            # Local development/tests: real storage semantics, no service.
            # Production without configured storage keeps the dependency
            # unset so storage endpoints fail loudly with 503.
            resolved_store = MemoryObjectStore()

    resolved_scanner = malware_scanner
    if resolved_scanner is None:
        if resolved.clamav_host:
            resolved_scanner = ClamAvScanner(resolved.clamav_host, resolved.clamav_port)
        elif not resolved.is_production:
            # Settings validation guarantees production always configures
            # a real scanner; the no-op never leaves development/test.
            resolved_scanner = NoopScanner()

    resolved_secret_store = secret_store
    if resolved_secret_store is None:
        # Settings validation already forced a managed backend in
        # production and required each backend's parameters (SEC-005).
        resolved_secret_store = build_secret_store(
            backend=resolved.secrets_backend,
            directory=resolved.secrets_directory,
            aws_region=resolved.secrets_aws_region,
            gcp_project=resolved.secrets_gcp_project,
        )

    deps = Dependencies(
        settings=resolved,
        telemetry=resolved_telemetry,
        oidc_validator=oidc_validator,
        db=resolved_db,
        object_store=resolved_store,
        malware_scanner=resolved_scanner,
        secret_store=resolved_secret_store,
    )
    deps.register_readiness_check("database", resolved_db.ping)
    app.state.dependencies = deps

    # Cross-origin access (SEC-002): strict allowlist only; the settings
    # validator has already refused wildcards. No configured origins
    # means the middleware is not installed at all.
    if resolved.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Dev-User", CORRELATION_HEADER],
            max_age=600,
        )

    security_headers = api_security_headers(is_production=resolved.is_production)

    @app.middleware("http")
    async def security_headers_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        for name, value in security_headers.items():
            response.headers.setdefault(name, value)
        return response

    @app.middleware("http")
    async def correlation_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get(CORRELATION_HEADER)
        with correlation_context(incoming) as correlation_id:
            request.state.correlation_id = correlation_id
            started = time.perf_counter()
            with resolved_telemetry.span(
                f"HTTP {request.method} {request.url.path}",
                attributes={
                    "http.request.method": request.method,
                    "url.path": request.url.path,
                },
            ):
                response = await call_next(request)
            response.headers[CORRELATION_HEADER] = correlation_id
            logger.info(
                "request completed",
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            return response

    register_error_handlers(app, resolved)
    app.include_router(health.router)
    app.include_router(me.router)
    app.include_router(organizations.router)
    app.include_router(jobs.router)
    app.include_router(processes.router)
    app.include_router(catalogs.router)
    app.include_router(providers.router)
    app.include_router(artifacts.router)
    app.include_router(uploads.router)
    app.include_router(documents.router)
    app.include_router(review.router)
    app.include_router(analytics.router)
    app.include_router(audit.router)
    app.include_router(data_exports.router)
    app.include_router(instructions.router)
    app.include_router(integrations.router)
    app.include_router(exports.router)
    app.include_router(public_ingest.router)
    app.include_router(email_intake.router)
    return app
