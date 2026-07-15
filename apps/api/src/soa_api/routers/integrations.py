"""Integration and mapping-profile endpoints (EXP-003).

Reads need ``integrations.read``; configuration writes need
``integrations.manage``; the credential write needs
``credentials.manage``. Two hard rules everywhere:

- SECRETS NEVER APPEAR IN A RESPONSE. Integration serializations carry
  only whether a credential is configured and its kind; the credential
  write endpoint acknowledges without echoing.
- Draft edits use optimistic concurrency (If-Match on the record
  version); published mappings are immutable, so editing one is a 409.

Validation runs the REAL engine (EXP-002) against a sample canonical
order — the caller's own sample or the built-in one — and returns the
mapped payload with its full transform trace, so a mapping is proven
before it publishes. Publish refuses definitions with structural
errors. Compare diffs two versions' definitions by target field.
"""

import time
import uuid
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession, SecretStoreDep, SettingsDep
from soa_api.domain.integrations import (
    Integration,
    IntegrationRepository,
    IntegrationStatus,
    InvalidIntegrationTransitionError,
    MappingProfileVersion,
    MappingProfileVersionRepository,
    UnknownIntegrationTypeError,
    create_integration,
    create_mapping_draft,
    credential_secret_for_delivery,
    publish_mapping_draft,
    store_integration_credential,
    transition_integration,
)
from soa_api.domain.versioning import (
    ImmutableVersionError,
    InvalidVersionStateError,
    VersionState,
)
from soa_canonical import CanonicalValidationError, validate_order
from soa_canonical.mapping_engine import (
    MappingDefinitionError,
    MappingExecutionError,
    execute_mapping,
    validate_mapping_definition,
)
from soa_config import SecretNotFoundError
from soa_db.audit import ActorType, record_audit_event
from soa_db.mixins import VersionConflictError
from soa_db.pagination import CursorRequest
from soa_integrations import ConnectionTestRequest, capabilities_for, test_connection

router = APIRouter(tags=["integrations"])

#: The built-in sample canonical order used when a validation request
#: brings no sample of its own. Kept valid against the canonical schema
#: (a test asserts it).
DEFAULT_SAMPLE: dict[str, Any] = {
    "schema_version": "1.0.0",
    "identifiers": {"po_number": "PO-100042"},
    "parties": {"buyer": {"name": "Acme Industrial"}},
    "dates": {"order_date": "2026-03-14"},
    "terms": {"currency": "USD"},
    "totals": {"grand_total": {"amount": "1234.50", "currency": "USD"}},
    "line_items": [
        {
            "line_number": 1,
            "sku": "WID-100",
            "description": "Widget, 10mm",
            "quantity": "10",
            "unit_of_measure": "each",
            "unit_price": {"amount": "45.00", "currency": "USD"},
            "line_total": {"amount": "450.00", "currency": "USD"},
        },
        {
            "line_number": 2,
            "sku": "GAD-205",
            "quantity": "3",
            "line_total": {"amount": "784.50", "currency": "USD"},
        },
    ],
    "source": {
        "document_id": "8a111111-1111-4111-8111-111111111111",
        "run_id": "a2222222-2222-4222-8222-222222222222",
    },
}


def _actor(authorized: AuthorizedContext) -> str:
    return f"user:{authorized.membership.user_id}"


def _serialize_integration(integration: Integration) -> dict[str, Any]:
    """The integration WITHOUT its secret — only that one is configured."""
    capabilities = capabilities_for(integration.integration_type)
    return {
        "id": str(integration.id),
        "name": integration.name,
        "slug": integration.slug,
        "integration_type": integration.integration_type,
        "status": integration.status,
        "endpoint_url": integration.endpoint_url,
        "credential_configured": integration.credential_id is not None,
        "production_ready": capabilities.production_ready,
        "readiness_detail": capabilities.readiness_detail,
        "idempotency_mechanism": capabilities.idempotency_mechanism,
        "active_mapping_version_id": (
            str(integration.active_mapping_version_id)
            if integration.active_mapping_version_id
            else None
        ),
        "version": integration.version,
        "created_at": integration.created_at.isoformat(),
    }


