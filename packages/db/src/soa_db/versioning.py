"""Shared version-lifecycle machinery for configuration records (CFG-001/002).

Any model inheriting ImmutablePublishedVersionMixin gets the invariant:
DRAFT rows edit freely (under optimistic concurrency); PUBLISHED and
SUPERSEDED rows never change or delete again — the sole legal touch is the
published -> superseded state flip performed when a newer version publishes.
"""

import uuid
from enum import StrEnum

from sqlalchemy import String, event, inspect
from sqlalchemy.orm import Mapped, Session, declarative_mixin, mapped_column


class VersionState(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


class ImmutableVersionError(Exception):
    def __init__(self, version_id: uuid.UUID, state: str) -> None:
        super().__init__(
            f"version {version_id} is {state} and immutable — "
            "create a new draft instead of editing history"
        )


class InvalidVersionStateError(Exception):
    pass


@declarative_mixin
class ImmutablePublishedVersionMixin:
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=VersionState.DRAFT)


@event.listens_for(Session, "before_flush")
def _reject_published_mutations(session: Session, flush_context: object, instances: object) -> None:
    for entity in session.dirty:
        if not isinstance(entity, ImmutablePublishedVersionMixin):
            continue
        if not session.is_modified(entity, include_collections=False):
            continue
        insp = inspect(entity)
        assert insp is not None
        state_hist = insp.attrs.state.history
        previous_state = state_hist.deleted[0] if state_hist.deleted else entity.state
        if previous_state in (VersionState.PUBLISHED, VersionState.SUPERSEDED):
            only_state_changed = all(
                attr.key == "state" or not attr.history.has_changes()
                for attr in insp.attrs
                if attr.key not in ("updated_at", "version")
            )
            is_supersede = (
                previous_state == VersionState.PUBLISHED
                and entity.state == VersionState.SUPERSEDED
                and only_state_changed
            )
            if not is_supersede:
                identity = insp.identity
                assert identity is not None
                raise ImmutableVersionError(identity[0], str(previous_state))
    for entity in session.deleted:
        if (
            isinstance(entity, ImmutablePublishedVersionMixin)
            and entity.state != VersionState.DRAFT
        ):
            insp = inspect(entity)
            assert insp is not None and insp.identity is not None
            raise ImmutableVersionError(insp.identity[0], str(entity.state))
