"""Review comments (REV-011).

Comments are tenant-scoped, audited, and append-only in practice (there
is no edit surface). Mentions are extracted at write time as plain
``@token`` handles — resolution against real users arrives when a user
directory lookup exists; storing the tokens today keeps the data ready
without pretending to notify anyone.

NOTIFICATION SAFETY: the outbox event announcing a comment carries ids
and the author only — never the comment body. Comment text can contain
customer order details; notification channels (email, chat) are not
storage and do not get it.
"""

import re
import uuid
from typing import Any

from sqlalchemy import String, Text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.outbox import PORTABLE_JSON, enqueue_event
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID

_MENTION = re.compile(r"@([A-Za-z0-9._-]{2,64})")
MAX_COMMENT_LENGTH = 4000


class ReviewComment(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    __tablename__ = "review_comments"

    task_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    author: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text(), nullable=False)
    #: Plain @handles found in the body; resolution is a future concern.
    mentions: Mapped[list[Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=list)


class ReviewCommentRepository(ScopedRepository[ReviewComment]):
    model = ReviewComment

    async def list_for_task(self, task_id: uuid.UUID) -> list[ReviewComment]:
        stmt = (
            self._scoped_select()
            .where(ReviewComment.task_id == task_id)
            .order_by(ReviewComment.created_at, ReviewComment.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())


def extract_mentions(body: str) -> list[str]:
    return sorted({match.group(1) for match in _MENTION.finditer(body)})


async def add_comment(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    task_id: uuid.UUID,
    document_id: uuid.UUID,
    author: str,
    body: str,
) -> ReviewComment:
    text = body.strip()
    if not text:
        raise ValueError("a comment needs a body")
    if len(text) > MAX_COMMENT_LENGTH:
        raise ValueError(f"comments are limited to {MAX_COMMENT_LENGTH} characters")
    comment = ReviewCommentRepository(session, context).add(
        ReviewComment(
            task_id=task_id,
            document_id=document_id,
            author=author,
            body=text,
            mentions=extract_mentions(text),
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=author,
        action="review.comment_added",
        target_type="review_task",
        target_id=str(task_id),
        organization_id=context.organization_id,
        # Length only — audit summaries are widely readable; the body
        # stays in the comment row.
        summary={"comment_id": str(comment.id), "length": len(text)},
    )
    # Notification event WITHOUT the body: channels never see raw content.
    await enqueue_event(
        session,
        event_type="review.comment_added",
        payload={
            "comment_id": str(comment.id),
            "task_id": str(task_id),
            "document_id": str(document_id),
            "author": author,
            "mentions": comment.mentions,
        },
        organization_id=context.organization_id,
        dedupe_key=f"review.comment_added:{comment.id}",
    )
    return comment
