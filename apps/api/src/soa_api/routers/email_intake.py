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
from soa_api.services.email_intake import parse_inbound_email, process_inbound_email

router = APIRouter(tags=["public-api"])


@router.post("/v1/inbound-email")
async def inbound_email(
    request: Request,
    session: DbSession,
    store: ObjectStoreDep,
    scanner: MalwareScannerDep,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> dict[str, Any]:
    secret = deps.settings.email_intake_secret
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email intake is not configured.",
        )
    provided = request.headers.get("X-Intake-Secret", "")
    if not hmac.compare_digest(provided, secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid intake secret."
        )
    raw = await request.body()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Empty message body."
        )
    email = parse_inbound_email(raw)
    report = await process_inbound_email(session, email=email, store=store, scanner=scanner)
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
