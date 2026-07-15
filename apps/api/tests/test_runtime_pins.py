"""Complete immutable execution pins resolved at intake."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.domain.streams import StreamRepository
from soa_api.services.runtime_pins import resolve_runtime_pins
from soa_api.settings import ApiSettings, Environment
from soa_api.test_support.runtime_config import publish_runtime_config
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.instructions import (
    create_instruction_draft,
    publish_instruction_draft,
)
from soa_db.repository import OrganizationContext

ADMIN = {"X-Dev-User": "user:reviewer"}


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/pins.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    return TestClient(create_app(ApiSettings(environment=Environment.TEST), db=db)), db


async def test_instruction_and_confidence_are_pinned_at_intake(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "Orders", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Email", "slug": "email"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201
    stream_version_id = await publish_runtime_config(client, db)
    organization_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=organization_id)
    async with db.session_scope() as session:
        stream = await StreamRepository(session, context).get_by_slug("email")
        assert stream is not None
        version = await StreamRepository(session, context).get(stream.id)
        assert version is not None
        from soa_api.domain.streams import StreamVersionRepository

        stream_version = await StreamVersionRepository(session, context).get(stream_version_id)
        assert stream_version is not None and stream_version.resolved_snapshot is not None
        schema_id = uuid.UUID(str(stream_version.resolved_snapshot["config"]["schema_version_id"]))
        draft = await create_instruction_draft(
            session,
            context,
            stream_version_id=stream_version_id,
            schema_version_id=schema_id,
            content={
                "instructions": "Extract only values visible in the order.",
                "field_guidance": {"po_number": "Keep leading zeroes."},
            },
            actor_id="test:pins",
        )
        published = await publish_instruction_draft(
            session, context, draft=draft, actor_id="test:pins"
        )

    async with db.session_scope() as session:
        pins = await resolve_runtime_pins(
            session,
            context,
            stream_version_id=stream_version_id,
        )
    assert pins.instruction_version_id == published.id
    assert pins.confidence_policy_version_id is not None
    assert pins.provider_policy_version_id is not None
    assert len(pins.execution_fingerprint) == 64

    # Publishing a replacement produces a new intake contract; the first
    # returned RuntimePins remains an immutable historical value.
    async with db.session_scope() as session:
        second = await create_instruction_draft(
            session,
            context,
            stream_version_id=stream_version_id,
            schema_version_id=schema_id,
            content={"instructions": "Second version", "field_guidance": {}},
            actor_id="test:pins",
        )
        second = await publish_instruction_draft(
            session, context, draft=second, actor_id="test:pins"
        )
        newer = await resolve_runtime_pins(
            session,
            context,
            stream_version_id=stream_version_id,
        )
    assert newer.instruction_version_id == second.id
    assert newer.execution_fingerprint != pins.execution_fingerprint
    assert pins.instruction_version_id == published.id