def _serialize_version(record: MappingProfileVersion) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "integration_id": str(record.integration_id),
        "version_number": record.version_number,
        "state": record.state,
        "definition": record.definition,
        "target_schema": record.target_schema,
        "change_summary": record.change_summary,
        "published_at": record.published_at.isoformat() if record.published_at else None,
        "published_by": record.published_by,
        "version": record.version,
    }


async def _load_integration(
    session: DbSession, authorized: AuthorizedContext, slug: str
) -> Integration:
    integration = await IntegrationRepository(session, authorized.org_context).get_by_slug(slug)
    if integration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found.")
    return integration


async def _load_version(
    session: DbSession,
    authorized: AuthorizedContext,
    integration: Integration,
    version_id: uuid.UUID,
) -> MappingProfileVersion:
    record = await MappingProfileVersionRepository(session, authorized.org_context).get(version_id)
    if record is None or record.integration_id != integration.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Mapping version not found."
        )
    return record


class IntegrationCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    integration_type: str = Field(min_length=1, max_length=50)
    endpoint_url: str | None = Field(default=None, max_length=2000)


@router.post("/orgs/{organization_slug}/integrations", status_code=status.HTTP_201_CREATED)
async def create_integration_endpoint(
    body: IntegrationCreateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.manage"))],
    session: DbSession,
) -> dict[str, Any]:
    if await IntegrationRepository(session, authorized.org_context).get_by_slug(body.slug):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That slug is already in use."
        )
    try:
        integration = await create_integration(
            session,
            authorized.org_context,
            name=body.name,
            slug=body.slug,
            integration_type=body.integration_type,
            endpoint_url=body.endpoint_url,
            actor_id=_actor(authorized),
        )
    except UnknownIntegrationTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    return _serialize_integration(integration)


class ConnectionTestResponse(BaseModel):
    ok: bool
    detail: str


class ActivationResponse(BaseModel):
    activated: bool
    detail: str
    integration: dict[str, Any]


async def _connection_test_result(
    *,
    integration: Integration,
    authorized: AuthorizedContext,
    session: DbSession,
    secret_store: SecretStoreDep,
    settings: SettingsDep,
) -> ConnectionTestResponse:
    if integration.status == IntegrationStatus.ARCHIVED:
        return ConnectionTestResponse(
            ok=False,
            detail="Archived integrations are terminal and cannot be tested or activated.",
        )
    if not integration.endpoint_url:
        return ConnectionTestResponse(ok=False, detail="Configure an endpoint before testing.")
    try:
        secret = await credential_secret_for_delivery(
            session,
            authorized.org_context,
            integration=integration,
            secret_store=secret_store,
        )
    except SecretNotFoundError:
        return ConnectionTestResponse(
            ok=False,
            detail="The configured credential is unavailable; rotate it before testing.",
        )
    if secret is None:
        return ConnectionTestResponse(ok=False, detail="Configure a credential before testing.")
    async with httpx.AsyncClient(follow_redirects=False) as client:
        result = await test_connection(
            client,
            integration.integration_type,
            ConnectionTestRequest(
                url=integration.endpoint_url,
                secret=secret,
                business_key=str(integration.id),
                timestamp=int(time.time()),
                allowlist=settings.outbound_destination_allowlist,
            ),
        )
    return ConnectionTestResponse(ok=result.ok, detail=result.detail)


async def _audit_connection_test(
    session: DbSession,
    authorized: AuthorizedContext,
    integration: Integration,
    result: ConnectionTestResponse,
) -> None:
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=_actor(authorized),
        action="integration.connection_tested",
        target_type="integration",
        target_id=str(integration.id),
        organization_id=authorized.organization.id,
        summary={
            "type": integration.integration_type,
            "ok": result.ok,
            "detail": result.detail,
        },
    )


