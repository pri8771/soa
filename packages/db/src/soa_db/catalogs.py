"""Catalog, catalog-version, record, and stream-binding models (CAT-001).

A catalog is a tenant's reference data — customers, products, price
lists, units — that matching (CAT-005+) and business validation resolve
against. Correctness discipline mirrors the rest of the platform's
configuration:

- versions follow draft → active → superseded; an ACTIVATED version is
  immutable forever (the shared versioning flush guard refuses edits),
  so every match/validation outcome can name the exact records it ran
  against;
- records carry the SOURCE system's identifier (unique within a
  version), display name, free attributes, alias spellings, and
  effective dates — reference data is temporal and matching must know
  when a record was valid;
- a stream consumes a catalog through an EXPLICIT binding: either
  PINNED to one version, or ROLLING (always the active version) as a
  deliberate recorded choice — there is no implicit default.

Audit events record counts and identifiers, never record contents
(reference data can embed customer terms and prices).
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Date,
    Index,
    String,
    UniqueConstraint,
    cast,
    event,
    func,
    or_,
    select,
    text,
)
from sqlalchemy import (
    inspect as sa_inspect,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import PORTABLE_JSON
from soa_db.pagination import CursorRequest, Page, build_page
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow
from soa_db.versioning import (
    ImmutablePublishedVersionMixin,
    ImmutableVersionError,
    InvalidVersionStateError,
    VersionState,
)

CATALOG_TYPES = frozenset({"customers", "products", "price_lists", "units", "custom"})
CATALOG_SOURCES = frozenset({"csv_import", "xlsx_import", "api", "manual"})
CATALOG_VERSION_PINS_KEY = "catalog_version_pins"


class CatalogBindingMode(StrEnum):
    #: The stream uses exactly this version until someone repins.
    PINNED = "pinned"
    #: The stream always uses the ACTIVE version — an explicit choice.
    ROLLING = "rolling"


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class CatalogVersionPin:
    """One stream binding resolved to an exact immutable catalog version."""

    binding_id: uuid.UUID
    catalog_id: uuid.UUID
    catalog_type: str
    catalog_version_id: uuid.UUID

    def to_json(self) -> dict[str, str]:
        return {
            "binding_id": str(self.binding_id),
            "catalog_id": str(self.catalog_id),
            "catalog_type": self.catalog_type,
            "catalog_version_id": str(self.catalog_version_id),
        }


def parse_catalog_version_pins(raw: object) -> tuple[CatalogVersionPin, ...] | None:
    """Parse authenticated snapshot pins; ``None`` marks a legacy snapshot."""

    if raw is None:
        return None
    if not isinstance(raw, list):
        raise CatalogError("catalog_version_pins must be a list")
    pins: list[CatalogVersionPin] = []
    seen_types: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise CatalogError("catalog_version_pins contains an invalid entry")
        try:
            pin = CatalogVersionPin(
                binding_id=uuid.UUID(str(item["binding_id"])),
                catalog_id=uuid.UUID(str(item["catalog_id"])),
                catalog_type=str(item["catalog_type"]),
                catalog_version_id=uuid.UUID(str(item["catalog_version_id"])),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise CatalogError("catalog_version_pins contains an invalid entry") from error
        if pin.catalog_type not in CATALOG_TYPES:
            raise CatalogError(f"catalog pin has unknown type {pin.catalog_type!r}")
        if pin.catalog_type in seen_types:
            raise CatalogError(f"catalog pins contain more than one {pin.catalog_type} catalog")
        seen_types.add(pin.catalog_type)
        pins.append(pin)
    return tuple(sorted(pins, key=lambda pin: (pin.catalog_type, pin.catalog_id.hex)))


class Catalog(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "catalogs"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    catalog_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)

    __table_args__ = (UniqueConstraint("organization_id", "slug"),)


class CatalogVersion(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    ImmutablePublishedVersionMixin,
    TimestampMixin,
    VersionedMixin,
    Base,
):
    __tablename__ = "catalog_versions"

    catalog_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(nullable=False)
    change_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    record_count: Mapped[int] = mapped_column(nullable=False, default=0)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("catalog_id", "version_number"),
        Index(
            "uq_catalog_versions_single_active",
            "catalog_id",
            unique=True,
            postgresql_where=text("state = 'published'"),
            sqlite_where=text("state = 'published'"),
        ),
    )


class CatalogRecord(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "catalog_records"

    catalog_version_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    #: The SOURCE system's identifier (SKU, customer number, ...).
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    #: Alternative spellings/names matching may recognise.
    aliases: Mapped[list[str]] = mapped_column(PORTABLE_JSON, nullable=False, default=list)
    effective_from: Mapped[date | None] = mapped_column(Date(), nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date(), nullable=True)

    __table_args__ = (UniqueConstraint("catalog_version_id", "source_id"),)


class CatalogBinding(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "catalog_bindings"

    stream_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    catalog_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Required when pinned; must be NULL when rolling.
    pinned_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)

    __table_args__ = (UniqueConstraint("stream_id", "catalog_id"),)


@event.listens_for(Session, "before_flush")
def _freeze_records_in_immutable_catalog_versions(
    session: Session, _ctx: object, _instances: object
) -> None:
    """Published catalog versions include their records, not only the header row."""

    changed = {
        record
        for record in (*session.new, *session.dirty, *session.deleted)
        if isinstance(record, CatalogRecord)
        and (
            record in session.new
            or record in session.deleted
            or session.is_modified(record, include_collections=True)
        )
    }
    for record in changed:
        inspected = sa_inspect(record)
        version_history = inspected.attrs.catalog_version_id.history
        organization_history = inspected.attrs.organization_id.history
        prior_version = (
            version_history.deleted[0]
            if version_history.has_changes() and version_history.deleted
            else record.catalog_version_id
        )
        prior_organization = (
            organization_history.deleted[0]
            if organization_history.has_changes() and organization_history.deleted
            else record.organization_id
        )
        # Authenticate both sides of a reparent. Looking only at the new FK
        # would let a caller move a record out of immutable history and into a
        # draft, silently changing the published version's contents.
        version_scopes = {
            (record.catalog_version_id, record.organization_id),
            (prior_version, prior_organization),
        }
        for version_id, organization_id in version_scopes:
            state = session.execute(
                select(CatalogVersion.state).where(
                    CatalogVersion.id == version_id,
                    CatalogVersion.organization_id == organization_id,
                )
            ).scalar_one_or_none()
            if state in (VersionState.PUBLISHED.value, VersionState.SUPERSEDED.value):
                raise ImmutableVersionError(version_id, str(state))


class CatalogRepository(ScopedRepository[Catalog]):
    model = Catalog

    async def get_by_slug(self, slug: str) -> Catalog | None:
        stmt = self._scoped_select().where(Catalog.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()


class CatalogVersionRepository(ScopedRepository[CatalogVersion]):
    model = CatalogVersion

    async def list_for_catalog(self, catalog_id: uuid.UUID) -> list[CatalogVersion]:
        stmt = (
            self._scoped_select()
            .where(CatalogVersion.catalog_id == catalog_id)
            .order_by(CatalogVersion.version_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_active(self, catalog_id: uuid.UUID) -> CatalogVersion | None:
        stmt = self._scoped_select().where(
            CatalogVersion.catalog_id == catalog_id,
            CatalogVersion.state == VersionState.PUBLISHED,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


class CatalogRecordRepository(ScopedRepository[CatalogRecord]):
    model = CatalogRecord

    async def list_for_version(self, catalog_version_id: uuid.UUID) -> list[CatalogRecord]:
        stmt = (
            self._scoped_select()
            .where(CatalogRecord.catalog_version_id == catalog_version_id)
            .order_by(CatalogRecord.source_id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def search_page(
        self,
        catalog_version_id: uuid.UUID,
        *,
        request: "CursorRequest",
        q: str | None = None,
    ) -> "Page[CatalogRecord]":
        """Cursor-paginated records, optionally filtered by a substring
        of the source id, display name, or aliases."""
        stmt = (
            self._scoped_select()
            .where(CatalogRecord.catalog_version_id == catalog_version_id)
            .order_by(CatalogRecord.id)
            .limit(request.limit + 1)
        )
        if request.after is not None:
            stmt = stmt.where(CatalogRecord.id > request.after)
        if q:
            needle = f"%{q.lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(CatalogRecord.source_id).like(needle),
                    func.lower(CatalogRecord.display_name).like(needle),
                    func.lower(cast(CatalogRecord.aliases, String)).like(needle),
                )
            )
        rows = list((await self._session.execute(stmt)).scalars().all())
        return build_page(rows, request.limit, id_of=lambda row: row.id)

    async def get_by_source_id(
        self, catalog_version_id: uuid.UUID, source_id: str
    ) -> CatalogRecord | None:
        stmt = self._scoped_select().where(
            CatalogRecord.catalog_version_id == catalog_version_id,
            CatalogRecord.source_id == source_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


class CatalogBindingRepository(ScopedRepository[CatalogBinding]):
    model = CatalogBinding

    async def get_for_stream(
        self, stream_id: uuid.UUID, catalog_id: uuid.UUID
    ) -> CatalogBinding | None:
        stmt = self._scoped_select().where(
            CatalogBinding.stream_id == stream_id, CatalogBinding.catalog_id == catalog_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_stream(self, stream_id: uuid.UUID) -> list[CatalogBinding]:
        stmt = (
            self._scoped_select()
            .where(CatalogBinding.stream_id == stream_id)
            .order_by(CatalogBinding.created_at, CatalogBinding.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())


async def create_catalog(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    name: str,
    slug: str,
    catalog_type: str,
    source: str,
    actor_id: str,
) -> Catalog:
    if catalog_type not in CATALOG_TYPES:
        raise CatalogError(f"catalog_type must be one of {sorted(CATALOG_TYPES)}")
    if source not in CATALOG_SOURCES:
        raise CatalogError(f"source must be one of {sorted(CATALOG_SOURCES)}")
    catalog = CatalogRepository(session, context).add(
        Catalog(name=name, slug=slug, catalog_type=catalog_type, source=source, created_by=actor_id)
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="catalog.created",
        target_type="catalog",
        target_id=str(catalog.id),
        organization_id=context.organization_id,
        summary={"slug": slug, "catalog_type": catalog_type, "source": source},
    )
    return catalog


async def create_catalog_version(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    catalog: Catalog,
    change_summary: str | None = None,
    actor_id: str,
) -> CatalogVersion:
    versions = await CatalogVersionRepository(session, context).list_for_catalog(catalog.id)
    next_number = (versions[-1].version_number + 1) if versions else 1
    draft = CatalogVersionRepository(session, context).add(
        CatalogVersion(
            catalog_id=catalog.id, version_number=next_number, change_summary=change_summary
        )
    )
    await session.flush()
    return draft


def _validate_record(
    source_id: str,
    display_name: str,
    aliases: list[str],
    effective_from: date | None,
    effective_to: date | None,
) -> None:
    if not source_id.strip():
        raise CatalogError("a record needs the source system's identifier")
    if not display_name.strip():
        raise CatalogError("a record needs a display name")
    for alias in aliases:
        if not isinstance(alias, str) or not alias.strip():
            raise CatalogError("aliases must be non-empty strings")
    if effective_from and effective_to and effective_to < effective_from:
        raise CatalogError("effective_to cannot precede effective_from")


async def add_catalog_record(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    version: CatalogVersion,
    source_id: str,
    display_name: str,
    attributes: dict[str, Any] | None = None,
    aliases: list[str] | None = None,
    effective_from: date | None = None,
    effective_to: date | None = None,
) -> CatalogRecord:
    if version.state != VersionState.DRAFT:
        raise InvalidVersionStateError(
            f"records can only be added to drafts; this version is {version.state!r}"
        )
    _validate_record(source_id, display_name, aliases or [], effective_from, effective_to)
    existing = await CatalogRecordRepository(session, context).get_by_source_id(
        version.id, source_id
    )
    if existing is not None:
        raise CatalogError(f"source_id {source_id!r} is already in this catalog version")
    record = CatalogRecordRepository(session, context).add(
        CatalogRecord(
            catalog_version_id=version.id,
            source_id=source_id,
            display_name=display_name,
            attributes=dict(attributes or {}),
            aliases=list(aliases or []),
            effective_from=effective_from,
            effective_to=effective_to,
        )
    )
    version.record_count = version.record_count + 1
    await session.flush()
    return record


async def activate_catalog_version(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    catalog: Catalog,
    version: CatalogVersion,
    actor_id: str,
    now: datetime | None = None,
) -> CatalogVersion:
    """Activate a draft: the previously active version is superseded
    (kept, immutable), the catalog's active pointer moves, and the
    version's records freeze forever."""
    if version.state != VersionState.DRAFT:
        raise InvalidVersionStateError(f"only drafts activate; this version is {version.state!r}")
    if version.catalog_id != catalog.id:
        raise InvalidVersionStateError("version belongs to a different catalog")
    records = await CatalogRecordRepository(session, context).list_for_version(version.id)
    if not records:
        raise CatalogError("an empty catalog version cannot activate — nothing to match against")
    previous = await CatalogVersionRepository(session, context).get_active(catalog.id)
    if previous is not None:
        previous.state = VersionState.SUPERSEDED
        await session.flush()
    version.state = VersionState.PUBLISHED
    version.published_at = now or utcnow()
    version.published_by = actor_id
    catalog.active_version_id = version.id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="catalog.version_activated",
        target_type="catalog_version",
        target_id=str(version.id),
        organization_id=context.organization_id,
        summary={
            "catalog_id": str(catalog.id),
            "version_number": version.version_number,
            "records": len(records),
            "superseded_version_id": str(previous.id) if previous else None,
        },
    )
    return version


