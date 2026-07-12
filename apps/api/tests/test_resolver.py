"""Configuration resolver tests (CFG-006): inheritance matrix, override
reset, deterministic fingerprints, provenance."""

import uuid
from pathlib import Path

import pytest

from soa_api.domain.policies import PolicyType, create_policy_draft, publish_policy_draft
from soa_api.domain.processes import create_draft, create_process, publish_draft
from soa_api.domain.resolver import (
    effective_config,
    fingerprint,
    resolve_configuration,
)
from soa_api.domain.versioning import InvalidVersionStateError
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext

ORG_A = OrganizationContext(organization_id=uuid.UUID(int=0xA))
ACTOR = "user:test-admin"


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/resolver.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_inheritance_matrix_with_provenance_and_fingerprint(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        process = await create_process(session, ORG_A, name="POs", slug="pos", actor_id=ACTOR)
        version = await create_draft(
            session,
            ORG_A,
            process=process,
            # overrides environment's confidence_floor; adds provider.
            definition={"confidence_floor": 0.9, "provider": "acme"},
            actor_id=ACTOR,
        )
        await publish_draft(session, ORG_A, process=process, draft=version, actor_id=ACTOR)
        policy = await create_policy_draft(
            session,
            ORG_A,
            policy_type=PolicyType.RETENTION,
            definition={"document_days": 90},
            actor_id=ACTOR,
        )
        await publish_policy_draft(session, ORG_A, draft=policy, actor_id=ACTOR)

        resolved = resolve_configuration(
            process_version=version,
            stream_overrides={"provider": "premium"},
            policies=[policy],
        )
        values = resolved["values"]
        # environment -> process -> stream precedence with provenance:
        assert values["language"] == {"value": "en", "source": "environment"}
        assert values["confidence_floor"] == {"value": 0.9, "source": "process"}
        assert values["provider"] == {"value": "premium", "source": "stream"}
        assert values["max_pages"]["source"] == "environment"
        assert resolved["policies"][0]["policy_type"] == "retention"
        assert effective_config(resolved)["provider"] == "premium"

        # Deterministic: identical inputs, identical fingerprint; the stored
        # fingerprint matches a recomputation; any change moves it.
        again = resolve_configuration(
            process_version=version,
            stream_overrides={"provider": "premium"},
            policies=[policy],
        )
        assert again["fingerprint"] == resolved["fingerprint"]
        assert fingerprint(resolved) == resolved["fingerprint"]
        different = resolve_configuration(
            process_version=version, stream_overrides={}, policies=[policy]
        )
        assert different["fingerprint"] != resolved["fingerprint"]
        # Override reset: removing the stream override falls back to process.
        assert different["values"]["provider"] == {"value": "acme", "source": "process"}


async def test_resolver_rejects_mutable_inputs(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        process = await create_process(session, ORG_A, name="POs", slug="pos", actor_id=ACTOR)
        draft = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        with pytest.raises(InvalidVersionStateError, match="immutable process versions"):
            resolve_configuration(process_version=draft, stream_overrides={})
        await publish_draft(session, ORG_A, process=process, draft=draft, actor_id=ACTOR)
        unpublished_policy = await create_policy_draft(
            session,
            ORG_A,
            policy_type=PolicyType.RETENTION,
            definition={"document_days": 30},
            actor_id=ACTOR,
        )
        with pytest.raises(InvalidVersionStateError, match="not published"):
            resolve_configuration(
                process_version=draft, stream_overrides={}, policies=[unpublished_policy]
            )