@router.post(
    "/orgs/{organization_slug}/integrations/{integration_slug}/connection-test",
    response_model=ConnectionTestResponse,
)
async def test_integration_connection(
    integration_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("credentials.manage"))],
    session: DbSession,
    secret_store: SecretStoreDep,
    settings: SettingsDep,
) -> ConnectionTestResponse:
    integration = await _load_integration(session, authorized, integration_slug)
    result = await _connection_test_result(
        integration=integration,
        authorized=authorized,
        session=session,
        secret_store=secret_store,
        settings=settings,
    )
    await _audit_connection_test(session, authorized, integration, result)
    return result


def _expect_integration_version(integration: Integration, if_match: int) -> None:
    try:
        integration.expect_version(if_match)
    except VersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.post(
    "/orgs/{organization_slug}/integrations/{integration_slug}/activate",
    response_model=ActivationResponse,
)
async def activate_integration(
    integration_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.manage"))],
    session: DbSession,
    secret_store: SecretStoreDep,
    settings: SettingsDep,
    if_match: Annotated[int, Header(alias="If-Match")],
) -> ActivationResponse:
    if "credentials.manage" not in authorized.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Activation also requires credentials.manage.",
        )
    integration = await _load_integration(session, authorized, integration_slug)
    _expect_integration_version(integration, if_match)
    capabilities = capabilities_for(integration.integration_type)
    if not capabilities.production_ready:
        result = ConnectionTestResponse(ok=False, detail=capabilities.readiness_detail)
    elif integration.active_mapping_version_id is None:
        result = ConnectionTestResponse(
            ok=False, detail="Publish a mapping before activating this integration."
        )
    else:
        result = await _connection_test_result(
            integration=integration,
            authorized=authorized,
            session=session,
            secret_store=secret_store,
            settings=settings,
        )
        await _audit_connection_test(session, authorized, integration, result)
    if not result.ok:
        await record_audit_event(
            session,
            actor_type=ActorType.USER,
            actor_id=_actor(authorized),
            action="integration.activation_refused",
            target_type="integration",
            target_id=str(integration.id),
            organization_id=authorized.organization.id,
            summary={"detail": result.detail},
        )
        return ActivationResponse(
            activated=False,
            detail=result.detail,
            integration=_serialize_integration(integration),
        )
    # The network probe runs without holding a database row lock. Reload and
    # recheck the caller's precondition so a concurrent credential, mapping,
    # or lifecycle change cannot be activated based on stale test results.
    await session.refresh(integration)
    _expect_integration_version(integration, if_match)
    try:
        await transition_integration(
            session,
            authorized.org_context,
            integration=integration,
            requested=IntegrationStatus.ACTIVE,
            actor_id=_actor(authorized),
        )
    except InvalidIntegrationTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return ActivationResponse(
        activated=True,
        detail="Connection verified; integration activated.",
        integration=_serialize_integration(integration),
    )


async def _transition_endpoint(
    *,
    requested: IntegrationStatus,
    integration_slug: str,
    authorized: AuthorizedContext,
    session: DbSession,
    if_match: int,
) -> dict[str, Any]:
    integration = await _load_integration(session, authorized, integration_slug)
    _expect_integration_version(integration, if_match)
    try:
        await transition_integration(
            session,
            authorized.org_context,
            integration=integration,
            requested=requested,
            actor_id=_actor(authorized),
        )
    except InvalidIntegrationTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return _serialize_integration(integration)


@router.post("/orgs/{organization_slug}/integrations/{integration_slug}/deactivate")
async def deactivate_integration(
    integration_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.manage"))],
    session: DbSession,
    if_match: Annotated[int, Header(alias="If-Match")],
) -> dict[str, Any]:
    return await _transition_endpoint(
        requested=IntegrationStatus.PAUSED,
        integration_slug=integration_slug,
        authorized=authorized,
        session=session,
        if_match=if_match,
    )


