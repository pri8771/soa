"""Application factory."""

import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

import soa_api
from soa_api.dependencies import Dependencies
from soa_api.errors import CORRELATION_HEADER, register_error_handlers
from soa_api.routers import health
from soa_api.settings import ApiSettings, load_settings


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    """Create the API application.

    Settings validate on load, so a misconfigured process fails at startup
    instead of serving with unsafe defaults.
    """
    resolved = settings if settings is not None else load_settings()

    app = FastAPI(
        title="SOA API",
        version=soa_api.__version__,
        docs_url="/docs" if not resolved.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not resolved.is_production else None,
    )
    app.state.dependencies = Dependencies(settings=resolved)

    @app.middleware("http")
    async def correlation_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        correlation_id = request.headers.get(CORRELATION_HEADER) or uuid.uuid4().hex
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = correlation_id
        return response

    register_error_handlers(app, resolved)
    app.include_router(health.router)
    return app
