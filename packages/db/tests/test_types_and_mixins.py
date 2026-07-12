import asyncio
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError

from soa_db import (
    Base,
    DatabaseSessions,
    TimestampMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    VersionConflictError,
    VersionedMixin,
    create_database_engine,
    currency_column,
    money_column,
    uuid7,
)


class Order(UuidPrimaryKeyMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "test_order"
    total: Mapped[Decimal] = money_column()
    currency: Mapped[str] = currency_column()
    note: Mapped[str] = mapped_column(default="")
    happened_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


@pytest.fixture
async def sessions(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/types.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_uuid_pk_money_and_timestamps_round_trip(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        order = Order(total=Decimal("1315.92"), currency="GBP")
        session.add(order)
    async with sessions.session_scope() as session:
        loaded = (await session.execute(select(Order))).scalar_one()
    assert isinstance(loaded.id, uuid.UUID)
    assert loaded.total == Decimal("1315.92")
    assert isinstance(loaded.total, Decimal), "money must never come back as float"
    assert loaded.currency == "GBP"
    assert loaded.created_at.tzinfo is not None
    assert loaded.updated_at.tzinfo is not None
    assert loaded.version == 1
    await sessions.dispose()


async def test_naive_datetime_is_rejected(sessions: DatabaseSessions) -> None:
    with pytest.raises(Exception, match="timezone-aware"):
        async with sessions.session_scope() as session:
            session.add(
                Order(
                    total=Decimal("1"),
                    currency="EUR",
                    happened_at=datetime(2026, 7, 12, 12, 0, 0),  # naive
                )
            )
    await sessions.dispose()


async def test_aware_datetime_comes_back_as_utc(sessions: DatabaseSessions) -> None:
    plus_two = timezone(timedelta(hours=2))
    local = datetime(2026, 7, 12, 14, 30, 0, tzinfo=plus_two)
    async with sessions.session_scope() as session:
        order = Order(total=Decimal("1"), currency="EUR", happened_at=local)
        session.add(order)
    async with sessions.session_scope() as session:
        loaded = (await session.execute(select(Order))).scalar_one()
    assert loaded.happened_at is not None
    assert loaded.happened_at.tzinfo is not None
    assert loaded.happened_at.astimezone(UTC).hour == 12
    await sessions.dispose()


async def test_stale_concurrent_update_raises(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        order = Order(total=Decimal("10"), currency="GBP")
        session.add(order)
    order_id = order.id

    async with sessions.session_scope() as first:
        async with sessions.session_scope() as second:
            a = (await first.execute(select(Order).where(Order.id == order_id))).scalar_one()
            b = (await second.execute(select(Order).where(Order.id == order_id))).scalar_one()
            b.note = "second writer wins"
            await second.commit()
            a.note = "first writer is stale"
            with pytest.raises(StaleDataError):
                await first.commit()
            await first.rollback()
    await sessions.dispose()


async def test_version_increments_on_update(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        order = Order(total=Decimal("10"), currency="GBP")
        session.add(order)
    order_id = order.id
    async with sessions.session_scope() as session:
        loaded = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        loaded.note = "edited"
    async with sessions.session_scope() as session:
        reloaded = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
    assert reloaded.version == 2
    await sessions.dispose()


def test_expect_version_raises_on_mismatch() -> None:
    order = Order(total=Decimal("5"), currency="EUR")
    order.version = 3
    order.expect_version(3)
    with pytest.raises(VersionConflictError, match="expected 2, found 3"):
        order.expect_version(2)


async def test_uuid7_is_time_sortable() -> None:
    first = uuid7()
    await asyncio.sleep(0.002)
    second = uuid7()
    assert first.version == 7
    assert second.version == 7
    assert first.int < second.int
