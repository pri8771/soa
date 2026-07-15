"""Tenant service-credential administration.

Raw API keys have exactly two response paths: create and rotate. Listing and
revocation return metadata only. Every key is bound to one tenant, one or
more explicit stream UUIDs, and the shipped machine capability allowlist.
"""

import uuid
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.credentials import (
    CredentialStatus,
    ServiceCredential,
    ServiceCredentialRepository,
    create_credential,
    revoke_credential,
    rotate_credential,
)
from soa_api.domain.streams import StreamRepository, StreamStatus
from soa_db.mixins import VersionConflictError

router = APIRouter(tags=["service-credentials"])


def _upload_scope_default() -> list[Literal["documents.upload"]]:
    return ["documents.upload"]


class CredentialCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    scopes: list[Literal["documents.upload"]] = Field(
        default_factory=_upload_scope_default, min_length=1, max_length=1
    )
    allowed_stream_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    expires_in_days: int = Field(default=90, ge=1, le=365)

    @field_validator("name")
    @classmethod
    def name_is_trimmed(cls, value: str) -> str:
        if value != value.strip() or not value.strip():
            raise ValueError("name must be non-empty and trimmed")
        return value

    @field_validator("allowed_stream_ids")
    @classmethod
    def stream_ids_are_unique(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_stream_ids must not contain duplicates")
        return value


class CredentialRotateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_in_days: int = Field(default=90, ge=1, le=365)


class CredentialMetadataResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    scopes: list[str]
    allowed_stream_ids: list[str]
    status: str
    expires_at: str | None
    last_used_at: str | None
    created_by: str
    created_at: str
    updated_at: str
    version: int


class CredentialListResponse(BaseModel):
    items: list[CredentialMetadataResponse]


class CredentialSecretResponse(BaseModel):
    credential: CredentialMetadataResponse
    api_key: str = Field(repr=False)
    warning: str


class CredentialRevokeResponse(BaseModel):
    credential: CredentialMetadataResponse


def _actor(authorized: AuthorizedContext) -> str:
    return f"user:{authorized.membership.user_id}"


def _prevent_secret_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _safe_metadata_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return list(value)


def _serialize(credential: ServiceCredential) -> CredentialMetadataResponse:
    """Serialize metadata. ``key_hash`` has intentionally no response path."""

    return CredentialMetadataResponse(
        id=str(credential.id),
        name=credential.name,
        key_prefix=credential.key_prefix,
        scopes=_safe_metadata_list(credential.scopes),
        allowed_stream_ids=_safe_metadata_list(credential.allowed_stream_ids),
        status=credential.status,
        expires_at=credential.expires_at.isoformat() if credential.expires_at else None,
        last_used_at=credential.last_used_at.isoformat() if credential.last_used_at else None,
        created_by=credential.created_by,
        created_at=credential.created_at.isoformat(),
        updated_at=credential.updated_at.isoformat(),
        version=credential.version,
    )


async def _validate_streams(
    session: DbSession,
    authorized: AuthorizedContext,
    stream_ids: list[uuid.UUID],
) -> None:
    repository = StreamRepository(session, authorized.org_context)
    for stream_id in stream_ids:
        stream = await repository.get(stream_id)
        if stream is None or stream.status == StreamStatus.ARCHIVED:
            # Do not reveal whether the UUID belongs to another tenant.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="One or more allowed streams are invalid for this organization.",
            )


def _expect_version(credential: ServiceCredential, if_match: int | None) -> None:
    if if_match is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match is required for service-credential changes.",
        )
    try:
        credential.expect_version(if_match)
    except VersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


async def _load_for_change(
    session: DbSession,
    authorized: AuthorizedContext,
    credential_id: uuid.UUID,
) -> ServiceCredential:
    credential = await ServiceCredentialRepository(session, authorized.org_context).get(
        credential_id, for_update=True
    )
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Service credential not found."
        )
    return credential


@router.get(
    "/orgs/{organization_slug}/service-credentials",
    response_model=CredentialListResponse,
)
async def list_service_credentials(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("credentials.manage"))],
    session: DbSession,
) -> CredentialListResponse:
    credentials = await ServiceCredentialRepository(session, authorized.org_context).list_all()
    return CredentialListResponse(items=[_serialize(credential) for credential in credentials])


@router.post(
    "/orgs/{organization_slug}/service-credentials",
    status_code=status.HTTP_201_CREATED,
    response_model=CredentialSecretResponse,
)
async def create_service_credential(
    body: CredentialCreateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("credentials.manage"))],
    session: DbSession,
    response: Response,
) -> CredentialSecretResponse:
    await _validate_streams(session, authorized, body.allowed_stream_ids)
    credential, api_key = await create_credential(
        session,
        authorized.org_context,
        name=body.name,
        scopes=list(body.scopes),
        allowed_stream_ids=body.allowed_stream_ids,
        actor_id=_actor(authorized),
        expires_in=timedelta(days=body.expires_in_days),
    )
    await session.flush()
    _prevent_secret_caching(response)
    return CredentialSecretResponse(
        credential=_serialize(credential),
        api_key=api_key,
        warning="Copy this API key now. It cannot be retrieved after this response.",
    )


@router.post(
    "/orgs/{organization_slug}/service-credentials/{credential_id}/rotate",
    response_model=CredentialSecretResponse,
)
async def rotate_service_credential(
    credential_id: uuid.UUID,
    body: CredentialRotateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("credentials.manage"))],
    session: DbSession,
    response: Response,
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> CredentialSecretResponse:
    credential = await _load_for_change(session, authorized, credential_id)
    _expect_version(credential, if_match)
    if credential.status != CredentialStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Revoked service credentials cannot be rotated.",
        )
    if not credential.allowed_stream_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This legacy credential has no stream scope; create a scoped replacement "
                "instead of rotating it."
            ),
        )
    api_key = await rotate_credential(
        session,
        authorized.org_context,
        credential=credential,
        actor_id=_actor(authorized),
        expires_in=timedelta(days=body.expires_in_days),
    )
    await session.flush()
    _prevent_secret_caching(response)
    return CredentialSecretResponse(
        credential=_serialize(credential),
        api_key=api_key,
        warning=(
            "Copy this API key now. It cannot be retrieved after this response; "
            "the previous key stopped working immediately."
        ),
    )


@router.post(
    "/orgs/{organization_slug}/service-credentials/{credential_id}/revoke",
    response_model=CredentialRevokeResponse,
)
async def revoke_service_credential(
    credential_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("credentials.manage"))],
    session: DbSession,
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> CredentialRevokeResponse:
    credential = await _load_for_change(session, authorized, credential_id)
    _expect_version(credential, if_match)
    if credential.status == CredentialStatus.REVOKED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Service credential is already revoked.",
        )
    await revoke_credential(
        session,
        authorized.org_context,
        credential=credential,
        actor_id=_actor(authorized),
    )
    await session.flush()
    return CredentialRevokeResponse(credential=_serialize(credential))
