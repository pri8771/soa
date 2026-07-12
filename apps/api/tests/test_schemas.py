"""Schema and field-definition tests (CFG-003)."""

import uuid
from pathlib import Path
from typing import Any

import pytest

from soa_api.domain.schemas import (
    SchemaValidationError,
    SchemaVersionRepository,
    create_schema_draft,
    publish_schema_draft,
    to_json_schema,
    validate_schema,
    validate_schema_evolution,
)
from soa_api.domain.versioning import ImmutableVersionError, VersionState
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext

ORG_A = OrganizationContext(organization_id=uuid.UUID(int=0xA))
ACTOR = "user:test-admin"
PROCESS_ID = uuid.UUID(int=0x77)

# Canonical purchase-order example: header fields + repeating line items.
CANONICAL_PO: dict[str, Any] = {
    "fields": [
        {
            "key": "po_number",
            "label": "PO number",
            "type": "text",
            "required": True,
            "criticality": "critical",
            "examples": ["PO-10023"],
        },
        {"key": "order_date", "label": "Order date", "type": "date", "normalization": "date_iso"},
        {
            "key": "currency",
            "label": "Currency",
            "type": "enum",
            "enum_values": ["USD", "EUR", "GBP"],
        },
        {"key": "total", "label": "Order total", "type": "money", "criticality": "critical"},
        {
            "key": "line_items",
            "label": "Line items",
            "type": "table",
            "required": True,
            "columns": [
                {"key": "sku", "label": "SKU", "type": "text", "required": True},
                {
                    "key": "description",
                    "label": "Description",
                    "type": "text",
                    "evidence_required": False,
                },
                {"key": "quantity", "label": "Quantity", "type": "number", "required": True},
                {"key": "unit_price", "label": "Unit price", "type": "money"},
            ],
        },
    ]
}


def test_canonical_po_schema_validates_and_exports_json_schema() -> None:
    schema = validate_schema(CANONICAL_PO)
    exported = to_json_schema(schema)
    assert exported["required"] == ["po_number", "line_items"]
    assert exported["properties"]["order_date"] == {"type": "string", "format": "date"}
    assert exported["properties"]["currency"]["enum"] == ["USD", "EUR", "GBP"]
    items = exported["properties"]["line_items"]["items"]
    assert items["required"] == ["sku", "quantity"]
    assert items["properties"]["quantity"] == {"type": "number"}
    assert exported["additionalProperties"] is False


def test_duplicate_header_keys_rejected() -> None:
    with pytest.raises(SchemaValidationError, match="duplicate field key"):
        validate_schema(
            {
                "fields": [
                    {"key": "total", "label": "A", "type": "text"},
                    {"key": "total", "label": "B", "type": "number"},
                ]
            }
        )


def test_duplicate_table_column_keys_rejected() -> None:
    with pytest.raises(SchemaValidationError, match="duplicate field key 'sku'"):
        validate_schema(
            {
                "fields": [
                    {
                        "key": "line_items",
                        "label": "Items",
                        "type": "table",
                        "columns": [
                            {"key": "sku", "label": "A", "type": "text"},
                            {"key": "sku", "label": "B", "type": "text"},
                        ],
                    }
                ]
            }
        )


def test_nested_tables_rejected() -> None:
    with pytest.raises(SchemaValidationError, match="nested tables"):
        validate_schema(
            {
                "fields": [
                    {
                        "key": "line_items",
                        "label": "Items",
                        "type": "table",
                        "columns": [
                            {
                                "key": "sub_items",
                                "label": "Sub",
                                "type": "table",
                                "columns": [{"key": "x", "label": "X", "type": "text"}],
                            }
                        ],
                    }
                ]
            }
        )


