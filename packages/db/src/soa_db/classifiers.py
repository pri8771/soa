"""Versioned document classifiers — the routing table on an intake stream.

A classifier turns an intake stream (bucket) into a router: documents arriving
there are classified on their own text and re-routed to the matching skill
(stream) before extraction. The content is a CLOSED routing table:

    {"routes": [{"label": "pharma-wholesale",
                 "target_stream_id": "<uuid>",
                 "signals": ["mawdsley", "phoenix healthcare"]}]}

v1 matching is deterministic and auditable: a route matches when any of its
signals (customer names, letterheads) appears in the document's native text;
the route with the most hits wins, ties or zero hits leave the document
unrouted for a human decision. Versioning follows the CFG discipline —
draft → published → superseded, published rows immutable — so every routing
decision is attributable to an exact classifier version.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow
from soa_db.versioning import (
    ImmutablePublishedVersionMixin,
    InvalidVersionStateError,
    VersionState,
)

MAX_ROUTES = 20
MAX_SIGNALS_PER_ROUTE = 50
MAX_SIGNAL_CHARS = 100


class ClassifierValidationError(ValueError):
    pass


def validate_classifier_content(content: dict[str, Any]) -> None:
    routes = content.get("routes")
    if not isinstance(routes, list):
        raise ClassifierValidationError("content needs a 'routes' list")
    # An empty list is valid and publishable: it is the degenerate "disabled"
    # case (docs/ROUTING.md) -- the classify stage treats it identically to
    # no published classifier at all, not "nothing can ever match".
    if len(routes) > MAX_ROUTES:
        raise ClassifierValidationError(f"'routes' exceeds {MAX_ROUTES} entries")
    labels: set[str] = set()
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            raise ClassifierValidationError(f"routes[{index}] must be an object")
        label = route.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            raise ClassifierValidationError(f"routes[{index}].label must be 1..100 characters")
        if label in labels:
            raise ClassifierValidationError(f"route label {label!r} appears twice")
        labels.add(label)
        target = route.get("target_stream_id")
        try:
            uuid.UUID(str(target))
        except (ValueError, TypeError):
            raise ClassifierValidationError(
                f"routes[{index}].target_stream_id must be a stream UUID"
            ) from None
        signals = route.get("signals")
        if not isinstance(signals, list) or not signals:
            raise ClassifierValidationError(f"routes[{index}].signals must be a non-empty list")
        if len(signals) > MAX_SIGNALS_PER_ROUTE:
            raise ClassifierValidationError(
                f"routes[{index}].signals exceeds {MAX_SIGNALS_PER_ROUTE} entries"
            )
        for signal in signals:
            if not isinstance(signal, str) or not signal.strip() or len(signal) > MAX_SIGNAL_CHARS:
                raise ClassifierValidationError(
                    f"routes[{index}] signals must be 1..{MAX_SIGNAL_CHARS} character strings"
                )
        unknown = set(route) - {"label", "target_stream_id", "signals"}
        if unknown:
            raise ClassifierValidationError(f"routes[{index}] has unknown keys: {sorted(unknown)}")
    unknown = set(content) - {"routes"}
    if unknown:
        raise ClassifierValidationError(
            f"unknown content keys: {sorted(unknown)} — the shape is closed"
        )


class ClassifierVersion(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    ImmutablePublishedVersionMixin,
    TimestampMixin,
    VersionedMixin,
    Base,
):
    __tablename__ = "classifier_versions"

    #: The INTAKE stream this classifier routes for (soft ref, no FK).
    stream_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    change_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "stream_id", "version_number"),
        Index(
            "uq_classifier_versions_single_published",
            "organization_id",
            "stream_id",
            unique=True,
            postgresql_where=text("state = 'published'"),
            sqlite_where=text("state = 'published'"),
        ),
    )

    @property
    def reference(self) -> str:
        """The exact-version string routing decisions record."""
        return f"classifier:{self.id}:v{self.version_number}"


class ClassifierVersionRepository(ScopedRepository[ClassifierVersion]):
    model = ClassifierVersion

    async def list_for_stream(self, stream_id: uuid.UUID) -> list[ClassifierVersion]:
        stmt = (
            self._scoped_select()
            .where(ClassifierVersion.stream_id == stream_id)
            .order_by(ClassifierVersion.version_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_published(self, stream_id: uuid.UUID) -> ClassifierVersion | None:
        stmt = self._scoped_select().where(
            ClassifierVersion.stream_id == stream_id,
            ClassifierVersion.state == VersionState.PUBLISHED,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


def _content_summary(content: dict[str, Any]) -> dict[str, Any]:
    routes = content.get("routes", []) or []
    return {"routes": len(routes)}


async def create_classifier_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
    content: dict[str, Any],
    change_summary: str | None = None,
    actor_id: str,
) -> ClassifierVersion:
    validate_classifier_content(content)
    repo = ClassifierVersionRepository(session, context)
    versions = await repo.list_for_stream(stream_id)
    next_number = (versions[-1].version_number + 1) if versions else 1
    draft = repo.add(
        ClassifierVersion(
            stream_id=stream_id,
            version_number=next_number,
            content=dict(content),
            change_summary=change_summary,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="classifier.draft_created",
        target_type="classifier_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={
            "stream_id": str(stream_id),
            "version_number": next_number,
            **_content_summary(content),
        },
    )
    return draft


async def update_classifier_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    draft: ClassifierVersion,
    content: dict[str, Any],
    change_summary: str | None = None,
    actor_id: str,
) -> ClassifierVersion:
    if draft.state != VersionState.DRAFT:
        raise InvalidVersionStateError(f"only drafts are editable; this version is {draft.state!r}")
    validate_classifier_content(content)
    draft.content = dict(content)
    if change_summary is not None:
        draft.change_summary = change_summary
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="classifier.draft_updated",
        target_type="classifier_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary=_content_summary(content),
    )
    return draft


async def publish_classifier_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    draft: ClassifierVersion,
    actor_id: str,
    now: datetime | None = None,
) -> ClassifierVersion:
    if draft.state != VersionState.DRAFT:
        raise InvalidVersionStateError(f"only drafts publish; this version is {draft.state!r}")
    validate_classifier_content(draft.content)
    previous = await ClassifierVersionRepository(session, context).get_published(draft.stream_id)
    if previous is not None:
        previous.state = VersionState.SUPERSEDED
        await session.flush()
    draft.state = VersionState.PUBLISHED
    draft.published_at = now or utcnow()
    draft.published_by = actor_id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="classifier.published",
        target_type="classifier_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={
            "stream_id": str(draft.stream_id),
            "version_number": draft.version_number,
            "reference": draft.reference,
            "superseded_version_id": str(previous.id) if previous else None,
        },
    )
    return draft


def match_route(content: dict[str, Any], document_text: str) -> dict[str, Any] | None:
    """The deterministic v1 routing decision.

    Case-insensitive substring match of each route's signals against the
    document text; the route with the most distinct signal hits wins. Returns
    ``{"label", "target_stream_id", "matched_signals", "score"}`` or ``None``
    when nothing matches or two routes tie (ambiguity goes to a human)."""
    haystack = " ".join(document_text.lower().split())
    best: dict[str, Any] | None = None
    best_score = 0
    tied = False
    for route in content.get("routes", []) or []:
        signals = [s for s in route.get("signals", []) if isinstance(s, str)]
        matched = sorted({s for s in signals if s.lower().strip() in haystack})
        score = len(matched)
        if score == 0:
            continue
        if score > best_score:
            best = {
                "label": route.get("label"),
                "target_stream_id": route.get("target_stream_id"),
                "matched_signals": matched,
                "score": score,
            }
            best_score = score
            tied = False
        elif score == best_score:
            tied = True
    if best is None or tied:
        return None
    return best


__all__ = [
    "MAX_ROUTES",
    "MAX_SIGNALS_PER_ROUTE",
    "MAX_SIGNAL_CHARS",
    "ClassifierValidationError",
    "ClassifierVersion",
    "ClassifierVersionRepository",
    "create_classifier_draft",
    "match_route",
    "publish_classifier_draft",
    "update_classifier_draft",
    "validate_classifier_content",
]
