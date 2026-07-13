"""Page and text-artifact models (PRC-005).

Pages belong to the PROCESSING RUN that rendered them: reprocessing a
document produces a fresh page set under the new run, and the old one
stays with its run for evidence traceability.

Page numbering is 1-based and stable: ``page_number`` is unique per run
and never renumbered — page 3 of run 2 means the same physical page for
the lifetime of that run.

Coordinate system (documented once, used by every evidence consumer):

- Origin is the TOP-LEFT corner of the rendered page image.
- ``x`` grows rightward, ``y`` grows downward.
- Units are PIXELS of the rendered raster whose dimensions are stored
  here (``width_px`` by ``height_px`` at ``dpi``). Evidence polygons are
  lists of ``[x, y]`` vertices in this space; consumers rescale by
  comparing against the stored dimensions, never by assuming a DPI.

Each page references its artifacts by id: the rendered image (always),
and the text/layout artifacts once extraction produces them (nullable
until then — never fake references).
"""

import uuid

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID


class DocumentPage(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    __tablename__ = "document_pages"

    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(nullable=False)
    width_px: Mapped[int] = mapped_column(nullable=False)
    height_px: Mapped[int] = mapped_column(nullable=False)
    dpi: Mapped[int | None] = mapped_column(nullable=True)
    #: The rendered page image (artifacts row) — always present.
    image_artifact_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    #: Extraction outputs; null until the stage that produces them runs.
    text_artifact_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    layout_artifact_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: Original page rotation applied during rendering, degrees clockwise.
    rotation_degrees: Mapped[int] = mapped_column(nullable=False, default=0)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False, default="image/png")

    __table_args__ = (UniqueConstraint("run_id", "page_number"),)


class DocumentPageRepository(ScopedRepository[DocumentPage]):
    model = DocumentPage

    async def list_for_run(self, run_id: uuid.UUID) -> list[DocumentPage]:
        stmt = (
            self._scoped_select()
            .where(DocumentPage.run_id == run_id)
            .order_by(DocumentPage.page_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())


async def create_page(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    run_id: uuid.UUID,
    page_number: int,
    width_px: int,
    height_px: int,
    dpi: int | None,
    image_artifact_id: uuid.UUID,
    rotation_degrees: int = 0,
) -> DocumentPage:
    if page_number < 1:
        raise ValueError("page numbering is 1-based")
    if width_px <= 0 or height_px <= 0:
        raise ValueError("page dimensions must be positive")
    page = DocumentPageRepository(session, context).add(
        DocumentPage(
            document_id=document_id,
            run_id=run_id,
            page_number=page_number,
            width_px=width_px,
            height_px=height_px,
            dpi=dpi,
            image_artifact_id=image_artifact_id,
            rotation_degrees=rotation_degrees,
        )
    )
    await session.flush()
    return page
