"""
test_webhook.py — Tests for POST /webhook signature verification and
                  delivery lifecycle (Tests 4, 8–14).
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Delivery, Event, Rule
from app.pseudogram import (
    PseudoGramBadRequestError,
    PseudoGramRateLimitError,
    PseudoGramServerError,
)
from app.worker import _send_one_delivery
from tests.conftest import make_webhook_payload, sign_payload, valid_headers, invalid_headers


# ---------------------------------------------------------------------------
# Test 8: Signature verification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_signature_returns_200(client):
    """Valid HMAC signature → HTTP 200."""
    payload = make_webhook_payload(event_id="evt_sig_ok")
    response = await client.post(
        "/webhook",
        content=json.dumps(payload).encode(),
        headers=valid_headers(payload),
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_invalid_signature_returns_401(client):
    """Wrong HMAC signature → HTTP 401 (not 422, not 400)."""
    payload = make_webhook_payload(event_id="evt_sig_bad")
    response = await client.post(
        "/webhook",
        content=json.dumps(payload).encode(),
        headers=invalid_headers(),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_missing_signature_returns_401(client):
    """Missing X-PseudoGram-Signature header → HTTP 401."""
    payload = make_webhook_payload(event_id="evt_sig_missing")
    response = await client.post(
        "/webhook",
        content=json.dumps(payload).encode(),
        # No signature header at all.
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_tampered_body_returns_401(client):
    """
    Signing the original body then sending a modified body → 401.
    This verifies we check the signature over the ACTUAL received bytes.
    """
    original = make_webhook_payload(event_id="evt_tamper")
    sig = sign_payload(original)  # Compute signature over original.

    # Tamper the payload after signing.
    tampered = dict(original)
    tampered["event_id"] = "evt_TAMPERED"

    response = await client.post(
        "/webhook",
        content=json.dumps(tampered).encode(),
        headers={"X-PseudoGram-Signature": sig},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Test 4: Duplicate event_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_event_id_is_ignored(client, db_session):
    """
    Test 4: Same event_id sent twice → second call returns 200 but
    only one Event row is created in the database.
    """
    payload = make_webhook_payload(event_id="evt_dup_test")
    headers = valid_headers(payload)
    body = json.dumps(payload).encode()

    r1 = await client.post("/webhook", content=body, headers=headers)
    assert r1.status_code == 200

    r2 = await client.post("/webhook", content=body, headers=headers)
    assert r2.status_code == 200  # Must not return an error.

    result = await db_session.execute(
        select(Event).where(Event.event_id == "evt_dup_test")
    )
    events = result.scalars().all()
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Tests 9–11: Retry behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_500_schedules_retry(db_session):
    """Test 9: HTTP 500 → status back to 'queued', next_retry_at in future."""
    rule = Rule(keyword="PRICE", dm_message="Test message")
    db_session.add(rule)
    await db_session.flush()

    delivery = Delivery(
        rule_id=rule.id, user_id="usr_500", comment_id="cmt_500",
        status="queued", attempts=0,
    )
    db_session.add(delivery)
    await db_session.commit()

    with patch("app.worker.send_dm", new_callable=AsyncMock) as mock_send, \
         patch("app.worker.rate_limiter.acquire", new_callable=AsyncMock):
        mock_send.side_effect = PseudoGramServerError("500")
        await _send_one_delivery(db_session, delivery)

    await db_session.refresh(delivery)
    assert delivery.status == "queued"
    assert delivery.attempts == 1
    assert delivery.next_retry_at is not None

    # next_retry_at must be in the future (timezone-agnostic comparison).
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    retry_naive = delivery.next_retry_at.replace(tzinfo=None) if delivery.next_retry_at.tzinfo else delivery.next_retry_at
    assert retry_naive > now_naive


@pytest.mark.asyncio
async def test_500_fails_after_max_attempts(db_session):
    """Test 9b: After 5 attempts, delivery is permanently failed."""
    rule = Rule(keyword="PRICE", dm_message="Test message")
    db_session.add(rule)
    await db_session.flush()

    delivery = Delivery(
        rule_id=rule.id, user_id="usr_maxretry", comment_id="cmt_maxretry",
        status="queued", attempts=5,
    )
    db_session.add(delivery)
    await db_session.commit()

    with patch("app.worker.send_dm", new_callable=AsyncMock) as mock_send, \
         patch("app.worker.rate_limiter.acquire", new_callable=AsyncMock):
        mock_send.side_effect = PseudoGramServerError("500")
        await _send_one_delivery(db_session, delivery)

    await db_session.refresh(delivery)
    assert delivery.status == "failed"


@pytest.mark.asyncio
async def test_429_sets_retry_after(db_session):
    """Test 10: HTTP 429 → next_retry_at ≈ now + Retry-After, attempts not increased."""
    rule = Rule(keyword="PRICE", dm_message="Test message")
    db_session.add(rule)
    await db_session.flush()

    delivery = Delivery(
        rule_id=rule.id, user_id="usr_429", comment_id="cmt_429",
        status="queued", attempts=0,
    )
    db_session.add(delivery)
    await db_session.commit()

    with patch("app.worker.send_dm", new_callable=AsyncMock) as mock_send, \
         patch("app.worker.rate_limiter.acquire", new_callable=AsyncMock):
        mock_send.side_effect = PseudoGramRateLimitError(retry_after=30)
        await _send_one_delivery(db_session, delivery)

    await db_session.refresh(delivery)
    assert delivery.status == "queued"
    assert delivery.attempts == 0  # Not penalised for 429.

    expected_naive = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=30)
    retry_naive = delivery.next_retry_at.replace(tzinfo=None) if delivery.next_retry_at.tzinfo else delivery.next_retry_at
    delta = abs((retry_naive - expected_naive).total_seconds())
    assert delta < 5


@pytest.mark.asyncio
async def test_400_fails_immediately(db_session):
    """Test 11: HTTP 400 → permanently failed, no retry scheduled."""
    rule = Rule(keyword="PRICE", dm_message="Test message")
    db_session.add(rule)
    await db_session.flush()

    delivery = Delivery(
        rule_id=rule.id, user_id="usr_400", comment_id="cmt_400",
        status="queued", attempts=0,
    )
    db_session.add(delivery)
    await db_session.commit()

    with patch("app.worker.send_dm", new_callable=AsyncMock) as mock_send, \
         patch("app.worker.rate_limiter.acquire", new_callable=AsyncMock):
        mock_send.side_effect = PseudoGramBadRequestError("invalid recipient")
        await _send_one_delivery(db_session, delivery)

    await db_session.refresh(delivery)
    assert delivery.status == "failed"
    assert delivery.next_retry_at is None


# ---------------------------------------------------------------------------
# Tests 12–14: DM status lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_202_not_immediately_delivered(db_session):
    """
    Test 12: After POST /v1/dm/send returns 202, the delivery status
    must be 'sending' (not 'delivered') — confirmation comes from reconciliation.
    """
    rule = Rule(keyword="PRICE", dm_message="Test message")
    db_session.add(rule)
    await db_session.flush()

    delivery = Delivery(
        rule_id=rule.id, user_id="usr_202", comment_id="cmt_202",
        status="queued", attempts=0,
    )
    db_session.add(delivery)
    await db_session.commit()

    with patch("app.worker.send_dm", new_callable=AsyncMock) as mock_send, \
         patch("app.worker.rate_limiter.acquire", new_callable=AsyncMock):
        mock_send.return_value = "dm_202_test"
        await _send_one_delivery(db_session, delivery)

    await db_session.refresh(delivery)
    # After 202, status is "sending" — NOT "delivered".
    assert delivery.status == "sending"
    assert delivery.dm_id == "dm_202_test"
    assert delivery.next_reconcile_at is not None


@pytest.mark.asyncio
async def test_reconciliation_marks_delivered(db_session):
    """
    Test 13: GET /v1/dm/{dm_id} returns 'delivered' → delivery status = 'delivered'.
    """
    from app.worker import _check_one_delivery_status

    rule = Rule(keyword="PRICE", dm_message="Test message")
    db_session.add(rule)
    await db_session.flush()

    delivery = Delivery(
        rule_id=rule.id, user_id="usr_del", comment_id="cmt_del",
        status="sending", attempts=1, dm_id="dm_delivered_123",
        next_reconcile_at=datetime.now(timezone.utc),
    )
    db_session.add(delivery)
    await db_session.commit()

    with patch("app.worker.get_dm_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = "delivered"
        await _check_one_delivery_status(db_session, delivery)

    await db_session.refresh(delivery)
    assert delivery.status == "delivered"
    assert delivery.next_reconcile_at is None


@pytest.mark.asyncio
async def test_reconciliation_marks_failed(db_session):
    """
    Test 14: GET /v1/dm/{dm_id} returns 'failed' → delivery status = 'failed'.
    """
    from app.worker import _check_one_delivery_status

    rule = Rule(keyword="PRICE", dm_message="Test message")
    db_session.add(rule)
    await db_session.flush()

    delivery = Delivery(
        rule_id=rule.id, user_id="usr_fail", comment_id="cmt_fail",
        status="sending", attempts=1, dm_id="dm_failed_456",
        next_reconcile_at=datetime.now(timezone.utc),
    )
    db_session.add(delivery)
    await db_session.commit()

    with patch("app.worker.get_dm_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = "failed"
        await _check_one_delivery_status(db_session, delivery)

    await db_session.refresh(delivery)
    assert delivery.status == "failed"
    assert delivery.next_reconcile_at is None
