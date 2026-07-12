"""Extraction schema and field definitions (CFG-003).

A schema version describes WHAT to extract for a process: header fields
plus repeating line items (TABLE fields with typed columns). Definitions
are data, validated structurally on every edit and again at publish:

- keys are snake_case and unique within their scope,
- TABLE columns cannot themselves be tables (no unbounded recursion),
- ENUM fields declare their values; nothing else may,
- publishing against a previous version rejects TYPE CHANGES on existing
  keys — a field's meaning never silently changes; introduce a new key.

``to_json_schema`` exports a JSON-Schema-compatible view for API consumers
and canonical-output validation (CAN epic).
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import Index, String, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_api.domain.versioning import (
    ImmutablePublishedVersionMixin,
    InvalidVersionStateError,
    VersionState,
)
from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow

KEY_PATTERN = r"^[a-z][a-z0-9_]*$"


class FieldType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    MONEY = "money"
    DATE = "date"
    BOOLEAN = "boolean"
    ENUM = "enum"
    TABLE = "table"


class Criticality(StrEnum):
    CRITICAL = "critical"  # wrong value = wrong order; always human-verified
    STANDARD = "standard"
    INFORMATIONAL = "informational"


class SchemaValidationError(ValueError):
    pass


class FieldDefinition(BaseModel):
    key: str = Field(pattern=KEY_PATTERN, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    type: FieldType
    required: bool = False
    criticality: Criticality = Criticality.STANDARD
    # Named, deterministic normalizer applied post-extraction (e.g.
    # "date_iso", "trim", "currency_code"). Registry arrives with PRC.
    normalization: str | None = None
    # Every extracted value must carry source evidence unless waived.
    evidence_required: bool = True
    examples: list[str] = Field(default_factory=list)
    enum_values: list[str] | None = None
    columns: "list[FieldDefinition] | None" = None

    @model_validator(mode="after")
    def _validate_shape(self) -> "FieldDefinition":
        if self.type == FieldType.ENUM:
            if not self.enum_values:
                raise ValueError(f"enum field {self.key!r} must declare enum_values")
        elif self.enum_values is not None:
            raise ValueError(f"field {self.key!r} is not an enum; remove enum_values")
        if self.type == FieldType.TABLE:
            if not self.columns:
                raise ValueError(f"table field {self.key!r} must declare columns")
            for column in self.columns:
                if column.type == FieldType.TABLE:
                    raise ValueError(
                        f"table field {self.key!r} nests table {column.key!r} — "
                        "nested tables are not supported"
                    )
            _reject_duplicate_keys(self.columns, scope=f"table {self.key!r}")
        elif self.columns is not None:
            raise ValueError(f"field {self.key!r} is not a table; remove columns")
        return self


def _reject_duplicate_keys(fields: list[FieldDefinition], *, scope: str) -> None:
    seen: set[str] = set()
    for field in fields:
        if field.key in seen:
            raise ValueError(f"duplicate field key {field.key!r} in {scope}")
        seen.add(field.key)


class SchemaDefinition(BaseModel):
    fields: list[FieldDefinition]

    @field_validator("fields")
    @classmethod
    def _unique_header_keys(cls, fields: list[FieldDefinition]) -> list[FieldDefinition]:
        _reject_duplicate_keys(fields, scope="schema header")
        return fields

    def field_types(self) -> dict[str, str]:
        """Flat key -> type map (table columns as ``table_key.column_key``)."""
        result: dict[str, str] = {}
        for field in self.fields:
            result[field.key] = str(field.type)
            for column in field.columns or []:
                result[f"{field.key}.{column.key}"] = str(column.type)
        return result


def validate_schema(raw: dict[str, Any]) -> SchemaDefinition:
    try:
        return SchemaDefinition.model_validate(raw)
    except ValueError as exc:
        raise SchemaValidationError(str(exc)) from exc


def validate_schema_evolution(previous: SchemaDefinition, new: SchemaDefinition) -> None:
    """A key's type never changes across versions — downstream mappings and
    stored canonical outputs depend on it. New meaning = new key."""
    old_types = previous.field_types()
    for key, new_type in new.field_types().items():
        old_type = old_types.get(key)
        if old_type is not None and old_type != new_type:
            raise SchemaValidationError(
                f"field {key!r} changes type {old_type!r} -> {new_type!r}; "
                "type changes are rejected — introduce a new key instead"
            )


_PRIMITIVE_JSON_TYPES: dict[FieldType, dict[str, Any]] = {
    FieldType.TEXT: {"type": "string"},
    FieldType.NUMBER: {"type": "number"},
    FieldType.MONEY: {"type": "object"},  # amount + currency (DB-002 convention)
    FieldType.DATE: {"type": "string", "format": "date"},
    FieldType.BOOLEAN: {"type": "boolean"},
}


def _field_json_schema(field: FieldDefinition) -> dict[str, Any]:
    if field.type == FieldType.ENUM:
        return {"type": "string", "enum": list(field.enum_values or [])}
    if field.type == FieldType.TABLE:
        return {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {c.key: _field_json_schema(c) for c in field.columns or []},
                "required": [c.key for c in field.columns or [] if c.required],
                "additionalProperties": False,
            },
        }
    return dict(_PRIMITIVE_JSON_TYPES[field.type])


def to_json_schema(schema: SchemaDefinition) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {f.key: _field_json_schema(f) for f in schema.fields},
        "required": [f.key for f in schema.fields if f.required],
        "additionalProperties": False,
    }


class SchemaVersion(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    ImmutablePublishedVersionMixin,
    TimestampMixin,
    VersionedMixin,
    Base,
):
    __tablename__ = "schema_versions"

    process_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    change_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("process_id", "version_number"),
        # At most ONE published version can exist at a time — the
        # database backstops the supersede logic against concurrent
        # first publishes (no prior row for optimistic locking to trip).
        Index(
            "uq_schema_versions_single_published",
            "process_id",
            unique=True,
            postgresql_where=text("state = 'published'"),
            sqlite_where=text("state = 'published'"),
        ),
    )


class SchemaVersionRepository(ScopedRepository[SchemaVersion]):
    model = SchemaVersion

    async def list_for_process(self, process_id: uuid.UUID) -> list[SchemaVersion]:
        stmt = (
            self._scoped_select()
            .where(SchemaVersion.process_id == process_id)
            .order_by(SchemaVersion.version_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_published(self, process_id: uuid.UUID) -> SchemaVersion | None:
        stmt = self._scoped_select().where(
            SchemaVersion.process_id == process_id,
            SchemaVersion.state == VersionState.PUBLISHED,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


async def create_schema_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    process_id: uuid.UUID,
    definition: dict[str, Any],
    change_summary: str | None = None,
    actor_id: str,
) -> SchemaVersion:
    validate_schema(definition)  # drafts must at least be structurally sound
    repo = SchemaVersionRepository(session, context)
    versions = await repo.list_for_process(process_id)
    next_number = (versions[-1].version_number + 1) if versions else 1
    draft = repo.add(
        SchemaVersion(
            process_id=process_id,
            version_number=next_number,
            definition=dict(definition),
            change_summary=change_summary,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="schema.draft_created",
        target_type="schema_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={"process_id": str(process_id), "version_number": next_number},
    )
    return draft


async def publish_schema_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    draft: SchemaVersion,
    actor_id: str,
    now: datetime | None = None,
) -> SchemaVersion:
    """Validate, check evolution against the current published version,
    supersede it, and publish — one audited transaction."""
    if draft.state != VersionState.DRAFT:
        raise InvalidVersionStateError(f"only drafts publish; this version is {draft.state!r}")
    new_schema = validate_schema(draft.definition)
    repo = SchemaVersionRepository(session, context)
    previous = await repo.get_published(draft.process_id)
    if previous is not None:
        validate_schema_evolution(validate_schema(previous.definition), new_schema)
        previous.state = VersionState.SUPERSEDED
        # Flush the supersede before publishing: the single-published
        # unique index must never see two published rows mid-flush.
        await session.flush()
    draft.state = VersionState.PUBLISHED
    draft.published_at = now or utcnow()
    draft.published_by = actor_id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="schema.version_published",
        target_type="schema_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={
            "process_id": str(draft.process_id),
            "version_number": draft.version_number,
            "superseded_version_id": str(previous.id) if previous else None,
        },
    )
    return draft
