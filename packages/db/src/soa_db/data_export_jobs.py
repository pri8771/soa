"""Durable, resumable organization/document data-export jobs."""

import uuid
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow


class DataExportState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DataExportJob(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "data_export_jobs"

    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    total_documents: Mapped[int] = mapped_column(nullable=False, default=0)
    processed_documents: Mapped[int] = mapped_column(nullable=False, default=0)
    total_records: Mapped[int] = mapped_column(nullable=False, default=0)
    cursor_document_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    parts: Mapped[list[Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=list)
    manifest_object_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    safe_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class DataExportJobRepository(ScopedRepository[DataExportJob]):
    model = DataExportJob


def new_data_export_job(
    *,
    organization_id: uuid.UUID,
    scope: str,
    created_by: str,
    document_id: uuid.UUID | None = None,
    retention_days: int = 7,
) -> DataExportJob:
    if scope not in ("organization", "document"):
        raise ValueError("data export scope must be organization or document")
    if scope == "document" and document_id is None:
        raise ValueError("document exports must name a document")
    return DataExportJob(
        organization_id=organization_id,
        scope=scope,
        document_id=document_id,
        created_by=created_by,
        expires_at=utcnow() + timedelta(days=retention_days),
    )


__all__ = ["DataExportJob", "DataExportJobRepository", "DataExportState", "new_data_export_job"]
