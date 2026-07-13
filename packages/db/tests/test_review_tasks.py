"""Review-task tests (REV-001): routing (one active task, supersede),
linked reasons, and the claim/release/complete/cancel state matrix."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext
from soa_db.review_comments import (
    MAX_COMMENT_LENGTH,
    ReviewCommentRepository,
    add_comment,
    extract_mentions,
)
from soa_db.review_tasks import (
    InvalidReviewTaskTransitionError,
    ReviewTask,
    ReviewTaskRepository,
    cancel_active_task_for_document,
    cancel_task,
    claim_task,
    complete_task,
    escalate_task,
    release_task,
    route_document_to_review,
)

ORG_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
DOC = uuid.UUID("33333333-3333-4333-8333-333333333333")
RUN_1 = uuid.UUID("55555555-5555-4555-8555-555555555555")
RUN_2 = uuid.UUID("66666666-6666-4666-8666-666666666666")
CONTEXT = OrganizationContext(organization_id=ORG_A)

REASONS = [
    {
        "code": "low_confidence",
        "message": "confidence 0.60 is below the critical gate of 0.98",
        "field_key": "po_number",
        "row_index": None,
        "rule_key": None,
    },
    {
        "code": "rule_triggered",
        "message": "totals disagree",
        "field_key": None,
        "row_index": None,
        "rule_key": "totals.header_matches_lines",
    },
]


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/review.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def make_task(db: DatabaseSessions, run_id: uuid.UUID = RUN_1) -> uuid.UUID:
    async with db.session_scope() as session:
        task = await route_document_to_review(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=run_id,
            reasons=REASONS,
            priority=50,
        )
        return task.id


# -- routing ---------------------------------------------------------------------


async def test_routing_creates_a_task_with_linked_reasons(db: DatabaseSessions) -> None:
    task_id = await make_task(db)
    async with db.session_scope() as session:
        task = await ReviewTaskRepository(session, CONTEXT).get(task_id)
        assert task is not None
        assert task.state == "open"
        assert task.priority == 50
        assert task.version == 1
        assert [r["code"] for r in task.reasons] == ["low_confidence", "rule_triggered"]
        assert task.reasons[0]["field_key"] == "po_number"
        assert task.reasons[1]["rule_key"] == "totals.header_matches_lines"


async def test_unattributed_or_empty_reasons_are_refused(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        with pytest.raises(ValueError, match="at least one reason"):
            await route_document_to_review(
                session, CONTEXT, document_id=DOC, run_id=RUN_1, reasons=[]
            )
        with pytest.raises(ValueError, match="field_key or rule_key"):
            await route_document_to_review(
                session,
                CONTEXT,
                document_id=DOC,
                run_id=RUN_1,
                reasons=[{"code": "vibes", "message": "needs review"}],
            )


async def test_one_active_task_per_document_reroute_supersedes(db: DatabaseSessions) -> None:
    first_id = await make_task(db, run_id=RUN_1)
    # A fresh run routes again: the stale task is cancelled, not duplicated.
    second_id = await make_task(db, run_id=RUN_2)
    async with db.session_scope() as session:
        repo = ReviewTaskRepository(session, CONTEXT)
        first = await repo.get(first_id)
        second = await repo.get(second_id)
        assert first is not None and second is not None
        assert first.state == "cancelled"
        assert first.outcome == "superseded"
        assert second.state == "open"
        active = await repo.get_active_for_document(DOC)
        assert active is not None and active.id == second_id


async def test_the_partial_unique_index_backstops_the_invariant(db: DatabaseSessions) -> None:
    await make_task(db)
    with pytest.raises(IntegrityError):
        async with db.session_scope() as session:
            # Bypass the routing helper: the database still refuses.
            ReviewTaskRepository(session, CONTEXT).add(
                ReviewTask(document_id=DOC, run_id=RUN_2, reasons=REASONS, priority=10)
            )
            await session.flush()


# -- state matrix ------------------------------------------------------------------


async def test_claim_release_complete_lifecycle(db: DatabaseSessions) -> None:
    task_id = await make_task(db)
    async with db.session_scope() as session:
        repo = ReviewTaskRepository(session, CONTEXT)
        task = await repo.get(task_id)
        assert task is not None
        await claim_task(session, CONTEXT, task=task, user_id="user:reviewer-1")
        assert task.state == "in_progress"
        assert task.assigned_to == "user:reviewer-1"
        assert task.assigned_at is not None

        await release_task(session, CONTEXT, task=task, actor_id="user:reviewer-1")
        assert task.state == "open"
        assert task.assigned_to is None

        await claim_task(session, CONTEXT, task=task, user_id="user:reviewer-2")
        await complete_task(
            session, CONTEXT, task=task, outcome="approved", actor_id="user:reviewer-2"
        )
        assert task.state == "completed"
        assert task.outcome == "approved"
        assert task.completed_by == "user:reviewer-2"
        assert task.completed_at is not None
        assert task.version > 1  # optimistic locking moved with every step


async def test_invalid_transitions_are_refused(db: DatabaseSessions) -> None:
    task_id = await make_task(db)
    async with db.session_scope() as session:
        repo = ReviewTaskRepository(session, CONTEXT)
        task = await repo.get(task_id)
        assert task is not None
        # Completing an unclaimed task skips the claim: refused.
        with pytest.raises(InvalidReviewTaskTransitionError):
            await complete_task(session, CONTEXT, task=task, outcome="approved", actor_id="user:x")
        # Releasing an open task is meaningless: refused.
        with pytest.raises(InvalidReviewTaskTransitionError):
            await release_task(session, CONTEXT, task=task, actor_id="user:x")
        await claim_task(session, CONTEXT, task=task, user_id="user:x")
        await complete_task(session, CONTEXT, task=task, outcome="rejected", actor_id="user:x")
        # Completed is settled: nothing moves it.
        with pytest.raises(InvalidReviewTaskTransitionError):
            await claim_task(session, CONTEXT, task=task, user_id="user:y")
        with pytest.raises(InvalidReviewTaskTransitionError):
            await cancel_task(session, CONTEXT, task=task, reason="late", actor_id="user:y")


async def test_unknown_outcomes_are_refused(db: DatabaseSessions) -> None:
    task_id = await make_task(db)
    async with db.session_scope() as session:
        task = await ReviewTaskRepository(session, CONTEXT).get(task_id)
        assert task is not None
        await claim_task(session, CONTEXT, task=task, user_id="user:x")
        with pytest.raises(ValueError, match="'approved' or 'rejected'"):
            await complete_task(session, CONTEXT, task=task, outcome="shrugged", actor_id="user:x")


async def test_cancel_active_task_for_document_helper(db: DatabaseSessions) -> None:
    await make_task(db)
    async with db.session_scope() as session:
        cancelled = await cancel_active_task_for_document(
            session,
            CONTEXT,
            document_id=DOC,
            reason="document cancelled",
            actor_id="user:x",
        )
        assert cancelled is not None
        assert cancelled.state == "cancelled"
        assert cancelled.outcome == "document cancelled"
        # Idempotent: nothing active remains.
        assert (
            await cancel_active_task_for_document(
                session, CONTEXT, document_id=DOC, reason="again", actor_id="user:x"
            )
            is None
        )


async def test_tasks_are_tenant_scoped(db: DatabaseSessions) -> None:
    await make_task(db)
    async with db.session_scope() as session:
        other = OrganizationContext(organization_id=ORG_B)
        assert await ReviewTaskRepository(session, other).list_active() == []

    from soa_db.tenant_guard import RLS_PROTECTED_TABLES

    assert "review_tasks" in RLS_PROTECTED_TABLES


# -- escalation (REV-011) ----------------------------------------------------------


async def test_escalate_returns_in_progress_task_to_open_with_ownership(
    db: DatabaseSessions,
) -> None:
    task_id = await make_task(db)
    async with db.session_scope() as session:
        task = await ReviewTaskRepository(session, CONTEXT).get(task_id)
        assert task is not None
        await claim_task(session, CONTEXT, task=task, user_id="user:reviewer")
        await escalate_task(
            session, CONTEXT, task=task, reason="needs supervisor", actor_id="user:reviewer"
        )
        assert task.state == "open"
        assert task.assigned_to is None
        assert task.priority == 10  # min(50, 10): escalations jump the queue
        assert task.escalated_by == "user:reviewer"
        assert task.escalation_reason == "needs supervisor"
        assert task.escalated_at is not None
        # Still claimable after escalation.
        await claim_task(session, CONTEXT, task=task, user_id="user:supervisor")
        assert task.state == "in_progress"


async def test_escalate_refuses_settled_tasks_and_blank_reasons(db: DatabaseSessions) -> None:
    task_id = await make_task(db)
    async with db.session_scope() as session:
        task = await ReviewTaskRepository(session, CONTEXT).get(task_id)
        assert task is not None
        with pytest.raises(ValueError, match="needs a reason"):
            await escalate_task(session, CONTEXT, task=task, reason="   ", actor_id="user:x")
        # An OPEN task escalates in place (no release needed).
        await escalate_task(session, CONTEXT, task=task, reason="stuck", actor_id="user:x")
        assert task.state == "open"
        await claim_task(session, CONTEXT, task=task, user_id="user:x")
        await complete_task(session, CONTEXT, task=task, outcome="approved", actor_id="user:x")
        with pytest.raises(InvalidReviewTaskTransitionError):
            await escalate_task(session, CONTEXT, task=task, reason="too late", actor_id="user:x")


# -- comments (REV-011) ------------------------------------------------------------


async def test_add_comment_validates_extracts_mentions_and_scopes_by_tenant(
    db: DatabaseSessions,
) -> None:
    assert extract_mentions("ping @alice and @bob.smith — also @alice again") == [
        "alice",
        "bob.smith",
    ]
    assert extract_mentions("no handles here, a@b is too short") == []

    task_id = await make_task(db)
    async with db.session_scope() as session:
        with pytest.raises(ValueError, match="needs a body"):
            await add_comment(
                session, CONTEXT, task_id=task_id, document_id=DOC, author="user:x", body="  "
            )
        with pytest.raises(ValueError, match="limited to"):
            await add_comment(
                session,
                CONTEXT,
                task_id=task_id,
                document_id=DOC,
                author="user:x",
                body="x" * (MAX_COMMENT_LENGTH + 1),
            )
        comment = await add_comment(
            session,
            CONTEXT,
            task_id=task_id,
            document_id=DOC,
            author="user:x",
            body="  @alice please verify the totals  ",
        )
        assert comment.body == "@alice please verify the totals"  # stored trimmed
        assert comment.mentions == ["alice"]

    async with db.session_scope() as session:
        mine = await ReviewCommentRepository(session, CONTEXT).list_for_task(task_id)
        assert [c.body for c in mine] == ["@alice please verify the totals"]
        other = OrganizationContext(organization_id=ORG_B)
        assert await ReviewCommentRepository(session, other).list_for_task(task_id) == []

    from soa_db.tenant_guard import RLS_PROTECTED_TABLES

    assert "review_comments" in RLS_PROTECTED_TABLES
