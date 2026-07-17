"""Instruction-version tests (AIO-010): draft numbering, content
validation, publish-and-supersede, immutability of published content,
the exact-version reference, and audit summaries that never carry the
prompt text."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.instructions import (
    InstructionValidationError,
    create_instruction_draft,
    publish_instruction_draft,
    published_instruction,
    update_instruction_draft,
    validate_instruction_content,
)
from soa_db.repository import OrganizationContext
from soa_db.versioning import ImmutableVersionError, InvalidVersionStateError

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM_VERSION = uuid.UUID("44444444-4444-4444-8444-444444444444")
SCHEMA_VERSION = uuid.UUID("55555555-5555-4555-8555-555555555555")
CONTEXT = OrganizationContext(organization_id=ORG)

CONTENT = {
    "instructions": "Extract the purchase order fields. Values verbatim; null when absent.",
    "field_guidance": {"po_number": "top right, labelled 'PO No.'"},
}


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/instructions.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def make_draft(db: DatabaseSessions, content: dict | None = None) -> uuid.UUID:
    async with db.session_scope() as session:
        draft = await create_instruction_draft(
            session,
            CONTEXT,
            stream_version_id=STREAM_VERSION,
            schema_version_id=SCHEMA_VERSION,
            content=content or CONTENT,
            actor_id="user:u-1",
        )
        return draft.id


class TestContentValidation:
    def test_instructions_must_be_a_non_empty_string(self) -> None:
        with pytest.raises(InstructionValidationError, match="non-empty"):
            validate_instruction_content({"instructions": "  "})

    def test_instructions_are_bounded(self) -> None:
        with pytest.raises(InstructionValidationError, match="bounded"):
            validate_instruction_content({"instructions": "x" * 20_001})

    def test_the_content_shape_is_closed(self) -> None:
        with pytest.raises(InstructionValidationError, match="closed"):
            validate_instruction_content({"instructions": "ok", "tools": ["curl"]})

    def test_field_guidance_entries_must_be_strings(self) -> None:
        with pytest.raises(InstructionValidationError, match="po_number"):
            validate_instruction_content({"instructions": "ok", "field_guidance": {"po_number": 7}})

    def test_examples_slot_is_accepted_and_shaped(self) -> None:
        validate_instruction_content(
            {
                "instructions": "ok",
                "examples": [
                    {
                        "text": "PURCHASE ORDER PO-1",
                        "fields": {"po_number": "PO-1", "ship_date": None},
                        "lines": [{"sku": "A", "qty": "2"}],
                    }
                ],
            }
        )

    def test_examples_are_bounded_in_count(self) -> None:
        with pytest.raises(InstructionValidationError, match="exceeds"):
            validate_instruction_content(
                {
                    "instructions": "ok",
                    "examples": [{"fields": {"po_number": f"PO-{i}"}} for i in range(9)],
                }
            )

    def test_example_text_is_bounded(self) -> None:
        with pytest.raises(InstructionValidationError, match="characters"):
            validate_instruction_content(
                {"instructions": "ok", "examples": [{"text": "x" * 4001, "fields": {"a": "b"}}]}
            )

    def test_example_needs_fields_or_text(self) -> None:
        with pytest.raises(InstructionValidationError, match="expected fields or example text"):
            validate_instruction_content(
                {"instructions": "ok", "examples": [{"fields": {}, "text": "   "}]}
            )

    def test_example_field_values_must_be_strings(self) -> None:
        with pytest.raises(InstructionValidationError, match="must be a string or null"):
            validate_instruction_content(
                {"instructions": "ok", "examples": [{"fields": {"po_number": 7}}]}
            )

    def test_example_values_are_length_bounded(self) -> None:
        with pytest.raises(InstructionValidationError, match="exceeds"):
            validate_instruction_content(
                {"instructions": "ok", "examples": [{"fields": {"po_number": "x" * 501}}]}
            )


class TestVersionLifecycle:
    async def test_drafts_number_sequentially_per_stream_version(
        self, db: DatabaseSessions
    ) -> None:
        first = await make_draft(db)
        await make_draft(db)
        async with db.session_scope() as session:
            from soa_db.instructions import InstructionVersionRepository

            versions = await InstructionVersionRepository(session, CONTEXT).list_for_stream_version(
                STREAM_VERSION
            )
            assert [v.version_number for v in versions] == [1, 2]
            assert versions[0].id == first
            assert versions[0].reference == f"instruction:{first}:v1"

    async def test_publish_supersedes_the_previous_published_version(
        self, db: DatabaseSessions
    ) -> None:
        first = await make_draft(db)
        second = await make_draft(db)
        async with db.session_scope() as session:
            from soa_db.instructions import InstructionVersionRepository

            repo = InstructionVersionRepository(session, CONTEXT)
            draft_one = await repo.get(first)
            assert draft_one is not None
            await publish_instruction_draft(session, CONTEXT, draft=draft_one, actor_id="user:u-1")
        async with db.session_scope() as session:
            repo = InstructionVersionRepository(session, CONTEXT)
            draft_two = await repo.get(second)
            assert draft_two is not None
            await publish_instruction_draft(session, CONTEXT, draft=draft_two, actor_id="user:u-1")
        async with db.session_scope() as session:
            repo = InstructionVersionRepository(session, CONTEXT)
            one = await repo.get(first)
            two = await repo.get(second)
            assert one is not None and one.state == "superseded"
            assert two is not None and two.state == "published"
            live = await published_instruction(session, CONTEXT, stream_version_id=STREAM_VERSION)
            assert live is not None and live.id == second

    async def test_published_content_is_immutable(self, db: DatabaseSessions) -> None:
        draft_id = await make_draft(db)
        async with db.session_scope() as session:
            from soa_db.instructions import InstructionVersionRepository

            record = await InstructionVersionRepository(session, CONTEXT).get(draft_id)
            assert record is not None
            await publish_instruction_draft(session, CONTEXT, draft=record, actor_id="user:u-1")
        with pytest.raises(ImmutableVersionError):
            async with db.session_scope() as session:
                record = await InstructionVersionRepository(session, CONTEXT).get(draft_id)
                assert record is not None
                record.content = {"instructions": "history rewritten"}
                await session.flush()

    async def test_only_drafts_publish_or_update(self, db: DatabaseSessions) -> None:
        draft_id = await make_draft(db)
        async with db.session_scope() as session:
            from soa_db.instructions import InstructionVersionRepository

            record = await InstructionVersionRepository(session, CONTEXT).get(draft_id)
            assert record is not None
            await publish_instruction_draft(session, CONTEXT, draft=record, actor_id="user:u-1")
            with pytest.raises(InvalidVersionStateError):
                await publish_instruction_draft(session, CONTEXT, draft=record, actor_id="user:u-1")
            with pytest.raises(InvalidVersionStateError):
                await update_instruction_draft(
                    session, CONTEXT, draft=record, content=CONTENT, actor_id="user:u-1"
                )

    async def test_no_published_version_means_none(self, db: DatabaseSessions) -> None:
        await make_draft(db)
        async with db.session_scope() as session:
            assert (
                await published_instruction(session, CONTEXT, stream_version_id=STREAM_VERSION)
                is None
            )


class TestAuditDiscipline:
    async def test_audit_events_carry_sizes_never_the_prompt_text(
        self, db: DatabaseSessions
    ) -> None:
        draft_id = await make_draft(db)
        async with db.session_scope() as session:
            from soa_db.instructions import InstructionVersionRepository

            record = await InstructionVersionRepository(session, CONTEXT).get(draft_id)
            assert record is not None
            await publish_instruction_draft(session, CONTEXT, draft=record, actor_id="user:u-1")
        async with db.session_scope() as session:
            events = (
                (await session.execute(select(AuditEvent).order_by(AuditEvent.occurred_at)))
                .scalars()
                .all()
            )
            actions = {event.action for event in events}
            assert {"instructions.draft_created", "instructions.published"} <= actions
            for event in events:
                dumped = str(event.summary)
                assert "verbatim" not in dumped  # a phrase from the prompt text
                assert "top right" not in dumped  # field guidance text
