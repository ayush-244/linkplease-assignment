"""
test_matching.py — Tests for keyword matching logic (Tests 2 and 3).

Tests the _handle_comment_created() worker function directly.
"""

import pytest
from datetime import datetime, timezone
from sqlalchemy import select

from app.models import Delivery, Event, Rule
from app.worker import _handle_comment_created


def _rule(keyword: str) -> Rule:
    return Rule(keyword=keyword, dm_message=f"DM for {keyword}")


def _event(text: str, user_id: str = "usr_001", comment_id: str = "cmt_001") -> Event:
    return Event(
        event_id=f"evt_{comment_id}_{user_id}",
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
# Test 2: Case-insensitive matching
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_matches_uppercase(db_session):
    """Rule 'PRICE' matches comment text 'PRICE'."""
    rule = _rule("PRICE")
    db_session.add(rule)
    await db_session.flush()

    event = _event("PRICE", comment_id="cmt_a")
    db_session.add(event)
    await db_session.flush()

    await _handle_comment_created(db_session, event, [rule])
    await db_session.commit()

    deliveries = (await db_session.execute(select(Delivery))).scalars().all()
    assert len(deliveries) == 1


@pytest.mark.asyncio
async def test_matches_lowercase(db_session):
    """Rule 'PRICE' matches lowercase comment 'price'."""
    rule = _rule("PRICE")
    db_session.add(rule)
    await db_session.flush()

    event = _event("price", comment_id="cmt_b")
    db_session.add(event)
    await db_session.flush()

    await _handle_comment_created(db_session, event, [rule])
    await db_session.commit()

    deliveries = (await db_session.execute(select(Delivery))).scalars().all()
    assert len(deliveries) == 1


@pytest.mark.asyncio
async def test_matches_mixed_case(db_session):
    """Rule 'PRICE' matches mixed case 'Price'."""
    rule = _rule("PRICE")
    db_session.add(rule)
    await db_session.flush()

    event = _event("Price", comment_id="cmt_c")
    db_session.add(event)
    await db_session.flush()

    await _handle_comment_created(db_session, event, [rule])
    await db_session.commit()

    deliveries = (await db_session.execute(select(Delivery))).scalars().all()
    assert len(deliveries) == 1


# ---------------------------------------------------------------------------
# Test 3: Keyword anywhere in text
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_matches_at_start(db_session):
    rule = _rule("PRICE")
    db_session.add(rule)
    await db_session.flush()
    event = _event("PRICE please 🙏", comment_id="cmt_start")
    db_session.add(event)
    await db_session.flush()
    await _handle_comment_created(db_session, event, [rule])
    await db_session.commit()
    assert len((await db_session.execute(select(Delivery))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_matches_in_middle(db_session):
    rule = _rule("PRICE")
    db_session.add(rule)
    await db_session.flush()
    event = _event("Can I get the PRICE?", comment_id="cmt_mid")
    db_session.add(event)
    await db_session.flush()
    await _handle_comment_created(db_session, event, [rule])
    await db_session.commit()
    assert len((await db_session.execute(select(Delivery))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_matches_at_end(db_session):
    rule = _rule("PRICE")
    db_session.add(rule)
    await db_session.flush()
    event = _event("What is the PRICE", comment_id="cmt_end")
    db_session.add(event)
    await db_session.flush()
    await _handle_comment_created(db_session, event, [rule])
    await db_session.commit()
    assert len((await db_session.execute(select(Delivery))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_no_match_when_keyword_absent(db_session):
    """Keyword not present → no delivery created."""
    rule = _rule("PRICE")
    db_session.add(rule)
    await db_session.flush()
    event = _event("Hello, how are you?", comment_id="cmt_none")
    db_session.add(event)
    await db_session.flush()
    await _handle_comment_created(db_session, event, [rule])
    await db_session.commit()
    assert len((await db_session.execute(select(Delivery))).scalars().all()) == 0