async def bind_catalog_to_stream(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
    catalog: Catalog,
    mode: CatalogBindingMode,
    pinned_version_id: uuid.UUID | None = None,
    actor_id: str,
) -> CatalogBinding:
    """Bind a stream to a catalog — pinned to one version or explicitly
    rolling. Re-binding replaces the previous choice (audited)."""
    if mode == CatalogBindingMode.PINNED:
        if pinned_version_id is None:
            raise CatalogError("a pinned binding must name the version it pins")
        pinned = await CatalogVersionRepository(session, context).get(pinned_version_id)
        if pinned is None or pinned.catalog_id != catalog.id:
            raise CatalogError("the pinned version does not belong to this catalog")
        if pinned.state == VersionState.DRAFT:
            raise CatalogError("a draft version cannot be pinned — activate it first")
    elif pinned_version_id is not None:
        raise CatalogError("a rolling binding follows the active version; do not pin one")

    repo = CatalogBindingRepository(session, context)
    binding = await repo.get_for_stream(stream_id, catalog.id)
    if binding is None:
        binding = repo.add(
            CatalogBinding(
                stream_id=stream_id,
                catalog_id=catalog.id,
                mode=mode,
                pinned_version_id=pinned_version_id,
                created_by=actor_id,
            )
        )
    else:
        binding.mode = mode
        binding.pinned_version_id = pinned_version_id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="catalog.bound_to_stream",
        target_type="catalog_binding",
        target_id=str(binding.id),
        organization_id=context.organization_id,
        summary={
            "stream_id": str(stream_id),
            "catalog_id": str(catalog.id),
            "mode": mode.value,
            "pinned_version_id": str(pinned_version_id) if pinned_version_id else None,
        },
    )
    return binding


