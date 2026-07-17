"""Skills overview — the home screen's per-skill operating summary.

"Skill" is the product name for a stream: the trained, versioned extraction
capability a document is routed to. The home groups skills by their parent
process (the bucket grouping until classifier-driven intakes land) and shows
the numbers an operator triages by: review queue depth, 30-day volume, the
latest measured field accuracy, and the training state.
"""

import uuid
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.processes import Process
from soa_api.domain.streams import Stream
from soa_db.documents import Document, DocumentState
from soa_db.evaluation_runs import EvaluationRun, EvaluationRunState
from soa_db.gold_datasets import GoldDataset, GoldDatasetVersion
from soa_db.types import utcnow
from soa_db.versioning import VersionState

router = APIRouter(tags=["skills"])


@router.get("/orgs/{organization_slug}/skills")
async def skills_overview(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> dict[str, Any]:
    """Every non-archived skill with its operating numbers, grouped by
    process. One round trip; each aggregate is a single grouped query."""
    org = authorized.org_context.organization_id
    since = utcnow() - timedelta(days=30)

    streams = (
        (
            await session.execute(
                select(Stream).where(Stream.organization_id == org, Stream.status != "archived")
            )
        )
        .scalars()
        .all()
    )
    processes = {
        row.id: row
        for row in (
            (await session.execute(select(Process).where(Process.organization_id == org)))
            .scalars()
            .all()
        )
    }

    in_review: dict[uuid.UUID, int] = {}
    for stream_id, count in await session.execute(
        select(Document.stream_id, func.count())
        .where(
            Document.organization_id == org,
            Document.state == DocumentState.REVIEW_REQUIRED.value,
        )
        .group_by(Document.stream_id)
    ):
        in_review[stream_id] = int(count)
    received_30d: dict[uuid.UUID, int] = {}
    for stream_id, count in await session.execute(
        select(Document.stream_id, func.count())
        .where(Document.organization_id == org, Document.received_at >= since)
        .group_by(Document.stream_id)
    ):
        received_30d[stream_id] = int(count)

    # Latest finished evaluation per stream — field accuracy as last measured.
    eval_rows = (
        (
            await session.execute(
                select(EvaluationRun)
                .where(
                    EvaluationRun.organization_id == org,
                    EvaluationRun.state == EvaluationRunState.SUCCEEDED.value,
                )
                .order_by(EvaluationRun.created_at)
            )
        )
        .scalars()
        .all()
    )
    accuracy: dict[Any, float] = {}
    for run in eval_rows:  # ascending order — the last write per stream wins
        rate = (run.report or {}).get("field_exact_rate")
        if isinstance(rate, (int, float)):
            accuracy[run.stream_id] = float(rate)

    # Training state: does the stream have a published training set?
    trained: dict[uuid.UUID, int] = {}
    for stream_id, version in await session.execute(
        select(GoldDataset.stream_id, func.max(GoldDatasetVersion.version_number))
        .join(GoldDatasetVersion, GoldDatasetVersion.dataset_id == GoldDataset.id)
        .where(
            GoldDataset.organization_id == org,
            GoldDataset.stream_id.is_not(None),
            GoldDatasetVersion.state == VersionState.PUBLISHED.value,
        )
        .group_by(GoldDataset.stream_id)
    ):
        trained[stream_id] = int(version)

    skills = []
    for stream in streams:
        process = processes.get(stream.process_id)
        skills.append(
            {
                "id": str(stream.id),
                "slug": stream.slug,
                "name": stream.name,
                "status": stream.status,
                "process_slug": process.slug if process else None,
                "process_name": process.name if process else None,
                "in_review": int(in_review.get(stream.id, 0)),
                "received_30d": int(received_30d.get(stream.id, 0)),
                "field_accuracy": accuracy.get(stream.id),
                "trained_version": trained.get(stream.id),
            }
        )
    skills.sort(key=lambda s: (s["process_name"] or "", s["name"]))
    return {"items": skills}
