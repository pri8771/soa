"""Shared complete runtime configuration for integration tests."""

import uuid

from starlette.testclient import TestClient

from soa_api.domain.policies import (
    PolicyType,
    create_policy_draft,
    publish_policy_draft,
)
from soa_api.domain.processes import ProcessRepository, create_draft, publish_draft
from soa_api.domain.rules import create_rule_set_draft, publish_rule_set_draft
from soa_api.domain.schemas import create_schema_draft, publish_schema_draft
from soa_api.domain.streams import (
    StreamRepository,
    create_stream_draft,
    publish_stream_draft,
)
from soa_db import DatabaseSessions
from soa_db.repository import OrganizationContext

SCHEMA = {
    "fields": [
        {
            "key": "po_number",
            "label": "PO number",
            "type": "text",
            "required": True,
            "criticality": "critical",
            "normalization": "identifier",
        }
    ]
}
RULES = {
    "version": "test-1",
    "rules": [
        {
            "key": "required.po_number",
            "severity": "error",
            "action": "block",
            "condition": {"op": "not", "arg": {"op": "is_present", "key": "po_number"}},
            "test_cases": [
                {"values": {}, "expect_triggered": True},
                {"values": {"po_number": "PO-1"}, "expect_triggered": False},
            ],
        }
    ],
}


async def publish_runtime_config(
    client: TestClient,
    db: DatabaseSessions,
    *,
    organization_slug: str = "northstar",
    process_slug: str = "purchase-orders",
    stream_slug: str = "email",
    headers: dict[str, str] | None = None,
    stream_overrides: dict[str, object] | None = None,
) -> uuid.UUID:
    effective_headers = headers or {"X-Dev-User": "user:reviewer"}
    organization_id = uuid.UUID(
        client.get(f"/orgs/{organization_slug}", headers=effective_headers).json()["id"]
    )
    context = OrganizationContext(organization_id=organization_id)
    async with db.session_scope() as session:
        process = await ProcessRepository(session, context).get_by_slug(process_slug)
        stream = await StreamRepository(session, context).get_by_slug(stream_slug)
        assert process is not None and stream is not None
        schema = await create_schema_draft(
            session,
            context,
            process_id=process.id,
            definition=SCHEMA,
            actor_id="test:config",
        )
        await publish_schema_draft(session, context, draft=schema, actor_id="test:config")
        rules = await create_rule_set_draft(
            session,
            context,
            process_id=process.id,
            definition=RULES,
            field_types={"po_number": "text"},
            actor_id="test:config",
        )
        await publish_rule_set_draft(
            session,
            context,
            draft=rules,
            field_types={"po_number": "text"},
            actor_id="test:config",
        )
        provider = await create_policy_draft(
            session,
            context,
            policy_type=PolicyType.PROVIDER,
            definition={"provider_name": "mock", "capabilities": ["ocr", "field_extraction"]},
            actor_id="test:config",
        )
        await publish_policy_draft(session, context, draft=provider, actor_id="test:config")
        confidence = await create_policy_draft(
            session,
            context,
            policy_type=PolicyType.CONFIDENCE,
            definition={"floor": 0.86, "field_overrides": {"po_number": 0.99}},
            actor_id="test:config",
        )
        await publish_policy_draft(session, context, draft=confidence, actor_id="test:config")
        process_version = await create_draft(
            session,
            context,
            process=process,
            definition={
                "schema_version_id": str(schema.id),
                "rule_set_version_id": str(rules.id),
                "provider_policy_version_id": str(provider.id),
                "confidence_policy_version_id": str(confidence.id),
            },
            actor_id="test:config",
        )
        await publish_draft(
            session,
            context,
            process=process,
            draft=process_version,
            actor_id="test:config",
        )
        stream_version = await create_stream_draft(
            session,
            context,
            stream=stream,
            overrides={"languages": ["en"], **(stream_overrides or {})},
            actor_id="test:config",
        )
        await publish_stream_draft(
            session,
            context,
            stream=stream,
            draft=stream_version,
            process_version=process_version,
            actor_id="test:config",
        )
        return stream_version.id