async def resolve_catalog_version(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    binding: CatalogBinding,
) -> CatalogVersion | None:
    """The version a stream actually uses under its binding."""
    repo = CatalogVersionRepository(session, context)
    try:
        mode = CatalogBindingMode(binding.mode)
    except ValueError as error:
        raise CatalogError(f"catalog binding {binding.id} has an invalid mode") from error
    if mode is CatalogBindingMode.PINNED:
        if binding.pinned_version_id is None:
            raise CatalogError(f"pinned catalog binding {binding.id} has no catalog version")
        return await repo.get(binding.pinned_version_id)
    if binding.pinned_version_id is not None:
        raise CatalogError(
            f"rolling catalog binding {binding.id} unexpectedly pins a catalog version"
        )
    return await repo.get_active(binding.catalog_id)


async def materialize_catalog_version_pins(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
) -> tuple[CatalogVersionPin, ...]:
    """Resolve every live binding for publication into deterministic exact pins."""

    pins: list[CatalogVersionPin] = []
    seen_types: set[str] = set()
    catalogs = CatalogRepository(session, context)
    for binding in await CatalogBindingRepository(session, context).list_for_stream(stream_id):
        catalog = await catalogs.get(binding.catalog_id)
        version = await resolve_catalog_version(session, context, binding=binding)
        if catalog is None:
            raise CatalogError(f"catalog binding {binding.id} references a missing catalog")
        if version is None or version.catalog_id != catalog.id:
            raise CatalogError(f"catalog binding {binding.id} resolves to no usable version")
        if version.state not in (VersionState.PUBLISHED.value, VersionState.SUPERSEDED.value):
            raise CatalogError(f"catalog binding {binding.id} resolves to a mutable version")
        if catalog.catalog_type in seen_types:
            raise CatalogError(
                f"the stream has more than one {catalog.catalog_type} catalog bound; "
                "publish requires one deterministic version per catalog type"
            )
        seen_types.add(catalog.catalog_type)
        pins.append(
            CatalogVersionPin(
                binding_id=binding.id,
                catalog_id=catalog.id,
                catalog_type=catalog.catalog_type,
                catalog_version_id=version.id,
            )
        )
    return tuple(sorted(pins, key=lambda pin: (pin.catalog_type, pin.catalog_id.hex)))


