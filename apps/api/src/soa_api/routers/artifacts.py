"""Artifact reads and signed download authorization (STO-004).

Downloads are never served through the API process: an authorized caller
receives a SHORT-LIVED signed URL for the exact object and nothing else.
Authorization is the same central path as every other endpoint
(``documents.read`` on a membership in the artifact's organization) —
there is deliberately NO ambient support/staff bypass: platform support
reads customer artifacts only through an explicit membership granted by
the customer, which shows up in the member list and the audit trail.
Every issued URL is itself an audit event.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession, Dependencies, ObjectStoreDep, get_dependencies
from soa_db.artifacts import Artifact, ArtifactRepository
from soa_db.audit import ActorType, record_audit_event
from soa_db.documents import DocumentRepository, DocumentState
from soa_storage import ObjectNotFoundError

router = APIRouter(tags=["artifacts"])


class ArtifactResponse(BaseModel):
    """Artifact metadata. The object key stays server-side — clients get
    content only through issued signed URLs."""

    id: str
    document_id: str
    kind: str
    sha256: str
    size_bytes: int
    content_type: str
    produced_by_stage: str | None
    retention_class: str
    created_at: str

    @classmethod
    def from_model(cls, artifact: Artifact) -> "ArtifactResponse":
        return cls(
            id=str(artifact.id),
            document_id=str(artifact.document_id),
            kind=artifact.kind,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            content_type=artifact.content_type,
            produced_by_stage=artifact.produced_by_stage,
            retention_class=artifact.retention_class,
            created_at=artifact.created_at.isoformat(),
        )


class DownloadUrlResponse(BaseModel):
    url: str
    expires_at: str
    method: str


@router.get("/orgs/{organization_slug}/documents/{document_id}/artifacts")
async def list_document_artifacts(
    document_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.read"))],
    session: DbSession,
) -> list[ArtifactResponse]:
    artifacts = await ArtifactRepository(session, authorized.org_context).list_for_document(
        document_id
    )
    return [ArtifactResponse.from_model(a) for a in artifacts]


@router.post("/orgs/{organization_slug}/artifacts/{artifact_id}/download-url")
async def issue_download_url(
    artifact_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.read"))],
    session: DbSession,
    store: ObjectStoreDep,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> DownloadUrlResponse:
    # Abuse control (SEC-003): per-principal cap on signed-URL minting.
    await deps.rate_limiter.enforce(
        "download_urls",
        f"user:{authorized.membership.user_id}",
        deps.settings.rate_limit_download_urls_per_minute,
    )
    # Tenant scope first: an artifact outside the caller's organization is
    # indistinguishable from one that does not exist.
    artifact = await ArtifactRepository(session, authorized.org_context).get(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found.")
    # Quarantined documents' files are unavailable for normal download
    # (ING-004) — dedicated, separately-audited incident tooling is the
    # only path to infected content, and it does not exist yet.
    document = await DocumentRepository(session, authorized.org_context).get(artifact.document_id)
    if document is not None and document.state == DocumentState.QUARANTINED.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This document is quarantined; its files cannot be downloaded.",
        )
    ttl = deps.settings.download_url_ttl_seconds
    try:
        signed = await store.signed_download_url(artifact.object_key, expires_in_seconds=ttl)
    except ObjectNotFoundError:
        # Metadata exists but the object is gone — reconciliation territory
        # (STO-005), and a lie to hand out a URL for.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The stored object for this artifact is missing.",
        ) from None
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=f"user:{authorized.membership.user_id}",
        action="artifact.download_url_issued",
        target_type="artifact",
        target_id=str(artifact.id),
        organization_id=authorized.org_context.organization_id,
        summary={"kind": artifact.kind, "expires_at": signed.expires_at.isoformat()},
    )
    return DownloadUrlResponse(
        url=signed.url, expires_at=signed.expires_at.isoformat(), method=signed.method
    )
