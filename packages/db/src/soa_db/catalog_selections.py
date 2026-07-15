"""Durable, tenant-scoped catalog identities for matched order fields.

``ExtractedField.catalog_match_json`` is useful explainability evidence, but it
is not an identity boundary: display values and source ids can be repeated in
later catalog versions.  This module records the exact catalog, immutable
version, and record UUID chosen for each catalog-backed field.  Rows are
append-only so reviewer changes retain their full history; the latest row for
the field is its current resolution.

Approval and canonicalization use :func:`resolve_catalog_identities` to fail
closed when a selection no longer describes the effective value, the stream's
resolved catalog version changed, or the exact tenant-scoped record cannot be
loaded.  Explicit reviewer-confirmed no-match and cleared values remain
distinguishable from unresolved machine decisions.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from sqlalchemy import Index, String, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin
from soa_db.catalog_match_policy import MatchDecision, resolve_match
from soa_db.catalog_matching import facts_of
from soa_db.catalogs import (
    Catalog,
    CatalogBindingRepository,
    CatalogRecord,
    CatalogRecordRepository,
    CatalogRepository,
    CatalogVersion,
    resolve_catalog_version,
)
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID


@dataclass(frozen=True)
class CatalogFieldConfig:
    field_type: str
    catalog_type: str


# The authoritative field-to-catalog contract is shared by the worker, review
# API, revalidation, and canonicalization.  Keeping four private copies was an
# easy way for one phase to validate a different record set than another.
CATALOG_FIELD_CONFIG: dict[str, CatalogFieldConfig] = {
    "customer_name": CatalogFieldConfig(field_type="customer", catalog_type="customers"),
    "lines.sku": CatalogFieldConfig(field_type="material", catalog_type="products"),
}


class CatalogSelectionStatus(StrEnum):
    SELECTED = "selected"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED_NO_MATCH = "confirmed_no_match"
    CLEARED = "cleared"


class CatalogSelectionSource(StrEnum):
    MACHINE = "machine"
    REVIEWER = "reviewer"
    CORRECTION = "correction"


class CatalogSelectionError(ValueError):
    pass


class CatalogFieldSelection(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """One append-only resolution event for a field position."""

    __tablename__ = "catalog_field_selections"

    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    field_key: Mapped[str] = mapped_column(String(255), nullable=False)
    row_index: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    selection_source: Mapped[str] = mapped_column(String(30), nullable=False)

    # Identity hierarchy.  The record is nullable only for unresolved,
    # reviewer-confirmed no-match, and cleared outcomes.
    catalog_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    catalog_record_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    source_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Bind the identity to the exact effective field value it resolved.  The
    # JSON value supports future non-string catalog keys; the digest provides a
    # stable, cheap comparison and avoids relying on database JSON equality.
    matched_value: Mapped[Any | None] = mapped_column(PORTABLE_JSON, nullable=True)
    value_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(PORTABLE_JSON, nullable=True)
    selected_by: Mapped[str] = mapped_column(String(200), nullable=False)

    __table_args__ = (
        Index(
            "ix_catalog_field_selections_run_field",
            "run_id",
            "field_key",
            "row_index",
            "created_at",
        ),
    )


@event.listens_for(Session, "before_flush")
def _refuse_catalog_selection_mutation(session: Session, _ctx: object, _instances: object) -> None:
    for entity in session.dirty:
        if isinstance(entity, CatalogFieldSelection) and session.is_modified(entity):
            raise ValueError(
                f"catalog selection {entity.id} is append-only — record a new selection"
            )


class CatalogFieldSelectionRepository(ScopedRepository[CatalogFieldSelection]):
    model = CatalogFieldSelection

    async def list_for_run(self, run_id: uuid.UUID) -> list[CatalogFieldSelection]:
        stmt = (
            self._scoped_select()
            .where(CatalogFieldSelection.run_id == run_id)
            .order_by(CatalogFieldSelection.created_at, CatalogFieldSelection.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())


def latest_catalog_selections(
    rows: list[CatalogFieldSelection],
) -> dict[tuple[str, int | None], CatalogFieldSelection]:
    latest: dict[tuple[str, int | None], CatalogFieldSelection] = {}
    for row in rows:
        latest[(row.field_key, row.row_index)] = row
    return latest


def catalog_value_fingerprint(value: Any | None) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class BoundCatalog:
    catalog: Catalog
    version: CatalogVersion
    records: tuple[CatalogRecord, ...]


async def resolve_bound_catalog(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
    catalog_type: str,
) -> tuple[BoundCatalog | None, str | None]:
    """Resolve exactly one bound catalog of ``catalog_type``.

    ``(None, None)`` means the stream deliberately has no catalog of this
    type.  A non-null error means configuration exists but is unsafe or
    unusable.  Multiple catalogs of one type fail closed: merging versions
    makes a selected source id ambiguous and was inconsistent between API and
    worker before durable identities existed.
    """

    bindings = await CatalogBindingRepository(session, context).list_for_stream(stream_id)
    repo = CatalogRepository(session, context)
    matches: list[tuple[Any, Catalog]] = []
    for binding in bindings:
        catalog = await repo.get(binding.catalog_id)
        if catalog is not None and catalog.catalog_type == catalog_type:
            matches.append((binding, catalog))
    if not matches:
        return None, None
    if len(matches) > 1:
        return None, (
            f"the stream has {len(matches)} {catalog_type} catalogs bound; "
            "exactly one is required for deterministic identity resolution"
        )
    binding, catalog = matches[0]
    version = await resolve_catalog_version(session, context, binding=binding)
    if version is None:
        return None, f"the bound {catalog_type} catalog has no activated version"
    records = await CatalogRecordRepository(session, context).list_for_version(version.id)
    return BoundCatalog(catalog=catalog, version=version, records=tuple(records)), None


def _check_scope(
    context: OrganizationContext,
    *,
    catalog: Catalog,
    version: CatalogVersion,
    record: CatalogRecord | None,
) -> None:
    expected = context.organization_id
    for label, entity in (
        ("catalog", catalog),
        ("catalog version", version),
        ("catalog record", record),
    ):
        if entity is not None and entity.organization_id != expected:
            raise CatalogSelectionError(f"{label} belongs to a different organization")
    if version.catalog_id != catalog.id:
        raise CatalogSelectionError("catalog version does not belong to the selected catalog")
    if record is not None and record.catalog_version_id != version.id:
        raise CatalogSelectionError("catalog record does not belong to the selected version")


async def record_catalog_selection(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID | None,
    field_key: str,
    row_index: int | None,
    status: CatalogSelectionStatus,
    selection_source: CatalogSelectionSource,
    catalog: Catalog,
    version: CatalogVersion,
    record: CatalogRecord | None,
    matched_value: Any | None,
    selected_by: str,
    decision: MatchDecision | dict[str, Any] | None = None,
) -> CatalogFieldSelection:
    if field_key not in CATALOG_FIELD_CONFIG:
        raise CatalogSelectionError(f"field {field_key!r} is not catalog-backed")
    _check_scope(context, catalog=catalog, version=version, record=record)
    if status == CatalogSelectionStatus.SELECTED and record is None:
        raise CatalogSelectionError("a selected outcome requires an exact catalog record")
    if status != CatalogSelectionStatus.SELECTED and record is not None:
        raise CatalogSelectionError(f"a {status.value} outcome cannot carry a catalog record")
    if status == CatalogSelectionStatus.CLEARED and matched_value is not None:
        raise CatalogSelectionError("a cleared outcome must bind to a null value")

    decision_json = decision.to_record() if isinstance(decision, MatchDecision) else decision
    row = CatalogFieldSelectionRepository(session, context).add(
        CatalogFieldSelection(
            document_id=document_id,
            run_id=run_id,
            task_id=task_id,
            field_key=field_key,
            row_index=row_index,
            status=status.value,
            selection_source=selection_source.value,
            catalog_id=catalog.id,
            catalog_version_id=version.id,
            catalog_record_id=record.id if record is not None else None,
            source_id=record.source_id if record is not None else None,
            display_name=record.display_name if record is not None else None,
            matched_value=matched_value,
            value_fingerprint=catalog_value_fingerprint(matched_value),
            decision_json=decision_json,
            selected_by=selected_by,
        )
    )
    await session.flush()
    return row


async def reconcile_catalog_selection(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
    document_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID | None,
    field_key: str,
    row_index: int | None,
    value: Any | None,
    as_of: date,
    selected_by: str,
    selection_source: CatalogSelectionSource,
) -> CatalogFieldSelection | None:
    """Re-resolve a catalog-backed value after extraction or correction.

    A missing/unusable binding returns ``None``; approval's bulk validator
    reports the configuration problem.  Ambiguous/no-match machine results
    are persisted as ``needs_review`` rather than silently retaining an older
    record identity.
    """

    config = CATALOG_FIELD_CONFIG.get(field_key)
    if config is None:
        return None
    bound, error = await resolve_bound_catalog(
        session, context, stream_id=stream_id, catalog_type=config.catalog_type
    )
    if error is not None or bound is None:
        return None
    if value is None or (isinstance(value, str) and not value.strip()):
        return await record_catalog_selection(
            session,
            context,
            document_id=document_id,
            run_id=run_id,
            task_id=task_id,
            field_key=field_key,
            row_index=row_index,
            status=CatalogSelectionStatus.CLEARED,
            selection_source=selection_source,
            catalog=bound.catalog,
            version=bound.version,
            record=None,
            matched_value=None,
            selected_by=selected_by,
        )

    text_value = str(value)
    decision = resolve_match(
        text_value,
        [facts_of(record) for record in bound.records],
        field_type=config.field_type,
        as_of=as_of,
    )
    selected = None
    if decision.selected_source_id is not None:
        selected = next(
            (record for record in bound.records if record.source_id == decision.selected_source_id),
            None,
        )
    return await record_catalog_selection(
        session,
        context,
        document_id=document_id,
        run_id=run_id,
        task_id=task_id,
        field_key=field_key,
        row_index=row_index,
        status=(
            CatalogSelectionStatus.SELECTED
            if selected is not None
            else CatalogSelectionStatus.NEEDS_REVIEW
        ),
        selection_source=selection_source,
        catalog=bound.catalog,
        version=bound.version,
        record=selected,
        matched_value=value,
        selected_by=selected_by,
        decision=decision,
    )


@dataclass(frozen=True)
class CatalogIdentity:
    field_key: str
    row_index: int | None
    catalog_id: uuid.UUID
    catalog_version_id: uuid.UUID
    catalog_record_id: uuid.UUID
    source_id: str
    display_name: str


@dataclass(frozen=True)
class CatalogIdentityIssue:
    code: str
    message: str
    field_key: str
    row_index: int | None

    def to_reason(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field_key": self.field_key,
            "row_index": self.row_index,
            "rule_key": f"catalog.identity.{self.code}",
        }


@dataclass(frozen=True)
class CatalogIdentityResolution:
    identities: dict[tuple[str, int | None], CatalogIdentity]
    confirmed_no_match: frozenset[tuple[str, int | None]]
    issues: tuple[CatalogIdentityIssue, ...]


async def resolve_catalog_identities(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
    run_id: uuid.UUID,
    values: dict[tuple[str, int | None], Any | None],
    as_of: date,
) -> CatalogIdentityResolution:
    """Resolve and verify current identities for every catalog-backed value."""

    latest = latest_catalog_selections(
        await CatalogFieldSelectionRepository(session, context).list_for_run(run_id)
    )
    identities: dict[tuple[str, int | None], CatalogIdentity] = {}
    confirmed_no_match: set[tuple[str, int | None]] = set()
    issues: list[CatalogIdentityIssue] = []
    bound_cache: dict[str, tuple[BoundCatalog | None, str | None]] = {}
    record_cache: dict[str, dict[uuid.UUID, CatalogRecord]] = {}

    keys = {key for key in values if key[0] in CATALOG_FIELD_CONFIG} | {
        key for key in latest if key[0] in CATALOG_FIELD_CONFIG
    }
    for field_key, row_index in sorted(
        keys, key=lambda item: (item[0], -1 if item[1] is None else item[1])
    ):
        config = CATALOG_FIELD_CONFIG[field_key]
        if config.catalog_type not in bound_cache:
            bound_cache[config.catalog_type] = await resolve_bound_catalog(
                session,
                context,
                stream_id=stream_id,
                catalog_type=config.catalog_type,
            )
            loaded, _error = bound_cache[config.catalog_type]
            if loaded is not None:
                record_cache[config.catalog_type] = {record.id: record for record in loaded.records}
        bound, binding_error = bound_cache[config.catalog_type]
        value = values.get((field_key, row_index))
        selection = latest.get((field_key, row_index))

        def issue(
            code: str,
            message: str,
            current_field: str = field_key,
            current_row: int | None = row_index,
        ) -> None:
            issues.append(CatalogIdentityIssue(code, message, current_field, current_row))

        if binding_error is not None:
            issue("binding_invalid", binding_error)
            continue
        if bound is None:
            # An unconfigured field remains ordinary extracted data.  A prior
            # selection after unbinding is stale and must not leak downstream.
            if selection is not None:
                issue("binding_removed", "the catalog used by this selection is no longer bound")
            continue
        if selection is None:
            if value is not None and not (isinstance(value, str) and not value.strip()):
                issue("selection_missing", "the field has no retained catalog resolution")
            continue
        if selection.value_fingerprint != catalog_value_fingerprint(value):
            issue(
                "value_changed",
                "the effective field value changed after its catalog resolution; reselect it",
            )
            continue
        if selection.catalog_id != bound.catalog.id:
            issue("catalog_changed", "the stream now resolves this field through another catalog")
            continue
        if selection.catalog_version_id != bound.version.id:
            issue(
                "version_changed",
                "the stream's resolved catalog version changed; revalidate this field",
            )
            continue

        status = CatalogSelectionStatus(selection.status)
        if status == CatalogSelectionStatus.CLEARED:
            if value is not None:
                issue("cleared_value_present", "a cleared catalog resolution now has a value")
            continue
        if status == CatalogSelectionStatus.CONFIRMED_NO_MATCH:
            confirmed_no_match.add((field_key, row_index))
            continue
        if status == CatalogSelectionStatus.NEEDS_REVIEW:
            issue("selection_unresolved", "the catalog match still requires reviewer resolution")
            continue
        if selection.catalog_record_id is None:
            issue("record_missing", "the selected outcome retained no catalog record identifier")
            continue

        record = record_cache.get(config.catalog_type, {}).get(selection.catalog_record_id)
        if record is None:
            issue("record_unavailable", "the exact selected catalog record is unavailable")
            continue
        if record.catalog_version_id != selection.catalog_version_id:
            issue("record_version_mismatch", "the selected record is not in the retained version")
            continue
        if record.source_id != selection.source_id or record.display_name != selection.display_name:
            issue("record_snapshot_mismatch", "the selected record no longer matches its snapshot")
            continue
        if not facts_of(record).effective_on(as_of):
            issue(
                "record_out_of_effect",
                f"the selected record is not effective on {as_of.isoformat()}",
            )
            continue
        identities[(field_key, row_index)] = CatalogIdentity(
            field_key=field_key,
            row_index=row_index,
            catalog_id=selection.catalog_id,
            catalog_version_id=selection.catalog_version_id,
            catalog_record_id=record.id,
            source_id=record.source_id,
            display_name=record.display_name,
        )

    return CatalogIdentityResolution(
        identities=identities,
        confirmed_no_match=frozenset(confirmed_no_match),
        issues=tuple(issues),
    )


__all__ = [
    "CATALOG_FIELD_CONFIG",
    "BoundCatalog",
    "CatalogFieldConfig",
    "CatalogFieldSelection",
    "CatalogFieldSelectionRepository",
    "CatalogIdentity",
    "CatalogIdentityIssue",
    "CatalogIdentityResolution",
    "CatalogSelectionError",
    "CatalogSelectionSource",
    "CatalogSelectionStatus",
    "catalog_value_fingerprint",
    "latest_catalog_selections",
    "reconcile_catalog_selection",
    "record_catalog_selection",
    "resolve_bound_catalog",
    "resolve_catalog_identities",
]