@router.post("/orgs/{organization_slug}/integrations/{integration_slug}/archive")
async def archive_integration(
    integration_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.manage"))],
    session: DbSession,
    if_match: Annotated[int, Header(alias="If-Match")],
) -> dict[str, Any]:
    return await _transition_endpoint(
        requested=IntegrationStatus.ARCHIVED,
        integration_slug=integration_slug,
        authorized=authorized,
        session=session,
        if_match=if_match,
    )


@router.get("/orgs/{organization_slug}/integrations")
async def list_integrations(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.read"))],
    session: DbSession,
) -> dict[str, Any]:
    rows = await IntegrationRepository(session, authorized.org_context).list_page(
        CursorRequest(limit=100)
    )
    return {"items": [_serialize_integration(row) for row in rows.items]}


@router.get("/orgs/{organization_slug}/integrations/{integration_slug}")
async def get_integration(
    integration_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.read"))],
    session: DbSession,
) -> dict[str, Any]:
    integration = await _load_integration(session, authorized, integration_slug)
    versions = await MappingProfileVersionRepository(
        session, authorized.org_context
    ).list_for_integration(integration.id)
    return {
        "integration": _serialize_integration(integration),
        "mapping_versions": [_serialize_version(record) for record in versions],
    }


class CredentialRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=50)
    secret: str = Field(min_length=8, max_length=4000)


@router.put("/orgs/{organization_slug}/integrations/{integration_slug}/credential")
async def set_credential(
    integration_slug: str,
    body: CredentialRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("credentials.manage"))],
    session: DbSession,
    secret_store: SecretStoreDep,
) -> dict[str, Any]:
    """Store or rotate the credential. The VALUE goes to the secret
    store (SEC-005); the database keeps a reference. The response
    acknowledges — it NEVER echoes the secret, and neither does any
    other endpoint."""
    integration = await _load_integration(session, authorized, integration_slug)
    rotated = integration.credential_id is not None
    await store_integration_credential(
        session,
        authorized.org_context,
        integration=integration,
        kind=body.kind,
        secret=body.secret,
        actor_id=_actor(authorized),
        secret_store=secret_store,
    )
    return {"credential_configured": True, "kind": body.kind, "rotated": rotated}


class MappingDraftRequest(BaseModel):
    definition: dict[str, Any] = Field(default_factory=dict)
    target_schema: dict[str, Any] = Field(default_factory=dict)
    change_summary: str | None = Field(default=None, max_length=500)


@router.post(
    "/orgs/{organization_slug}/integrations/{integration_slug}/mapping-versions",
    status_code=status.HTTP_201_CREATED,
)
async def create_mapping_version(
    integration_slug: str,
    body: MappingDraftRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.manage"))],
    session: DbSession,
) -> dict[str, Any]:
    integration = await _load_integration(session, authorized, integration_slug)
    draft = await create_mapping_draft(
        session,
        authorized.org_context,
        integration=integration,
        definition=body.definition,
        target_schema=body.target_schema,
        change_summary=body.change_summary,
        actor_id=_actor(authorized),
    )
    return _serialize_version(draft)


@router.patch(
    "/orgs/{organization_slug}/integrations/{integration_slug}/mapping-versions/{version_id}"
)
async def update_mapping_draft(
    integration_slug: str,
    version_id: uuid.UUID,
    body: MappingDraftRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.manage"))],
    session: DbSession,
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    integration = await _load_integration(session, authorized, integration_slug)
    record = await _load_version(session, authorized, integration, version_id)
    if record.state != VersionState.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Published mappings are immutable; create a new draft.",
        )
    try:
        if if_match is not None:
            record.expect_version(if_match)
        record.definition = dict(body.definition)
        record.target_schema = dict(body.target_schema)
        if body.change_summary is not None:
            record.change_summary = body.change_summary
        await session.flush()
    except VersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return _serialize_version(record)


class ValidateRequest(BaseModel):
    #: A canonical order to run the mapping against; the built-in sample
    #: is used when omitted.
    sample: dict[str, Any] | None = None


