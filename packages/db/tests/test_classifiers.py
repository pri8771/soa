"""Classifier routing tables: closed-shape validation, the version
lifecycle, and the deterministic v1 matcher."""

import uuid
from pathlib import Path

import pytest

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.classifiers import (
    ClassifierValidationError,
    ClassifierVersionRepository,
    create_classifier_draft,
    match_route,
    publish_classifier_draft,
    validate_classifier_content,
)
from soa_db.repository import OrganizationContext
from soa_db.versioning import ImmutableVersionError

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTEXT = OrganizationContext(organization_id=ORG)
STREAM = uuid.UUID("22222222-2222-4222-8222-222222222222")
TARGET_A = str(uuid.UUID("33333333-3333-4333-8333-333333333333"))
TARGET_B = str(uuid.UUID("44444444-4444-4444-8444-444444444444"))

CONTENT = {
    "routes": [
        {"label": "pharma", "target_stream_id": TARGET_A, "signals": ["Mawdsley", "Phoenix"]},
        {"label": "retail", "target_stream_id": TARGET_B, "signals": ["Ashgrove"]},
    ]
}


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/classifiers.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


class TestContentValidation:
    def test_valid_routing_table_passes(self) -> None:
        validate_classifier_content(CONTENT)

    def test_routes_are_required(self) -> None:
        with pytest.raises(ClassifierValidationError, match="routes"):
            validate_classifier_content({"routes": []})

    def test_duplicate_labels_are_refused(self) -> None:
        with pytest.raises(ClassifierValidationError, match="twice"):
            validate_classifier_content(
                {
                    "routes": [
                        {"label": "a", "target_stream_id": TARGET_A, "signals": ["x"]},
                        {"label": "a", "target_stream_id": TARGET_B, "signals": ["y"]},
                    ]
                }
            )

    def test_target_must_be_a_uuid(self) -> None:
        with pytest.raises(ClassifierValidationError, match="target_stream_id"):
            validate_classifier_content(
                {"routes": [{"label": "a", "target_stream_id": "spain", "signals": ["x"]}]}
            )

    def test_the_shape_is_closed(self) -> None:
        with pytest.raises(ClassifierValidationError, match="unknown"):
            validate_classifier_content({**CONTENT, "fallback": "review"})
        with pytest.raises(ClassifierValidationError, match="unknown"):
            validate_classifier_content(
                {
                    "routes": [
                        {
                            "label": "a",
                            "target_stream_id": TARGET_A,
                            "signals": ["x"],
                            "confidence": 0.5,
                        }
                    ]
                }
            )


class TestLifecycle:
    async def test_draft_publish_supersede(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            first = await create_classifier_draft(
                session, CONTEXT, stream_id=STREAM, content=CONTENT, actor_id="user:u-1"
            )
            published = await publish_classifier_draft(
                session, CONTEXT, draft=first, actor_id="user:u-1"
            )
            assert published.state == "published"
            assert published.reference == f"classifier:{published.id}:v1"

            second = await create_classifier_draft(
                session, CONTEXT, stream_id=STREAM, content=CONTENT, actor_id="user:u-1"
            )
            assert second.version_number == 2
            await publish_classifier_draft(session, CONTEXT, draft=second, actor_id="user:u-1")
            live = await ClassifierVersionRepository(session, CONTEXT).get_published(STREAM)
            assert live is not None and live.version_number == 2

    async def test_published_content_is_immutable(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            draft = await create_classifier_draft(
                session, CONTEXT, stream_id=STREAM, content=CONTENT, actor_id="user:u-1"
            )
            await publish_classifier_draft(session, CONTEXT, draft=draft, actor_id="user:u-1")
        async with db.session_scope() as session:
            live = await ClassifierVersionRepository(session, CONTEXT).get_published(STREAM)
            assert live is not None
            live.content = {"routes": []}
            with pytest.raises(ImmutableVersionError):
                await session.flush()
            await session.rollback()


class TestMatcher:
    def test_most_signal_hits_wins(self) -> None:
        decision = match_route(CONTENT, "Order from MAWDSLEY and Phoenix Healthcare, Leeds")
        assert decision is not None
        assert decision["label"] == "pharma"
        assert decision["target_stream_id"] == TARGET_A
        assert decision["matched_signals"] == ["Mawdsley", "Phoenix"]
        assert decision["score"] == 2

    def test_no_hits_is_unrouted(self) -> None:
        assert match_route(CONTENT, "Completely unrelated packing list") is None

    def test_ties_go_to_a_human(self) -> None:
        # One hit each — ambiguous, never guess.
        assert match_route(CONTENT, "Mawdsley sells to Ashgrove") is None

    def test_matching_is_case_insensitive_and_whitespace_tolerant(self) -> None:
        decision = match_route(CONTENT, "ASHGROVE   Industrial\nSupplies")
        assert decision is not None and decision["label"] == "retail"

    def test_empty_text_is_unrouted(self) -> None:
        assert match_route(CONTENT, "") is None
