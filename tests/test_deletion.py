"""
test_deletion.py — Tests for comment.deleted event handling (Step 7).

Covers:
  - comment.deleted before DM is sent → delivery cancelled
  - comment.deleted after DM is delivered → delivery left as delivered
  - comment.deleted webhook is accepted with valid signature
"""

import json
import pytest
from datetime import datetime, timezone
from sqlalchemy import select
from unittest.mock import AsyncMock, patch

from app.models import Delivery, Event, Rule
from app.worker import _handle_comment_deleted, _handle_comment_created
from tests.conftest import make_webhook_payload, valid_headers


def _rule(keyword: str) -> Rule:
    return Rule(keyword=keyword, dm_message=f"DM for {keyword}")


def _created_event(text: str, user_id: str, comment_id: str) -> Event:
    return Event(
        event_id=f"evt_created_{comment_id}",
        event_type="comment.created",
        comment_id=comment_id,
        post_id="post_001",
        user_id=user_id,
        username="user_test",
        text=text,
        sent_at=datetime.now(timezone.utc),
        processed=True,
        deleted=False,
    )


def _deleted_event(comment_id: str) -> Event:
    return Event(
        event_id=f"evt_deleted_{comment_id}",
        event_type="comment.deleted",
        comment_id=comment_id,
        sent_at=datetime.now(timezone.utc),
        processed=False,
        deleted=False,
    )


# ---------------------------------------------------------------------------
# Test: deleted before sending → cancel pending delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_comment_deleted_before_sending_cancels_delivery(db_session):
    """
    comment.created → queued delivery
    comment.deleted → delivery cancelled (never sent)
    """
    rule = _rule("PRICE")
    db_session.add(rule)
    await db_session.flush()

    # Simulate a queued delivery for comment cmt_del_001.
    delivery = Delivery(
        rule_id=rule.id,
        user_id="usr_del",
        comment_id="cmt_del_001",
        status="queued",
        attempts=0,
    )
    db_session.add(delivery)

    # Add the original event so _handle_comment_deleted can mark it deleted.
    original_event = _created_event("PRICE", "usr_del", "cmt_del_001")
    db_session.add(original_event)
    await db_session.flush()

    # Process a comment.deleted event.
    deleted_event = _deleted_event("cmt_del_001")
    db_session.add(deleted_event)
    await db_session.flush()

    await _handle_comment_deleted(db_session, deleted_event)
    await db_session.commit()

    await db_session.refresh(delivery)
    assert delivery.status == "cancelled"


# ---------------------------------------------------------------------------
# Test: deleted after delivery → leave as delivered
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_comment_deleted_after_delivery_leaves_delivered(db_session):
    """
    comment.created → delivered DM
    comment.deleted → delivery untouched (already delivered, cannot be recalled)
    """
    rule = _rule("PRICE")
    db_session.add(rule)
    await db_session.flush()

    # Simulate an already-delivered delivery.
    delivery = Delivery(
        rule_id=rule.id,
        user_id="usr_already_del",
        comment_id="cmt_del_002",
        status="delivered",  # Already done.
        attempts=1,
        dm_id="dm_already_delivered",
    )
    db_session.add(delivery)

    original_event = _created_event("PRICE", "usr_already_del", "cmt_del_002")
    db_session.add(original_event)
    await db_session.flush()

    deleted_event = _deleted_event("cmt_del_002")
    db_session.add(deleted_event)
    await db_session.flush()

    await _handle_comment_deleted(db_session, deleted_event)
    await db_session.commit()

    await db_session.refresh(delivery)
    # A delivered DM cannot be recalled — status must remain "delivered".
    assert delivery.status == "delivered"


# ---------------------------------------------------------------------------
# Test: comment.deleted via HTTP webhook
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_comment_deleted_webhook_accepted(client):
    """
    POST /webhook with event_type=comment.deleted and valid signature → HTTP 200.
    """
    payload = make_webhook_payload(
        event_id="evt_del_webhook_001",
        event_type="comment.deleted",
        comment_id="cmt_del_via_http",
    )
    response = await client.post(
        "/webhook",
        content=json.dumps(payload).encode(),
        headers=valid_headers(payload),
    )
    assert response.status_code == 200
