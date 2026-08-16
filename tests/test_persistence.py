"""
test_persistence.py — Tests that retry/reconciliation state survives process restart.

These tests verify that all critical state is in the database, not memory.
We simulate a "restart" by: inserting rows directly into the DB (bypassing
the normal worker flow), then running the worker functions and confirming
they pick up the state correctly.
"""

import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from unittest.mock import AsyncMock, patch

from app.models import Delivery, Rule
from app.worker import (
    _recover_stuck_deliveries,
    _send_pending_deliveries,
    _reconcile_deliveries,
)


def _rule():
    return Rule(keyword="PRICE", dm_message="Test message")


# ---------------------------------------------------------------------------
# Test: queued job survives restart
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queued_job_survives_restart(db_session):
    """
    A delivery inserted directly into the DB (simulating pre-restart state)
    must be picked up by _send_pending_deliveries on the next worker iteration.
    """
    rule = _rule()
    db_session.add(rule)
    await db_session.flush()

    # Insert a queued delivery — as if inserted before a crash.
    delivery = Delivery(
        rule_id=rule.id, user_id="usr_persist", comment_id="cmt_persist",
        status="queued", attempts=0,
    )
    db_session.add(delivery)
    await db_session.commit()

    # Run the send phase — it should pick up this delivery.
    with patch("app.worker.send_dm", new_callable=AsyncMock) as mock_send, \
         patch("app.worker.rate_limiter.acquire", new_callable=AsyncMock):
        mock_send.return_value = "dm_persist_001"
        await _send_pending_deliveries(db_session)

    await db_session.refresh(delivery)
    # Delivery was picked up and sent — now in "sending" awaiting reconciliation.
    assert delivery.status == "sending"
    assert delivery.dm_id == "dm_persist_001"


# ---------------------------------------------------------------------------
# Test: retry job survives restart
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_survives_restart(db_session):
    """
    A delivery with next_retry_at in the past (scheduled for retry) must
    be picked up by _send_pending_deliveries even after a simulated restart.
    """
    rule = _rule()
    db_session.add(rule)
    await db_session.flush()

    past_time = datetime.now(timezone.utc) - timedelta(seconds=10)
    delivery = Delivery(
        rule_id=rule.id, user_id="usr_retry_restart", comment_id="cmt_retry_restart",
        status="queued", attempts=2, next_retry_at=past_time,
    )
    db_session.add(delivery)
    await db_session.commit()

    with patch("app.worker.send_dm", new_callable=AsyncMock) as mock_send, \
         patch("app.worker.rate_limiter.acquire", new_callable=AsyncMock):
        mock_send.return_value = "dm_retry_restart_001"
        await _send_pending_deliveries(db_session)

    await db_session.refresh(delivery)
    assert delivery.status == "sending"


# ---------------------------------------------------------------------------
# Test: reconciliation survives restart
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconciliation_survives_restart(db_session):
    """
    A delivery in "sending" state with dm_id and next_reconcile_at in the past
    must be reconciled by _reconcile_deliveries on the next worker iteration.
    """
    rule = _rule()
    db_session.add(rule)
    await db_session.flush()

    past_time = datetime.now(timezone.utc) - timedelta(seconds=5)
    delivery = Delivery(
        rule_id=rule.id, user_id="usr_reconcile", comment_id="cmt_reconcile",
        status="sending", attempts=1, dm_id="dm_reconcile_001",
        next_reconcile_at=past_time,
    )
    db_session.add(delivery)
    await db_session.commit()

    with patch("app.worker.get_dm_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = "delivered"
        await _reconcile_deliveries(db_session)

    await db_session.refresh(delivery)
    assert delivery.status == "delivered"


# ---------------------------------------------------------------------------
# Test: stuck "sending" recovery on startup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stuck_sending_without_dm_id_resets_to_queued(db_session):
    """
    A delivery stuck in "sending" without a dm_id (crashed mid-send)
    must be reset to "queued" by _recover_stuck_deliveries.
    """
    rule = _rule()
    db_session.add(rule)
    await db_session.flush()

    delivery = Delivery(
        rule_id=rule.id, user_id="usr_stuck", comment_id="cmt_stuck",
        status="sending", attempts=1, dm_id=None,
    )
    db_session.add(delivery)
    await db_session.commit()

    await _recover_stuck_deliveries(db_session)
    await db_session.refresh(delivery)
    assert delivery.status == "queued"


@pytest.mark.asyncio
async def test_stuck_sending_with_dm_id_schedules_reconciliation(db_session):
    """
    A delivery stuck in "sending" WITH a dm_id (crashed after send but before
    recording reconciliation timestamp) must have next_reconcile_at set.
    """
    rule = _rule()
    db_session.add(rule)
    await db_session.flush()

    delivery = Delivery(
        rule_id=rule.id, user_id="usr_stuck2", comment_id="cmt_stuck2",
        status="sending", attempts=1, dm_id="dm_stuck_existing",
        next_reconcile_at=None,  # Never got set due to crash.
    )
    db_session.add(delivery)
    await db_session.commit()

    await _recover_stuck_deliveries(db_session)
    await db_session.refresh(delivery)
    # Status remains "sending" but next_reconcile_at is now set.
    assert delivery.status == "sending"
    assert delivery.next_reconcile_at is not None