async def validate_catalog_version_pins(
    session: AsyncSession,
    context: OrganizationContext,
    pins: Sequence[CatalogVersionPin],
) -> tuple[CatalogVersionPin, ...]:
    """Authenticate referenced immutable rows without consulting today's bindings."""

    catalog_repo = CatalogRepository(session, context)
    version_repo = CatalogVersionRepository(session, context)
    for pin in pins:
        catalog = await catalog_repo.get(pin.catalog_id)
        version = await version_repo.get(pin.catalog_version_id)
        if catalog is None or catalog.catalog_type != pin.catalog_type:
            raise CatalogError(f"pinned {pin.catalog_type} catalog is missing or inconsistent")
        if version is None or version.catalog_id != catalog.id:
            raise CatalogError(f"pinned {pin.catalog_type} catalog version is missing")
        if version.state not in (VersionState.PUBLISHED.value, VersionState.SUPERSEDED.value):
            raise CatalogError(f"pinned {pin.catalog_type} catalog version is mutable")
    return tuple(pins)


__all__ = [
    "CATALOG_SOURCES",
    "CATALOG_TYPES",
    "CATALOG_VERSION_PINS_KEY",
    "Catalog",
    "CatalogBinding",
    "CatalogBindingMode",
    "CatalogBindingRepository",
    "CatalogError",
    "CatalogRecord",
    "CatalogRecordRepository",
    "CatalogRepository",
    "CatalogVersion",
    "CatalogVersionPin",
    "CatalogVersionRepository",
    "activate_catalog_version",
    "add_catalog_record",
    "bind_catalog_to_stream",
    "create_catalog",
    "create_catalog_version",
    "materialize_catalog_version_pins",
    "parse_catalog_version_pins",
    "resolve_catalog_version",
    "validate_catalog_version_pins",
]