@router.post(
    "/orgs/{organization_slug}/integrations/{integration_slug}"
    "/mapping-versions/{version_id}/validate"
)
async def validate_mapping_version(
    integration_slug: str,
    version_id: uuid.UUID,
    body: ValidateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.read"))],
    session: DbSession,
) -> dict[str, Any]:
    """Structural validation plus a REAL execution over a sample order:
    the mapped payload and its transform trace come back so the operator
    sees exactly what the integration would receive."""
    integration = await _load_integration(session, authorized, integration_slug)
    record = await _load_version(session, authorized, integration, version_id)
    definition_errors = validate_mapping_definition(record.definition)
    if definition_errors:
        return {"valid": False, "errors": definition_errors, "payload": None, "trace": []}
    sample = body.sample if body.sample is not None else DEFAULT_SAMPLE
    try:
        validate_order(sample)
    except CanonicalValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The sample is not a valid canonical order: {exc}",
        ) from None
    try:
        result = execute_mapping(
            record.definition, sample, target_schema=record.target_schema or None
        )
    except MappingExecutionError as exc:
        return {"valid": False, "errors": exc.errors, "payload": None, "trace": []}
    except MappingDefinitionError as exc:  # defensive: validated above
        return {"valid": False, "errors": exc.errors, "payload": None, "trace": []}
    return {
        "valid": True,
        "errors": [],
        "payload": result.payload,
        "trace": [entry.to_json() for entry in result.trace],
        "notes": result.notes,
    }


@router.post(
    "/orgs/{organization_slug}/integrations/{integration_slug}"
    "/mapping-versions/{version_id}/publish"
)
async def publish_mapping_version(
    integration_slug: str,
    version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.manage"))],
    session: DbSession,
) -> dict[str, Any]:
    integration = await _load_integration(session, authorized, integration_slug)
    record = await _load_version(session, authorized, integration, version_id)
    definition_errors = validate_mapping_definition(record.definition)
    if definition_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"errors": definition_errors},
        )
    try:
        published = await publish_mapping_draft(
            session,
            authorized.org_context,
            integration=integration,
            draft=record,
            actor_id=_actor(authorized),
        )
    except (InvalidVersionStateError, ImmutableVersionError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return _serialize_version(published)


def _field_index(definition: dict[str, Any]) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for spec in definition.get("fields", []):
        if isinstance(spec, dict) and isinstance(spec.get("target"), str):
            index[spec["target"]] = spec
    for spec in definition.get("constants", []):
        if isinstance(spec, dict) and isinstance(spec.get("target"), str):
            index[spec["target"]] = spec
    for spec in (definition.get("lines") or {}).get("fields", []):
        if isinstance(spec, dict) and isinstance(spec.get("target"), str):
            index[f"lines.{spec['target']}"] = spec
    return index


@router.get("/orgs/{organization_slug}/integrations/{integration_slug}/mapping-versions/compare")
async def compare_mapping_versions(
    integration_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.read"))],
    session: DbSession,
    from_version: Annotated[uuid.UUID, Query(alias="from")],
    to_version: Annotated[uuid.UUID, Query(alias="to")],
) -> dict[str, Any]:
    """Target-field diff between two versions: added, removed, changed
    (with both specs — mapping definitions carry no secrets)."""
    integration = await _load_integration(session, authorized, integration_slug)
    older = await _load_version(session, authorized, integration, from_version)
    newer = await _load_version(session, authorized, integration, to_version)
    old_index = _field_index(older.definition)
    new_index = _field_index(newer.definition)
    added = sorted(set(new_index) - set(old_index))
    removed = sorted(set(old_index) - set(new_index))
    changed = sorted(
        target
        for target in set(old_index) & set(new_index)
        if old_index[target] != new_index[target]
    )
    return {
        "from": {"id": str(older.id), "version_number": older.version_number},
        "to": {"id": str(newer.id), "version_number": newer.version_number},
        "added": added,
        "removed": removed,
        "changed": [
            {"target": target, "before": old_index[target], "after": new_index[target]}
            for target in changed
        ],
        "target_schema_changed": older.target_schema != newer.target_schema,
    }
