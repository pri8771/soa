"""Inbound email webhook (ING-014).

POST /v1/inbound-email with the raw MIME message as the request body and
the shared intake secret in ``X-Intake-Secret``. This is the
provider-neutral seam: any inbound provider (or the local Mailpit
poller) that can deliver raw MIME can feed it. Without a configured
secret the endpoint answers 503 — email intake is opt-in, never an
unauthenticated door.
"""

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from soa_api.dependencies import (
    DbSession,
    Dependencies,
    MalwareScannerDep,
    ObjectStoreDep,
    get_dependencies,
)
from soa_api.services.email_intake import (
    EmailLimitError,
    parse_inbound_email,
    process_inbound_email,
)
from soa_api.services.file_limits import FileLimits

router = APIRouter(tags=["public-api"])


@router.post("/v1/inbound-email")
async def inbound_email(
    request: Request,
    session: DbSession,
    store: ObjectStoreDep,
    scanner: MalwareScannerDep,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> dict[str, Any]:
    await deps.rate_limiter.enforce(
        "email_intake",
        request.client.host if request.client else "unknown",
        deps.settings.rate_limit_email_intake_per_minute,
    )
    configured_secret = deps.settings.email_intake_secret
    if configured_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email intake is not configured.",
        )
    provided = request.headers.get("X-Intake-Secret", "")
    if not hmac.compare_digest(provided, configured_secret.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid intake secret."
        )
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0  # Starlette/ASGI still enforces the streamed bound below.
        if declared_bytes > deps.settings.email_intake_max_body_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Inbound message exceeds the configured byte limit.",
            )
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > deps.settings.email_intake_max_body_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Inbound message exceeds the configured byte limit.",
            )
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Empty message body."
        )
    try:
        email = parse_inbound_email(
            bytes(raw),
            max_attachments=deps.settings.email_intake_max_attachments,
            max_total_attachment_bytes=deps.settings.email_intake_max_total_attachment_bytes,
        )
    except EmailLimitError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(error),
        ) from error
    report = await process_inbound_email(
        session,
        email=email,
        store=store,
        scanner=scanner,
        platform_limits=FileLimits(
            max_size_bytes=deps.settings.max_upload_bytes,
            max_pages=deps.settings.max_pages_per_document,
            max_total_pixels=deps.settings.max_total_pixels,
            max_decompressed_bytes=deps.settings.max_decompressed_bytes,
            max_conversion_seconds=deps.settings.max_conversion_seconds,
        ),
    )
    return {
        "outcome": report.outcome,
        "reason": report.reason,
        "attachments": [
            {
                "filename": item.filename,
                "outcome": item.outcome,
                "document_id": item.document_id,
                "state": item.state,
                "detail": item.detail,
            }
            for item in report.attachments
        ],
    }
