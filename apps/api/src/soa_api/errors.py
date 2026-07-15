"""Structured error envelope and exception handlers.

Every error response uses one JSON shape so clients and telemetry can rely on
it. Production responses never expose stack traces or internal exception
text; a correlation ID lets operators find the full detail in logs.
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException

from soa_api.settings import ApiSettings

CORRELATION_HEADER = "X-Request-ID"

_STATUS_CODES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "validation_error",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
}


def error_body(
    *,
    code: str,
    message: str,
    correlation_id: str | None,
    details: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "correlation_id": correlation_id,
        }
    }
    if details is not None:
        body["error"]["details"] = details
    return body


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def register_error_handlers(app: FastAPI, settings: ApiSettings) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                code=code,
                message=str(exc.detail),
                correlation_id=_correlation_id(request),
            ),
            # Preserve semantic headers (Retry-After, WWW-Authenticate, …) —
            # the envelope changes the body, never the HTTP contract.
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "location": [str(part) for part in err.get("loc", ())],
                "message": err.get("msg", "invalid value"),
                "type": err.get("type", "validation_error"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=error_body(
                code="validation_error",
                message="Request validation failed.",
                correlation_id=_correlation_id(request),
                details=details,
            ),
        )

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        # Check-then-insert races (duplicate slug, version number, dedupe
        # key) surface as a client-visible conflict, not a 500.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=error_body(
                code="conflict",
                message="The change conflicts with concurrent activity; retry the request.",
                correlation_id=_correlation_id(request),
            ),
        )

    @app.exception_handler(StaleDataError)
    async def handle_stale_data_error(request: Request, exc: StaleDataError) -> JSONResponse:
        # Optimistic concurrency lost the race: a read-then-flush on a
        # VersionedMixin record (deletion, approval, cancel, reprocess, …)
        # found the row already advanced by a concurrent writer. That is a
        # client-visible conflict to retry, not a 500.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=error_body(
                code="conflict",
                message="The document changed concurrently; retry the request.",
                correlation_id=_correlation_id(request),
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Staging is production-shaped: internals only leak in dev/test.
        if settings.is_development_like:
            message = f"{type(exc).__name__}: {exc}"
        else:
            message = "An internal error occurred."
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body(
                code="internal_error",
                message=message,
                correlation_id=_correlation_id(request),
            ),
        )
