"""
test_stats.py — Tests that GET /stats is accurate and DB-backed.

Covers:
  - sent count (only truly delivered DMs)
  - failed count
  - queued count
  - duplicates_blocked count
  - stats survive simulated restart (data is in DB, not memory)
"""

import pytest
from datetime import datetime, timezone
from sqlalchemy import select

from app.models import Delivery, DuplicateBlock, Rule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rule_obj(keyword="PRICE"):
    return Rule(keyword=keyword, dm_message="Test message")


def _delivery(rule_id, user_id, comment_id, status, dm_id=None):
    return Delivery(
        rule_id=rule_id,
        user_id=user_id,
        comment_id=comment_id,
        status=status,
        attempts=1,
        dm_id=dm_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_empty(client):
    """With no data, all stats are 0."""
    response = await client.get("/stats")
    assert response.status_code == 200
    body = response.json()
    assert body == {"sent": 0, "failed": 0, "queued": 0, "duplicates_blocked": 0}


@pytest.mark.asyncio
async def test_stats_sent_counts_only_delivered(client, db_session):
    """'sent' must count only deliveries with status='delivered'."""
    rule = _rule_obj()
    db_session.add(rule)
    await db_session.flush()

    db_session.add(_delivery(rule.id, "usr_1", "cmt_1", "delivered", "dm_1"))
    db_session.add(_delivery(rule.id, "usr_2", "cmt_2", "queued"))
    db_session.add(_delivery(rule.id, "usr_3", "cmt_3", "failed"))
    await db_session.commit()

    response = await client.get("/stats")
    body = response.json()
    assert body["sent"] == 1
    assert body["queued"] == 1
    assert body["failed"] == 1
    assert body["duplicates_blocked"] == 0


@pytest.mark.asyncio
async def test_stats_202_not_counted_as_sent(client, db_session):
    """
    A delivery with status='sending' (after 202 but before reconciliation)
    must NOT appear in 'sent'. It should appear in 'queued' (in-flight).
    """
    rule = _rule_obj()
    db_session.add(rule)
    await db_session.flush()

    db_session.add(_delivery(rule.id, "usr_infl", "cmt_infl", "sending", "dm_infl"))
    await db_session.commit()

    response = await client.get("/stats")
    body = response.json()
    assert body["sent"] == 0
    assert body["queued"] == 1  # "sending" counts as in-flight/queued.


@pytest.mark.asyncio
async def test_stats_duplicates_blocked(client, db_session):
    """duplicates_blocked counts rows in the duplicate_blocks table."""
    db_session.add(DuplicateBlock(rule_id="rule_x", user_id="usr_x"))
    db_session.add(DuplicateBlock(rule_id="rule_x", user_id="usr_y"))
    await db_session.commit()

    response = await client.get("/stats")
    body = response.json()
    assert body["duplicates_blocked"] == 2


@pytest.mark.asyncio
async def test_stats_survive_restart(db_session, db_engine):
    """
    Stats must come from the database, not in-memory counters.
    Simulated by: insert rows directly into DB → create new client → check stats.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from httpx import ASGITransport, AsyncClient
    from app.database import get_db
    from app.main import app

    rule = _rule_obj()
    db_session.add(rule)
    await db_session.flush()

    db_session.add(_delivery(rule.id, "usr_restart_1", "cmt_r1", "delivered", "dm_r1"))
    db_session.add(_delivery(rule.id, "usr_restart_2", "cmt_r2", "failed"))
    db_session.add(DuplicateBlock(rule_id=rule.id, user_id="usr_restart_3"))
    await db_session.commit()

    # Simulate a "fresh" client that has not seen the inserts in memory.
    TestSession = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)

    async def fresh_get_db():
        async with TestSession() as s:
            yield s

    app.dependency_overrides[get_db] = fresh_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as fresh_client:
        response = await fresh_client.get("/stats")

    app.dependency_overrides.clear()

    body = response.json()
    assert body["sent"] == 1
    assert body["failed"] == 1
    assert body["duplicates_blocked"] == 1
