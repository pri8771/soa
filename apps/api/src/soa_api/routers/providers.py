"""Provider administration API (AIO-019).

Serves the shipped provider catalog (the shared soa_config declaration
— one source of truth the worker registry is cross-checked against)
joined with the tenant's published provider policy (CFG-005):

- approval and credential-configured state come from the policy, while
  secret values and secret-store references have no response path;
- ``health`` and bounded aggregate metrics come from durable worker attempt
  telemetry shared across replicas; an adapter with no recent calls remains
  honestly ``unknown`` rather than receiving an invented green dot;
- the routing preview applies the AIO-013 elimination semantics over
  the static catalog so an administrator can see how a policy orders
  providers before publishing it.
"""

import copy
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession, SecretStoreDep, SettingsDep
from soa_api.domain.policies import (
    PolicyType,
    PolicyValidationError,
    PolicyVersion,
    PolicyVersionRepository,
    create_policy_draft,
    ensure_managed_provider_credentials_live,
    policy_references_credential,
    publish_policy_draft,
    validate_policy,
    validate_provider_capabilities,
)
from soa_api.domain.versioning import InvalidVersionStateError
from soa_config import SecretStoreError
from soa_config.provider_catalog import (
    PROVIDER_CATALOG,
    catalog_warnings,
    routing_preview,
)
from soa_db.audit import ActorType, record_audit_event
from soa_db.provider_credentials import (
    ProviderCredential,
    ProviderCredentialConflictError,
    ProviderCredentialRepository,
    credential_reference_is_live,
    revoke_provider_credential,
    store_provider_credential,
)
from soa_db.provider_metrics import ProviderRuntimeMetricRepository, provider_health

router = APIRouter(tags=["providers"])

VALID_CAPABILITIES = frozenset(entry.capability for entry in PROVIDER_CATALOG) | {
    "native_text",
    "ocr",
    "classify",
    "split",
    "field_extraction",
}

CATALOG_BY_NAME = {entry.name: entry for entry in PROVIDER_CATALOG}
ADMIN_POLICY_TYPES = frozenset({PolicyType.PROVIDER, PolicyType.CONFIDENCE})
PROVIDER_POLICY_FIELDS = frozenset(
    {
        "provider_name",
        "credential_id",
        "credential_ref",  # persisted server-side only
        "capabilities",
        "fallback_providers",
        "estimated_cost_cents",
        "evaluated_quality_score",
        "local_only",
        "allow_third_party_processing",
        "allow_content_retention",
        "allow_training_on_content",
        "allowed_regions",
        "budget_cents",
        "min_quality",
    }
)
PROVIDER_ENTRY_FIELDS = frozenset(
    {
        "provider_name",
        "credential_id",
        "credential_ref",
        "estimated_cost_cents",
        "evaluated_quality_score",
    }
)

HEALTH_NOTE = (
    "Health is derived from persisted worker attempts; providers with no recent "
    "attempts remain unknown, and an open circuit becomes probe-eligible after cooldown."
)


class RoutingPreviewRequest(BaseModel):
    capability: str
    local_only: bool = False
    language: str | None = None
    allow_third_party_processing: bool = False
    preferred_order: list[str] = Field(default_factory=list)


class ProviderCredentialRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    kind: Literal["api_key"] = "api_key"
    secret: SecretStr = Field(min_length=8, max_length=10_000)

    @field_validator("label")
    @classmethod
    def label_is_trimmed(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("label must be non-empty and trimmed")
        return value

    @field_validator("secret")
    @classmethod
    def secret_is_not_whitespace(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("credential value cannot be blank")
        return value


class ProviderCredentialRevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    force: bool = False
    confirmation: str | None = Field(default=None, max_length=20)

    @field_validator("reason")
    @classmethod
    def reason_is_trimmed(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("reason must be non-empty and trimmed")
        return value

    @model_validator(mode="after")
    def force_requires_explicit_confirmation(self) -> "ProviderCredentialRevokeRequest":
        if self.force and self.confirmation != "REVOKE":
            raise ValueError("force revocation requires confirmation='REVOKE'")
        return self


class PolicyDraftRequest(BaseModel):
    definition: dict[str, Any]
    change_summary: str | None = Field(default=None, max_length=500)


def _actor(authorized: AuthorizedContext) -> str:
    return f"user:{authorized.membership.user_id}"


def _require_additional_permission(authorized: AuthorizedContext, permission: str) -> None:
    if permission not in authorized.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Provider administration also requires {permission}.",
        )


def _serialize_credential(credential: ProviderCredential) -> dict[str, Any]:
    """Credential metadata only: no value and no secret-store reference."""

    return {
        "id": str(credential.id),
        "provider_name": credential.provider_name,
        "label": credential.label,
        "kind": credential.kind,
        "status": credential.status,
        "created_by": credential.created_by,
        "created_at": credential.created_at.isoformat(),
        "superseded_at": (
            credential.superseded_at.isoformat() if credential.superseded_at else None
        ),
        "revoked_at": credential.revoked_at.isoformat() if credential.revoked_at else None,
        "revocation_reason": credential.revocation_reason,
    }


def _safe_policy_definition(node: Any) -> Any:
    """Remove the opaque store reference from every admin read response."""

    if isinstance(node, dict):
        return {
            key: _safe_policy_definition(value)
            for key, value in node.items()
            if key != "credential_ref"
        }
    if isinstance(node, list):
        return [_safe_policy_definition(value) for value in node]
    return node


def _serialize_policy(policy: PolicyVersion) -> dict[str, Any]:
    return {
        "id": str(policy.id),
        "policy_type": policy.policy_type,
        "version_number": policy.version_number,
        "state": policy.state,
        "definition": _safe_policy_definition(policy.definition),
        "change_summary": policy.change_summary,
        "published_at": policy.published_at.isoformat() if policy.published_at else None,
        "published_by": policy.published_by,
        "version": policy.version,
    }


def _admin_policy_type(policy_type: PolicyType) -> PolicyType:
    if policy_type not in ADMIN_POLICY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Only provider and confidence policies are administered here.",
        )
    return policy_type


def _provider_nodes(definition: dict[str, Any]) -> list[dict[str, Any]]:
    fallbacks = definition.get("fallback_providers", [])
    if not isinstance(fallbacks, list):
        raise PolicyValidationError("fallback_providers must be an ordered list")
    if not all(isinstance(item, dict) for item in fallbacks):
        raise PolicyValidationError("every fallback provider must be an object")
    return [definition, *fallbacks]


def _reject_client_secret_references(node: Any, *, path: str = "$") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "credential_ref":
                raise PolicyValidationError(
                    f"{path}.credential_ref is server-bound; submit credential_id only"
                )
            _reject_client_secret_references(value, path=f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _reject_client_secret_references(value, path=f"{path}[{index}]")


def _catalog_policy_findings(
    definition: dict[str, Any], *, allow_mock: bool
) -> list[dict[str, str]]:
    unknown = sorted(set(definition) - PROVIDER_POLICY_FIELDS)
    if unknown:
        raise PolicyValidationError(f"provider policy has unknown fields: {unknown}")
    nodes = _provider_nodes(definition)
    warnings: list[dict[str, str]] = []
    hosted_regions: set[str] = set()
    local_only = definition.get("local_only", False)
    for index, node in enumerate(nodes):
        unknown_entry = sorted(
            set(node) - (PROVIDER_POLICY_FIELDS if index == 0 else PROVIDER_ENTRY_FIELDS)
        )
        if unknown_entry:
            location = "provider" if index == 0 else f"fallback_providers[{index - 1}]"
            raise PolicyValidationError(f"{location} has unknown fields: {unknown_entry}")
        name = node.get("provider_name")
        entry = CATALOG_BY_NAME.get(name) if isinstance(name, str) else None
        location = "provider" if index == 0 else f"fallback_providers[{index - 1}]"
        if entry is None:
            raise PolicyValidationError(f"{location} is not a shipped provider")
        if entry.capability != "field_extraction":
            raise PolicyValidationError(
                f"{location} {entry.name!r} cannot perform field extraction"
            )
        if entry.name == "mock" and not allow_mock:
            raise PolicyValidationError(
                "the deterministic mock provider cannot publish outside development"
            )
        credential_required = "tenant_credential" in entry.availability
        has_binding = (
            node.get("credential_id") is not None and node.get("credential_ref") is not None
        )
        if credential_required and not has_binding:
            raise PolicyValidationError(f"{location} {entry.name!r} requires a managed credential")
        if not credential_required and (
            node.get("credential_id") is not None or node.get("credential_ref") is not None
        ):
            raise PolicyValidationError(
                f"{location} {entry.name!r} does not accept a tenant credential"
            )
        if local_only and not entry.local:
            raise PolicyValidationError(f"{location} violates the local_only policy")
        if entry.sends_content_to_third_party:
            hosted_regions.add(entry.processing_region)
            if definition.get("allow_third_party_processing") is not True:
                raise PolicyValidationError(
                    f"{location} requires explicit allow_third_party_processing=true"
                )
        if entry.retains_content and definition.get("allow_content_retention") is not True:
            raise PolicyValidationError(f"{location} requires explicit content-retention approval")
        if (
            entry.uses_content_for_training
            and definition.get("allow_training_on_content") is not True
        ):
            raise PolicyValidationError(f"{location} requires explicit training approval")
        if "endpoint" in entry.availability:
            warnings.append(
                {
                    "level": "warning",
                    "path": location,
                    "message": f"{entry.name} also requires a deployment-managed endpoint/model.",
                }
            )
    if hosted_regions:
        raw_regions = definition.get("allowed_regions")
        if not isinstance(raw_regions, list) or not hosted_regions.issubset(set(raw_regions)):
            raise PolicyValidationError(
                f"allowed_regions must explicitly include hosted regions {sorted(hosted_regions)}"
            )
    return warnings


async def _bind_provider_credentials(
    session: DbSession,
    authorized: AuthorizedContext,
    definition: dict[str, Any],
) -> dict[str, Any]:
    _reject_client_secret_references(definition)
    bound = copy.deepcopy(definition)
    for index, node in enumerate(_provider_nodes(bound)):
        name = node.get("provider_name")
        entry = CATALOG_BY_NAME.get(name) if isinstance(name, str) else None
        location = "provider" if index == 0 else f"fallback_providers[{index - 1}]"
        if entry is None:
            raise PolicyValidationError(f"{location} is not a shipped provider")
        raw_credential_id = node.get("credential_id")
        if raw_credential_id is None:
            continue
        try:
            credential_id = uuid.UUID(str(raw_credential_id))
        except ValueError as error:
            raise PolicyValidationError(f"{location}.credential_id must be a UUID") from error
        credential = await credential_reference_is_live(
            session,
            authorized.org_context,
            credential_id=credential_id,
            provider_name=entry.name,
            require_current=True,
        )
        if credential is None:
            raise PolicyValidationError(
                f"{location} does not reference the current live credential for {entry.name!r}"
            )
        node["credential_id"] = str(credential.id)
        node["credential_ref"] = credential.secret_reference
    return bound


async def _validate_admin_policy(
    session: DbSession,
    authorized: AuthorizedContext,
    *,
    policy_type: PolicyType,
    definition: dict[str, Any],
    allow_mock: bool,
) -> list[dict[str, str]]:
    validate_policy(policy_type, definition)
    warnings: list[dict[str, str]] = []
    if policy_type == PolicyType.PROVIDER:
        validate_provider_capabilities(definition)
        warnings.extend(_catalog_policy_findings(definition, allow_mock=allow_mock))
        await ensure_managed_provider_credentials_live(
            session,
            authorized.org_context,
            definition=definition,
            require_current=True,
        )
    return warnings


async def _load_policy(
    session: DbSession,
    authorized: AuthorizedContext,
    *,
    policy_type: PolicyType,
    policy_id: uuid.UUID,
) -> PolicyVersion:
    policy = await PolicyVersionRepository(session, authorized.org_context).get(policy_id)
    if policy is None or policy.policy_type != policy_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Policy version not found."
        )
    return policy


@router.get("/orgs/{organization_slug}/providers")
async def list_providers(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> dict[str, Any]:
    policy = await PolicyVersionRepository(session, authorized.org_context).get_published(
        PolicyType.PROVIDER
    )
    approved_credentials: dict[str, dict[str, Any]] = {}
    if policy is not None:
        primary = policy.definition.get("provider_name")
        if isinstance(primary, str) and primary:
            reference = policy.definition.get("credential_ref")
            approved_credentials[primary] = {
                "configured": bool(reference),
                "credential_id": policy.definition.get("credential_id"),
            }
        fallbacks = policy.definition.get("fallback_providers", [])
        if isinstance(fallbacks, list):
            for raw in fallbacks:
                if not isinstance(raw, dict) or not isinstance(raw.get("provider_name"), str):
                    continue
                reference = raw.get("credential_ref")
                approved_credentials[str(raw["provider_name"])] = {
                    "configured": bool(reference),
                    "credential_id": raw.get("credential_id"),
                }
    metrics = await ProviderRuntimeMetricRepository(session, authorized.org_context).list_all()
    by_provider_capability = {(metric.provider, metric.capability): metric for metric in metrics}
    items = []
    for entry in PROVIDER_CATALOG:
        approved = entry.name in approved_credentials
        metric = by_provider_capability.get((entry.name, entry.capability))
        items.append(
            {
                "name": entry.name,
                "capability": entry.capability,
                "languages": list(entry.languages),
                "region": entry.processing_region,
                "local": entry.local,
                "data_policy": {
                    "sends_content_to_third_party": entry.sends_content_to_third_party,
                    "retains_content": entry.retains_content,
                    "uses_content_for_training": entry.uses_content_for_training,
                },
                "warnings": list(catalog_warnings(entry)),
                "availability": entry.availability,
                "description": entry.description,
                "health": provider_health(metric) if metric is not None else "unknown",
                "metrics": (
                    {
                        "attempts": metric.attempt_count,
                        "successes": metric.success_count,
                        "failures": metric.failure_count,
                        "fallbacks": metric.fallback_count,
                        "consecutive_failures": metric.consecutive_failures,
                        "mean_latency_ms": (
                            round(metric.total_latency_ms / metric.attempt_count, 2)
                            if metric.attempt_count
                            else None
                        ),
                        "mean_cost_cents": (
                            round(metric.total_cost_cents / metric.attempt_count, 2)
                            if metric.attempt_count
                            else None
                        ),
                        "mean_confidence": (
                            round(metric.mean_quality, 4)
                            if metric.mean_quality is not None
                            else None
                        ),
                        "last_attempt_at": metric.last_attempt_at.isoformat(),
                    }
                    if metric is not None
                    else None
                ),
                "approved": approved,
                "credential_configured": (
                    bool(approved_credentials[entry.name]["configured"]) if approved else False
                ),
                # Credential ids are metadata, but only credential managers
                # need them to build a new server-bound policy draft.
                "credential_id": (
                    approved_credentials[entry.name].get("credential_id")
                    if approved and "credentials.manage" in authorized.permissions
                    else None
                ),
            }
        )
    return {"items": items, "health_note": HEALTH_NOTE}


@router.get("/orgs/{organization_slug}/provider-credentials")
async def list_provider_credentials(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("credentials.manage"))],
    session: DbSession,
) -> dict[str, Any]:
    rows = await ProviderCredentialRepository(session, authorized.org_context).list_all()
    return {"items": [_serialize_credential(row) for row in rows]}


