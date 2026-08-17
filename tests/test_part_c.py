import asyncio
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import Response
from sqlalchemy import select

from app.config import settings
from app.models import Delivery, Event, Rule, RateLimitToken
from app.rate_limiter import rate_limiter
from app.worker import _process_events, _send_pending_deliveries, _reconcile_deliveries, _recover_stuck_deliveries, MAX_ATTEMPTS
from tests.conftest import make_webhook_payload, sign_payload, valid_headers


@pytest.mark.asyncio
async def test_accepted_dm_fails_then_retries_and_succeeds(db_session, mocker):
    """1. accepted DM later becomes failed and is retried. 2. retry eventually succeeds."""
    rule = Rule(keyword="TEST", dm_message="hello")
    db_session.add(rule)
    await db_session.flush()
    delivery = Delivery(rule_id=rule.id, user_id="u1", comment_id="c1", status="queued", attempts=0)
    db_session.add(delivery)
    await db_session.commit()

    # Mock send_dm to succeed (202 Accepted)
    mocker.patch("app.worker.send_dm", return_value="dm_123")
    await _send_pending_deliveries(db_session)
    
    await db_session.refresh(delivery)
    assert delivery.status == "sending"
    assert delivery.dm_id == "dm_123"
    
    # Fast forward time to trigger reconciliation
    delivery.next_reconcile_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    await db_session.commit()
    
    # Mock get_dm_status to return "failed"
    mocker.patch("app.worker.get_dm_status", return_value="failed")
    await _reconcile_deliveries(db_session)
    
    await db_session.refresh(delivery)
    # Should now be queued for retry
    assert delivery.status == "queued"
    assert delivery.attempts == 1
    assert delivery.next_retry_at is not None

    # Fast forward next_retry_at
    delivery.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    await db_session.commit()
    
    # Retry succeeds!
    mocker.patch("app.worker.send_dm", return_value="dm_456")
    await _send_pending_deliveries(db_session)
    
    await db_session.refresh(delivery)
    assert delivery.status == "sending"
    assert delivery.dm_id == "dm_456"


@pytest.mark.asyncio
async def test_retry_exhaustion_increments_failed(db_session, mocker):
    """3. retry exhaustion increments failed."""
    rule = Rule(keyword="TEST", dm_message="hello")
    db_session.add(rule)
    await db_session.flush()
    delivery = Delivery(rule_id=rule.id, user_id="u1", comment_id="c1", status="queued", attempts=settings.MAX_RETRIES)
    db_session.add(delivery)
    await db_session.commit()

    # Even if it gets a 500, it should fail permanently
    from app.pseudogram import PseudoGramServerError
    mocker.patch("app.worker.send_dm", side_effect=PseudoGramServerError())
    
    await _send_pending_deliveries(db_session)
    
    await db_session.refresh(delivery)
    assert delivery.status == "failed"


@pytest.mark.asyncio
async def test_comment_deleted_before_processing(db_session):
    """4. comment.deleted before processing prevents sending."""
    rule = Rule(keyword="TEST", dm_message="hello")
    db_session.add(rule)
    await db_session.flush()
    await db_session.commit()

    # 1. Insert deleted event first
    del_evt = Event(event_id="del1", event_type="comment.deleted", comment_id="c1", deleted=False, processed=False)
    db_session.add(del_evt)
    await db_session.commit()

    # 2. Insert created event
    cre_evt = Event(event_id="cre1", event_type="comment.created", comment_id="c1", text="TEST", user_id="u1", deleted=False, processed=False)
    db_session.add(cre_evt)
    await db_session.commit()

    # Process events
    await _process_events(db_session)

    # Verify no delivery was created
    result = await db_session.execute(select(Delivery))
    assert len(result.scalars().all()) == 0


