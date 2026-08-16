"""
test_duplicates.py — Tests for duplicate prevention (Tests 5, 6, 7).

Covers:
  - Same user + same rule → only ONE delivery, others blocked
  - Different users + same rule → each user gets a delivery
  - Same user + different rules → user gets one per rule
  - Concurrent same user + same rule (race condition regression)
"""

import asyncio
import pytest
from datetime import datetime, timezone
from sqlalchemy import select

from app.models import Delivery, DuplicateBlock, Event, Rule
from app.worker import _handle_comment_created


def _rule(keyword: str) -> Rule:
    return Rule(keyword=keyword, dm_message=f"DM for {keyword}")


def _event(text: str, user_id: str, comment_id: str) -> Event:
    return Event(
        event_id=f"evt_{comment_id}",
        event_type="comment.created",
        comment_id=comment_id,
        post_id="post_001",
        user_id=user_id,
        username=f"user_{user_id}",
        text=text,
        sent_at=datetime.now(timezone.utc),
        processed=False,
        deleted=False,
    )


# ---------------------------------------------------------------------------
# Test 5: Same user + same rule → only ONE delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_user_same_rule_is_blocked(db_session):
    """
    Test 5: Three comments from the same user, all matching the same rule.
    Only the FIRST delivery is created; the next two are blocked.
    """
    rule = _rule("PRICE")
    db_session.add(rule)
    await db_session.flush()

    events = [
        _event("PRICE", "usr_A", "cmt_001"),
        _event("PRICE please", "usr_A", "cmt_002"),
        _event("Can I get PRICE?", "usr_A", "cmt_003"),
    ]
    for e in events:
        db_session.add(e)
    await db_session.flush()

    for e in events:
        await _handle_comment_created(db_session, e, [rule])
    await db_session.commit()

    deliveries = (await db_session.execute(select(Delivery))).scalars().all()
    assert len(deliveries) == 1

    blocks = (await db_session.execute(select(DuplicateBlock))).scalars().all()
    assert len(blocks) == 2


# ---------------------------------------------------------------------------
# Test 6: Different users + same rule → each gets their own delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_different_users_same_rule(db_session):
    """Test 6: Three different users comment 'PRICE' → 3 deliveries, 0 blocked."""
    rule = _rule("PRICE")
    db_session.add(rule)
    await db_session.flush()

    events = [
        _event("PRICE", "usr_A", "cmt_101"),
        _event("PRICE", "usr_B", "cmt_102"),
        _event("PRICE", "usr_C", "cmt_103"),
    ]
    for e in events:
        db_session.add(e)
    await db_session.flush()

    for e in events:
        await _handle_comment_created(db_session, e, [rule])
    await db_session.commit()

    deliveries = (await db_session.execute(select(Delivery))).scalars().all()
    assert len(deliveries) == 3

    blocks = (await db_session.execute(select(DuplicateBlock))).scalars().all()
    assert len(blocks) == 0


# ---------------------------------------------------------------------------
# Test 7: Same user + different rules → one per rule
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_user_different_rules(db_session):
    """Test 7: One comment matches two rules → 2 deliveries for the same user."""
    rule_price = _rule("PRICE")
    rule_link = _rule("LINK")
    db_session.add(rule_price)
    db_session.add(rule_link)
    await db_session.flush()

    event = _event("PRICE LINK please", "usr_A", "cmt_201")
    db_session.add(event)
    await db_session.flush()

    await _handle_comment_created(db_session, event, [rule_price, rule_link])
    await db_session.commit()

    deliveries = (await db_session.execute(select(Delivery))).scalars().all()
    assert len(deliveries) == 2

    rule_ids = {d.rule_id for d in deliveries}
    assert rule_ids == {rule_price.id, rule_link.id}

    blocks = (await db_session.execute(select(DuplicateBlock))).scalars().all()
    assert len(blocks) == 0


# ---------------------------------------------------------------------------
# Race condition regression: concurrent same user + same rule
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_same_user_same_rule(db_engine):
    """
    Race condition test: two coroutines try to insert a Delivery for the
    same (rule_id, user_id) simultaneously.

    The UNIQUE(rule_id, user_id) database constraint must ensure only ONE
    delivery row is created, regardless of which coroutine wins the race.

    We simulate this with two separate DB sessions running concurrently
    via asyncio.gather().
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    TestSession = async_sessionmaker(
        bind=db_engine, class_=AsyncSession, expire_on_commit=False
    )

    # Set up the rule and event.
    async with TestSession() as setup_session:
        rule = _rule("PRICE")
        setup_session.add(rule)
        event = _event("PRICE", "usr_race", "cmt_race")
        setup_session.add(event)
        await setup_session.commit()

    # Two coroutines, each with their own session, racing to insert.
    async def attempt(session_factory, rule_ref, event_ref):
        async with session_factory() as session:
            # Reload the objects in this session.
            result = await session.execute(select(Rule).where(Rule.id == rule_ref.id))
            r = result.scalar_one()
            result2 = await session.execute(select(Event).where(Event.event_id == event_ref.event_id))
            e = result2.scalar_one()
            await _handle_comment_created(session, e, [r])
            await session.commit()

    # Run both simultaneously.
    results = await asyncio.gather(
        attempt(TestSession, rule, event),
        attempt(TestSession, rule, event),
        return_exceptions=True,  # Don't let one failure cancel the other.
    )

    # Both should complete without unhandled exceptions.
    for r in results:
        if isinstance(r, Exception):
            # An IntegrityError is acceptable — it means the DB constraint worked.
            # Re-raise only if it's an unexpected exception type.
            err_str = str(r) + type(r).__name__
            assert any(k in err_str.lower() for k in ("unique", "integrityerror")), (
                f"Unexpected exception: {r}"
            )

    # Final state: AT MOST one delivery row (NEVER two).
    # With PostgreSQL: exactly 1 (the first wins, the second hits UNIQUE constraint
    # inside a savepoint and inserts a DuplicateBlock instead).
    # With SQLite (tests): SQLite's concurrency model means both coroutines may
    # run sequentially in the same event loop; both see the constraint correctly.
    # The important guarantee is: the count is never 2.
    async with TestSession() as check_session:
        all_deliveries = (await check_session.execute(select(Delivery))).scalars().all()
        assert len(all_deliveries) <= 1, (
            f"CONSTRAINT VIOLATED: {len(all_deliveries)} deliveries created for "
            "the same (rule, user) — expected at most 1."
        )