@router.put("/orgs/{organization_slug}/providers/{provider_name}/credential")
async def set_provider_credential(
    provider_name: str,
    body: ProviderCredentialRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("credentials.manage"))],
    session: DbSession,
    secret_store: SecretStoreDep,
) -> dict[str, Any]:
    _require_additional_permission(authorized, "streams.manage")
    entry = CATALOG_BY_NAME.get(provider_name)
    if entry is None or "tenant_credential" not in entry.availability:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="That provider does not accept a tenant-managed credential.",
        )
    try:
        credential, previous_id = await store_provider_credential(
            session,
            authorized.org_context,
            provider_name=provider_name,
            label=body.label,
            kind=body.kind,
            secret=body.secret.get_secret_value(),
            actor_id=_actor(authorized),
            secret_store=secret_store,
        )
    except SecretStoreError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The credential store could not persist this value.",
        ) from None
    return {
        "credential": _serialize_credential(credential),
        "rotated": previous_id is not None,
        "retained_credential_id": str(previous_id) if previous_id is not None else None,
        "detail": (
            "The prior value is retained for immutable policy/run pins until explicit revocation."
            if previous_id is not None
            else "Credential stored. The secret value has no read endpoint."
        ),
    }


@router.post("/orgs/{organization_slug}/provider-credentials/{credential_id}/revoke")
async def revoke_provider_credential_endpoint(
    credential_id: uuid.UUID,
    body: ProviderCredentialRevokeRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("credentials.manage"))],
    session: DbSession,
) -> dict[str, Any]:
    _require_additional_permission(authorized, "streams.manage")
    credential = await ProviderCredentialRepository(session, authorized.org_context).get(
        credential_id
    )
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Provider credential not found.",
        )
    policies = await PolicyVersionRepository(session, authorized.org_context).list_for_type(
        PolicyType.PROVIDER
    )
    referenced_policy_ids = [
        policy.id
        for policy in policies
        if policy_references_credential(
            policy,
            credential_id=credential.id,
            secret_reference=credential.secret_reference,
        )
    ]
    try:
        revoked = await revoke_provider_credential(
            session,
            authorized.org_context,
            credential=credential,
            reason=body.reason,
            actor_id=_actor(authorized),
            referenced_policy_ids=referenced_policy_ids,
            force=body.force,
        )
    except ProviderCredentialConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None
    return {
        "credential": _serialize_credential(revoked),
        "revocation_queued": True,
        "affected_policy_ids": [str(item) for item in referenced_policy_ids],
        "detail": (
            "Break-glass revocation queued; affected immutable executions will fail closed."
            if body.force and referenced_policy_ids
            else "Revocation queued for durable post-commit execution."
        ),
    }


