"""Tenant-scoped repository base.

Tenant isolation rule (docs/AGENTS.md §6, ADR-011): every tenant-owned query
runs through a repository that REQUIRES an ``OrganizationContext``. There is
deliberately no unscoped variant and no way to construct one — a repository
class whose model lacks ``organization_id`` fails at import time.

Pagination and row locking are explicit parameters, never hidden defaults.
"""

import uuid
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, Protocol, TypeVar, runtime_checkable

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, declarative_mixin, mapped_column

from soa_db.pagination import CursorRequest, Page, build_page
from soa_db.types import GUID


class TenantMismatchError(Exception):
    """An entity's organization does not match the repository's context."""


@dataclass(frozen=True)
class OrganizationContext:
    """Explicit tenant scope. Constructed at the authorization boundary
    (API auth / worker job re-authorization), never inferred."""

    organization_id: uuid.UUID


@declarative_mixin
class OrganizationScopedMixin:
    organization_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)


@runtime_checkable
class _TenantOwned(Protocol):
    organization_id: Any
    id: Any


ModelT = TypeVar("ModelT", bound=_TenantOwned)


class ScopedRepository(Generic[ModelT]):
    """Base class for tenant-owned aggregates.

    Subclasses set ``model``. Every read filters by the context organization;
    every write stamps or validates it.
    """

    model: ClassVar[type[Any]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        model = getattr(cls, "model", None)
        if model is None:
            raise TypeError(f"{cls.__name__} must define a 'model' class attribute")
        if not hasattr(model, "organization_id"):
            raise TypeError(
                f"{cls.__name__}.model ({model.__name__}) has no organization_id column; "
                "tenant-owned repositories require organization scope"
            )

    def __init__(self, session: AsyncSession, context: OrganizationContext) -> None:
        self._session = session
        self._context = context

    @property
    def organization_id(self) -> uuid.UUID:
        return self._context.organization_id

    def _scoped_select(self) -> Select[tuple[ModelT]]:
        return select(self.model).where(self.model.organization_id == self.organization_id)

    async def get(self, entity_id: uuid.UUID, *, for_update: bool = False) -> ModelT | None:
        """Fetch one entity by ID within the tenant scope.

        ``for_update`` takes a row lock (SELECT ... FOR UPDATE) — explicit,
        for workflows that read-modify-write.
        """
        stmt = self._scoped_select().where(self.model.id == entity_id)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_page(self, request: CursorRequest) -> Page[ModelT]:
        """Cursor-paginated listing ordered by ID, scoped to the tenant."""
        stmt = self._scoped_select().order_by(self.model.id).limit(request.limit + 1)
        if request.after is not None:
            stmt = stmt.where(self.model.id > request.after)
        result = await self._session.execute(stmt)
        rows = list(result.scalars().all())
        return build_page(rows, request.limit, id_of=lambda row: row.id)

    async def count(self) -> int:
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.organization_id == self.organization_id)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    def add(self, entity: ModelT) -> ModelT:
        """Stage an entity, stamping or validating its organization."""
        current = getattr(entity, "organization_id", None)
        if current is None:
            entity.organization_id = self.organization_id
        elif current != self.organization_id:
            raise TenantMismatchError(
                f"entity belongs to organization {current}, "
                f"repository is scoped to {self.organization_id}"
            )
        self._session.add(entity)
        return entity
