"""Draft validation and checked publish tests (CFG-007)."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from soa_api.domain.policies import PolicyType, create_policy_draft, publish_policy_draft
from soa_api.domain.processes import (
    ProcessRepository,
    ProcessVersionRepository,
    create_draft,
    create_process,
    publish_draft,
)
from soa_api.domain.rules import create_rule_set_draft, publish_rule_set_draft
from soa_api.domain.schemas import create_schema_draft, publish_schema_draft
from soa_api.domain.versioning import VersionState
from soa_api.services.config_service import (
    DraftNotPublishableError,
    publish_process_draft_checked,
    rollback_active_version,
    validate_process_draft,
)
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.repository import OrganizationContext

ORG_A = OrganizationContext(organization_id=uuid.UUID(int=0xA))
ACTOR = "user:test-admin"

SCHEMA = {"fields": [{"key": "po_number", "label": "PO", "type": "text", "required": True}]}
PROVIDER = {"provider_name": "acme", "capabilities": ["ocr", "field_extraction"]}
RULES = {"version": "1", "rules": []}


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/config.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed_ready_process(db: DatabaseSessions) -> uuid.UUID:
    """Process with published schema + provider policy: publishable."""
    async with db.session_scope() as session:
        process = await create_process(session, ORG_A, name="POs", slug="pos", actor_id=ACTOR)
        schema = await create_schema_draft(
            session, ORG_A, process_id=process.id, definition=SCHEMA, actor_id=ACTOR
        )
        await publish_schema_draft(session, ORG_A, draft=schema, actor_id=ACTOR)
        rules = await create_rule_set_draft(
            session,
            ORG_A,
            process_id=process.id,
            definition=RULES,
            field_types={"po_number": "text"},
            actor_id=ACTOR,
        )
        await publish_rule_set_draft(
            session,
            ORG_A,
            draft=rules,
            field_types={"po_number": "text"},
            actor_id=ACTOR,
        )
        policy = await create_policy_draft(
            session, ORG_A, policy_type=PolicyType.PROVIDER, definition=PROVIDER, actor_id=ACTOR
        )
        await publish_policy_draft(session, ORG_A, draft=policy, actor_id=ACTOR)
        return process.id


async def test_checked_publish_succeeds_with_clean_report(db: DatabaseSessions) -> None:
    process_id = await seed_ready_process(db)
    async with db.session_scope() as session:
        process = await ProcessRepository(session, ORG_A).get(process_id)
        assert process is not None
        draft = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        published, report = await publish_process_draft_checked(
            session, ORG_A, process=process, draft=draft, actor_id=ACTOR
        )
        assert report.is_publishable
        assert report.as_dict()["findings"] == []
        assert published.state == VersionState.PUBLISHED
        assert process.active_version_id == published.id


async def test_missing_provider_policy_blocks_publish_with_clear_report(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
        process = await create_process(session, ORG_A, name="POs", slug="pos", actor_id=ACTOR)
        draft = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        with pytest.raises(DraftNotPublishableError) as excinfo:
            await publish_process_draft_checked(
                session, ORG_A, process=process, draft=draft, actor_id=ACTOR
            )
        report = excinfo.value.report.as_dict()
        assert report["publishable"] is False
        paths = {f["path"] for f in report["findings"]}
        assert "policies.provider" in paths
        # Missing schema is a blocker: it cannot produce executable runs.
        schema_findings = [f for f in report["findings"] if f["path"] == "schema"]
        assert schema_findings and schema_findings[0]["level"] == "error"
        # The refused draft stays a draft.
        assert draft.state == VersionState.DRAFT


async def test_failed_unit_of_work_rolls_back_publish_entirely(db: DatabaseSessions) -> None:
    """Acceptance: publish is transactional — a failure later in the same
    unit of work must leave no published version, no moved pointer, and no
    audit events behind."""
    process_id = await seed_ready_process(db)
    async with db.session_scope() as session:
        process = await ProcessRepository(session, ORG_A).get(process_id)
        assert process is not None
        draft = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        draft_id = draft.id
    with pytest.raises(RuntimeError, match="downstream exploded"):
        async with db.session_scope() as session:
            process = await ProcessRepository(session, ORG_A).get(process_id)
            stored_draft = await ProcessVersionRepository(session, ORG_A).get(draft_id)
            assert process is not None and stored_draft is not None
            await publish_process_draft_checked(
                session, ORG_A, process=process, draft=stored_draft, actor_id=ACTOR
            )
            raise RuntimeError("downstream exploded")
    async with db.session_scope() as session:
        stored_draft = await ProcessVersionRepository(session, ORG_A).get(draft_id)
        process = await ProcessRepository(session, ORG_A).get(process_id)
        assert stored_draft is not None and stored_draft.state == VersionState.DRAFT
        assert process is not None and process.active_version_id is None
        from sqlalchemy import select

        actions = (await session.execute(select(AuditEvent.action))).scalars().all()
        assert "process.version_published" not in actions


async def test_database_backstops_concurrent_first_publish(db: DatabaseSessions) -> None:
    """Two publishes that each saw no prior published version cannot both
    commit: the single-published unique index rejects the second."""
    process_id = await seed_ready_process(db)
    async with db.session_scope() as session:
        process = await ProcessRepository(session, ORG_A).get(process_id)
        assert process is not None
        first = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        second = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        first_id, second_id = first.id, second.id
    async with db.session_scope() as session:
        process = await ProcessRepository(session, ORG_A).get(process_id)
        first = await ProcessVersionRepository(session, ORG_A).get(first_id)
        assert process is not None and first is not None
        await publish_draft(session, ORG_A, process=process, draft=first, actor_id=ACTOR)
    with pytest.raises(IntegrityError):
        async with db.session_scope() as session:
            second = await ProcessVersionRepository(session, ORG_A).get(second_id)
            assert second is not None
            # Simulate a racing publisher that never saw the first publish:
            # flip the state directly, skipping the supersede step.
            second.state = VersionState.PUBLISHED


async def test_rollback_requires_reason_and_moves_pointer(db: DatabaseSessions) -> None:
    process_id = await seed_ready_process(db)
    async with db.session_scope() as session:
        process = await ProcessRepository(session, ORG_A).get(process_id)
        assert process is not None
        first = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        await publish_draft(session, ORG_A, process=process, draft=first, actor_id=ACTOR)
        second = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        await publish_draft(session, ORG_A, process=process, draft=second, actor_id=ACTOR)
        with pytest.raises(ValueError, match="requires a reason"):
            await rollback_active_version(
                session, ORG_A, process=process, target=first, reason="  ", actor_id=ACTOR
            )
        await rollback_active_version(
            session, ORG_A, process=process, target=first, reason="bad mapping", actor_id=ACTOR
        )
        assert process.active_version_id == first.id


async def test_report_flags_rules_that_conflict_with_schema(db: DatabaseSessions) -> None:
    from soa_api.domain.rules import RuleSetVersion

    process_id = await seed_ready_process(db)
    async with db.session_scope() as session:
        # A published rule set referencing a field the schema doesn't have —
        # inserted directly to simulate drift created before the evolution
        # checks existed.
        from sqlalchemy import update

        await session.execute(
            update(RuleSetVersion)
            .where(RuleSetVersion.process_id == process_id)
            .values(
                definition={
                    "rules": [
                        {
                            "key": "ghost-field",
                            "severity": "error",
                            "action": "block",
                            "condition": {"op": "is_present", "key": "no_such_field"},
                        }
                    ]
                }
            )
        )
    async with db.session_scope() as session:
        process = await ProcessRepository(session, ORG_A).get(process_id)
        assert process is not None
        draft = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        report = await validate_process_draft(session, ORG_A, process=process, draft=draft)
        rule_findings = [f for f in report.findings if f.path == "rules"]
        assert rule_findings and rule_findings[0].level == "error"
        assert "no_such_field" in rule_findings[0].message
        assert not report.is_publishable