@router.get("/orgs/{organization_slug}/policies/{policy_type}")
async def list_policy_versions(
    policy_type: PolicyType,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> dict[str, Any]:
    policy_type = _admin_policy_type(policy_type)
    rows = await PolicyVersionRepository(session, authorized.org_context).list_for_type(policy_type)
    return {"items": [_serialize_policy(row) for row in rows]}


@router.post(
    "/orgs/{organization_slug}/policies/{policy_type}/drafts",
    status_code=status.HTTP_201_CREATED,
)
async def create_policy_draft_endpoint(
    policy_type: PolicyType,
    body: PolicyDraftRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    policy_type = _admin_policy_type(policy_type)
    try:
        definition = (
            await _bind_provider_credentials(session, authorized, body.definition)
            if policy_type == PolicyType.PROVIDER
            else copy.deepcopy(body.definition)
        )
        await _validate_admin_policy(
            session,
            authorized,
            policy_type=policy_type,
            definition=definition,
            allow_mock=settings.is_development_like,
        )
        draft = await create_policy_draft(
            session,
            authorized.org_context,
            policy_type=policy_type,
            definition=definition,
            change_summary=body.change_summary,
            actor_id=_actor(authorized),
        )
    except PolicyValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from None
    return _serialize_policy(draft)


@router.post("/orgs/{organization_slug}/policies/{policy_type}/{policy_id}/validate")
async def validate_policy_version_endpoint(
    policy_type: PolicyType,
    policy_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    policy_type = _admin_policy_type(policy_type)
    policy = await _load_policy(
        session,
        authorized,
        policy_type=policy_type,
        policy_id=policy_id,
    )
    if policy.state != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only mutable drafts are validated; published policy versions are immutable.",
        )
    findings: list[dict[str, str]] = []
    try:
        findings = await _validate_admin_policy(
            session,
            authorized,
            policy_type=policy_type,
            definition=policy.definition,
            allow_mock=settings.is_development_like,
        )
    except PolicyValidationError as error:
        findings.append({"level": "error", "path": "definition", "message": str(error)})
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=_actor(authorized),
        action="policy.draft_validated",
        target_type="policy_version",
        target_id=str(policy.id),
        organization_id=authorized.organization.id,
        summary={
            "policy_type": policy.policy_type,
            "version_number": policy.version_number,
            "valid": not any(item["level"] == "error" for item in findings),
        },
    )
    return {
        "valid": not any(item["level"] == "error" for item in findings),
        "findings": findings,
    }


@router.post("/orgs/{organization_slug}/policies/{policy_type}/{policy_id}/publish")
async def publish_policy_version_endpoint(
    policy_type: PolicyType,
    policy_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    policy_type = _admin_policy_type(policy_type)
    policy = await _load_policy(
        session,
        authorized,
        policy_type=policy_type,
        policy_id=policy_id,
    )
    try:
        await _validate_admin_policy(
            session,
            authorized,
            policy_type=policy_type,
            definition=policy.definition,
            allow_mock=settings.is_development_like,
        )
        published = await publish_policy_draft(
            session,
            authorized.org_context,
            draft=policy,
            actor_id=_actor(authorized),
        )
    except PolicyValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from None
    except InvalidVersionStateError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None
    return _serialize_policy(published)


@router.post("/orgs/{organization_slug}/providers/routing-preview")
async def preview_routing(
    body: RoutingPreviewRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
) -> dict[str, Any]:
    if body.capability not in VALID_CAPABILITIES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"unknown capability {body.capability!r}",
        )
    order, explanation = routing_preview(
        body.capability,
        local_only=body.local_only,
        language=body.language,
        allow_third_party_processing=body.allow_third_party_processing,
        preferred_order=tuple(body.preferred_order),
    )
    return {"order": list(order), "explanation": list(explanation)}