@pytest.mark.asyncio
async def test_comment_deleted_after_sending_does_not_create_duplicate(db_session, mocker):
    """5. comment.deleted after sending does not create a duplicate."""
    # This is basically testing idempotency and that we don't try to recall
    rule = Rule(keyword="TEST", dm_message="hello")
    db_session.add(rule)
    await db_session.flush()
    delivery = Delivery(rule_id=rule.id, user_id="u1", comment_id="c1", status="sending", attempts=1)
    db_session.add(delivery)
    await db_session.commit()

    del_evt = Event(event_id="del1", event_type="comment.deleted", comment_id="c1", deleted=False, processed=False)
    db_session.add(del_evt)
    await db_session.commit()

    await _process_events(db_session)
    
    await db_session.refresh(delivery)
    # Status remains sending, not cancelled!
    assert delivery.status == "sending"


@pytest.mark.asyncio
async def test_duplicate_deleted_events_harmless(db_session):
    """6. duplicate deleted events are harmless."""
    del_evt1 = Event(event_id="del1", event_type="comment.deleted", comment_id="c1", deleted=False, processed=False)
    del_evt2 = Event(event_id="del2", event_type="comment.deleted", comment_id="c1", deleted=False, processed=False)
    db_session.add_all([del_evt1, del_evt2])
    await db_session.commit()

    await _process_events(db_session)
    # Should not crash


@pytest.mark.asyncio
async def test_rate_limiter_10_requests_allowed_11th_delayed(db_session, mocker):
    """7. 10 requests are allowed, 8. 11th request is delayed."""
    rule = Rule(keyword="TEST", dm_message="hello")
    db_session.add(rule)
    await db_session.flush()
    
    for i in range(11):
        db_session.add(Delivery(rule_id=rule.id, user_id=f"u{i}", comment_id=f"c{i}", status="queued", attempts=0))
    await db_session.commit()

    mocker.patch("app.worker.send_dm", return_value="dm_x")
    
    await _send_pending_deliveries(db_session)
    
    result = await db_session.execute(select(Delivery).where(Delivery.status == "sending"))
    sent_deliveries = result.scalars().all()
    
    # Exactly 10 should be sent
    assert len(sent_deliveries) == 10
    
    result_queued = await db_session.execute(select(Delivery).where(Delivery.status == "queued"))
    queued = result_queued.scalars().all()
    assert len(queued) == 1
    assert queued[0].next_retry_at is not None


@pytest.mark.asyncio
async def test_queued_deliveries_survive_worker_restart(db_session):
    """10. queued deliveries survive worker restart."""
    # A delivery stuck in "sending" without a dm_id means the worker crashed before saving dm_id
    rule = Rule(keyword="TEST", dm_message="hello")
    db_session.add(rule)
    await db_session.flush()
    delivery = Delivery(rule_id=rule.id, user_id="u1", comment_id="c1", status="sending", dm_id=None)
    db_session.add(delivery)
    await db_session.commit()

    await _recover_stuck_deliveries(db_session)
    
    await db_session.refresh(delivery)
    assert delivery.status == "queued"
    assert delivery.next_retry_at is None


@pytest.mark.asyncio
async def test_concurrent_delivery_claim(db_session, db_engine, mocker):
    """Test that two workers cannot claim the same delivery concurrently."""
    from sqlalchemy.ext.asyncio import AsyncSession

    rule = Rule(keyword="TEST", dm_message="hello")
    db_session.add(rule)
    await db_session.flush()
    delivery = Delivery(rule_id=rule.id, user_id="u_concurrent", comment_id="c_concurrent", status="queued", attempts=0)
    db_session.add(delivery)
    await db_session.commit()

    # We want to trace how many times send_dm was called
    mock_send = mocker.patch("app.worker.send_dm", return_value="dm_concurrent")

    # Run _send_pending_deliveries concurrently
    async def worker_task():
        async with AsyncSession(db_engine, expire_on_commit=False) as session:
            await _send_pending_deliveries(session)

    # Fire two tasks concurrently
    await asyncio.gather(worker_task(), worker_task())

    # Only one should have successfully claimed and called send_dm
    assert mock_send.call_count == 1
    
    await db_session.refresh(delivery)
    assert delivery.status == "sending"
    assert delivery.attempts == 1
