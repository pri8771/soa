"""Provider administration API (AIO-019).

Serves the shipped provider catalog (the shared soa_config declaration
— one source of truth the worker registry is cross-checked against)
joined with the tenant's published provider policy (CFG-005):

- ``approved`` and ``credential_ref`` come from the policy; the
  credential reference is a NAME, never a secret — CFG-005 validation
  already refuses embedded secrets in policy definitions, and this
  surface serializes nothing secret-shaped;
- ``health`` is honestly ``unknown``: health is worker runtime state,
  and inventing a green dot would be worse than admitting the gap;
- the routing preview applies the AIO-013 elimination semantics over
  the static catalog so an administrator can see how a policy orders
  providers before publishing it.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.policies import PolicyType, PolicyVersionRepository
from soa_config.provider_catalog import (
    PROVIDER_CATALOG,
    catalog_warnings,
    routing_preview,
)

router = APIRouter(tags=["providers"])

VALID_CAPABILITIES = frozenset(entry.capability for entry in PROVIDER_CATALOG) | {
    "native_text",
    "ocr",
    "classify",
    "split",
    "field_extraction",
}

HEALTH_NOTE = (
    "Health is reported by the worker at runtime; a live health surface "
    "arrives with worker telemetry."
)


class RoutingPreviewRequest(BaseModel):
    capability: str
    local_only: bool = False
    language: str | None = None
    allow_third_party_processing: bool = False
    preferred_order: list[str] = Field(default_factory=list)


@router.get("/orgs/{organization_slug}/providers")
async def list_providers(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> dict[str, Any]:
    policy = await PolicyVersionRepository(session, authorized.org_context).get_published(
        PolicyType.PROVIDER
    )
    approved_name = str(policy.definition.get("provider_name", "")) if policy else ""
    credential_ref = str(policy.definition.get("credential_ref") or "") if policy else ""
    items = []
    for entry in PROVIDER_CATALOG:
        approved = entry.name == approved_name
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
                "health": "unknown",
                "approved": approved,
                # A reference NAME only — the credential value has no
                # read path on this surface.
                "credential_ref": credential_ref if approved else None,
            }
        )
    return {"items": items, "health_note": HEALTH_NOTE}


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