def test_enum_and_table_shape_rules() -> None:
    with pytest.raises(SchemaValidationError, match="must declare enum_values"):
        validate_schema({"fields": [{"key": "currency", "label": "C", "type": "enum"}]})
    with pytest.raises(SchemaValidationError, match="remove enum_values"):
        validate_schema(
            {
                "fields": [
                    {"key": "note", "label": "N", "type": "text", "enum_values": ["a"]},
                ]
            }
        )
    with pytest.raises(SchemaValidationError, match="must declare columns"):
        validate_schema({"fields": [{"key": "items", "label": "I", "type": "table"}]})
    with pytest.raises(SchemaValidationError, match="pattern"):
        validate_schema({"fields": [{"key": "Bad-Key", "label": "B", "type": "text"}]})


def test_type_changes_across_versions_rejected() -> None:
    old = validate_schema({"fields": [{"key": "total", "label": "T", "type": "money"}]})
    new = validate_schema({"fields": [{"key": "total", "label": "T", "type": "text"}]})
    with pytest.raises(SchemaValidationError, match="type changes are rejected"):
        validate_schema_evolution(old, new)
    # Adding and removing keys is legal evolution.
    grown = validate_schema(
        {
            "fields": [
                {"key": "total", "label": "T", "type": "money"},
                {"key": "tax", "label": "Tax", "type": "money"},
            ]
        }
    )
    validate_schema_evolution(old, grown)
    validate_schema_evolution(grown, old)


def test_table_column_type_change_rejected() -> None:
    old = validate_schema(CANONICAL_PO)
    changed = validate_schema(
        {
            "fields": [
                {
                    "key": "line_items",
                    "label": "Items",
                    "type": "table",
                    "columns": [{"key": "quantity", "label": "Q", "type": "text"}],
                }
            ]
        }
    )
    with pytest.raises(SchemaValidationError, match=r"line_items\.quantity"):
        validate_schema_evolution(old, changed)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/schemas.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_publish_lifecycle_supersedes_and_blocks_type_changes(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        draft = await create_schema_draft(
            session, ORG_A, process_id=PROCESS_ID, definition=CANONICAL_PO, actor_id=ACTOR
        )
        await publish_schema_draft(session, ORG_A, draft=draft, actor_id=ACTOR)
        first_id = draft.id
    # A draft that changes an existing key's type cannot publish.
    async with db.session_scope() as session:
        bad = await create_schema_draft(
            session,
            ORG_A,
            process_id=PROCESS_ID,
            definition={"fields": [{"key": "po_number", "label": "PO", "type": "number"}]},
            actor_id=ACTOR,
        )
        with pytest.raises(SchemaValidationError):
            await publish_schema_draft(session, ORG_A, draft=bad, actor_id=ACTOR)
        # Legal evolution publishes and supersedes.
        good = await create_schema_draft(
            session,
            ORG_A,
            process_id=PROCESS_ID,
            definition={"fields": [{"key": "po_number", "label": "PO", "type": "text"}]},
            actor_id=ACTOR,
        )
        await publish_schema_draft(session, ORG_A, draft=good, actor_id=ACTOR)
    async with db.session_scope() as session:
        repo = SchemaVersionRepository(session, ORG_A)
        first = await repo.get(first_id)
        assert first is not None and first.state == VersionState.SUPERSEDED
        published = await repo.get_published(PROCESS_ID)
        assert published is not None and published.version_number == 3


async def test_invalid_drafts_never_enter_storage(db: DatabaseSessions) -> None:
    with pytest.raises(SchemaValidationError):
        async with db.session_scope() as session:
            await create_schema_draft(
                session,
                ORG_A,
                process_id=PROCESS_ID,
                definition={"fields": [{"key": "x", "label": "X", "type": "enum"}]},
                actor_id=ACTOR,
            )


async def test_published_schema_versions_are_immutable(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        draft = await create_schema_draft(
            session, ORG_A, process_id=PROCESS_ID, definition=CANONICAL_PO, actor_id=ACTOR
        )
        await publish_schema_draft(session, ORG_A, draft=draft, actor_id=ACTOR)
        published_id = draft.id
    with pytest.raises(ImmutableVersionError):
        async with db.session_scope() as session:
            stored = await SchemaVersionRepository(session, ORG_A).get(published_id)
            assert stored is not None
            stored.definition = {"fields": []}
