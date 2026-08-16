"""
worker.py — Background worker that processes webhook events and sends DMs.

This is the heart of the async pipeline.  run_worker() is launched as an
asyncio Task when the app starts and runs forever in the background.

High-level loop (one iteration every WORKER_POLL_INTERVAL seconds):

  Phase 1 — Recover stuck deliveries:
    Any delivery stuck in "sending" from a previous crashed process is reset
    to "queued" so it gets retried.  This prevents jobs from being lost on restart.

  Phase 2 — Process new events:
    Fetch unprocessed Event rows, match them against rules, create Delivery rows.
    Handles comment.deleted: cancels any pending deliveries for that comment.

  Phase 3 — Send queued deliveries:
    For each delivery with status=queued and next_retry_at <= now:
    a. Acquire a rate-limiter slot (blocks if we're at the 10 req/60s limit).
    b. Call PseudoGram POST /v1/dm/send.
    c. On 202: save dm_id, set status="sending", set next_reconcile_at.
    d. On 500: schedule exponential backoff retry.
    e. On 429: schedule retry using Retry-After header value.
    f. On 400: mark failed immediately, no retry.

  Phase 4 — Reconcile in-flight deliveries:
    For each delivery with status="sending" and next_reconcile_at <= now:
    Call GET /v1/dm/{dm_id}.
    If delivered: mark delivered.
    If failed: mark failed.
    If still queued: push next_reconcile_at forward and check again later.

Why four phases instead of one big loop?
  Phase 1 ensures no delivery is permanently lost after a crash.
  Phase 3 and Phase 4 are decoupled: sending a DM and checking its status
  are separate loops.  This means the worker never blocks waiting 30+ seconds
  for status — it sends the next DM immediately after recording the dm_id,
  then comes back to check status in the next iteration.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Delivery, DuplicateBlock, Event, Rule
from app.pseudogram import (
    PseudoGramBadRequestError,
    PseudoGramError,
    PseudoGramRateLimitError,
    PseudoGramServerError,
    get_dm_status,
    send_dm,
)
from app.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)

# Maximum send attempts before permanently marking a delivery as failed.
MAX_ATTEMPTS = 5

# Exponential backoff delays (seconds) for HTTP 500 retries.
# Attempt 1 → wait 1s, attempt 2 → 2s, ..., attempt 5 → 16s.
BACKOFF_DELAYS = [1, 2, 4, 8, 16]


# ---------------------------------------------------------------------------
# Phase 1 — Recover deliveries stuck in "sending" after a crash
# ---------------------------------------------------------------------------

async def _recover_stuck_deliveries(session) -> None:
    """
    Reset any delivery stuck in "sending" back to "queued".

    Why this is needed:
      When the worker POSTs to /v1/dm/send, it immediately sets status="sending".
      If the process crashes AFTER the POST but BEFORE saving the dm_id and
      setting next_reconcile_at, the delivery stays in "sending" forever.
      On the next startup, this phase resets those deliveries so they get
      retried.

    Tradeoff:
      There is a small risk of sending the same DM twice if:
        1. POST /v1/dm/send succeeds on PseudoGram's side.
        2. Process crashes before we save the dm_id.
        3. On restart, we retry the send.
      This is a known limitation (see FAILURES.md).  The UNIQUE(rule_id, user_id)
      constraint prevents multiple *delivery rows*, but not multiple *send calls*
      if the row is reset.  In practice this window is very small.
    """
    result = await session.execute(
        select(Delivery).where(Delivery.status == "sending")
    )
    stuck = result.scalars().all()
    for delivery in stuck:
        if delivery.dm_id:
            # We have a dm_id — we already sent it.  Go straight to reconciliation.
            logger.warning(
                "Delivery %s was stuck in 'sending' with dm_id=%s — scheduling reconciliation.",
                delivery.id,
                delivery.dm_id,
            )
            delivery.next_reconcile_at = datetime.now(timezone.utc)
        else:
            # No dm_id — we crashed before the send completed.  Reset to retry.
            logger.warning(
                "Delivery %s was stuck in 'sending' without dm_id — resetting to 'queued'.",
                delivery.id,
            )
            delivery.status = "queued"
            delivery.next_retry_at = None
        delivery.updated_at = datetime.now(timezone.utc)

    if stuck:
        await session.commit()
        logger.info("Recovered %d stuck deliveries.", len(stuck))


# ---------------------------------------------------------------------------
# Phase 2 — Match events to rules and create Delivery rows
# ---------------------------------------------------------------------------

async def _process_events(session) -> None:
    """
    Fetch unprocessed events and handle them based on event_type.
    """
    result = await session.execute(
        select(Event).where(Event.processed == False)  # noqa: E712
    )
    events = result.scalars().all()

    if not events:
        return

    # Load all rules once — more efficient than querying per event.
    rules_result = await session.execute(select(Rule))
    rules = rules_result.scalars().all()

    for event in events:
        if event.event_type == "comment.created":
            await _handle_comment_created(session, event, rules)
        elif event.event_type == "comment.deleted":
            await _handle_comment_deleted(session, event)
        else:
            logger.info("Unknown event_type '%s' — ignoring.", event.event_type)

        event.processed = True

    await session.commit()


async def _handle_comment_created(session, event: Event, rules: list[Rule]) -> None:
    """
    Match a comment.created event against all rules.
    Create a Delivery row for each matching rule.
    """
    if not event.text:
        return

    comment_text_lower = event.text.lower()

    for rule in rules:
        if rule.keyword.lower() not in comment_text_lower:
            continue

        logger.info(
            "Rule '%s' matched comment '%s' by user '%s'.",
            rule.keyword,
            event.comment_id,
            event.user_id,
        )

        # Create the delivery inside a SAVEPOINT so that an IntegrityError
        # (duplicate user+rule) only rolls back this one insert, not the
        # entire transaction.  This lets other rules continue to be processed.
        delivery = Delivery(
            rule_id=rule.id,
            user_id=event.user_id,
            comment_id=event.comment_id,
            status="queued",
            attempts=0,
        )
        try:
            async with session.begin_nested():
                session.add(delivery)
                await session.flush()
        except IntegrityError:
            # UNIQUE(rule_id, user_id) was violated — user already has this DM.
            logger.info(
                "Duplicate blocked: rule_id=%s user_id=%s", rule.id, event.user_id
            )
            block = DuplicateBlock(rule_id=rule.id, user_id=event.user_id)
            session.add(block)
            await session.flush()


async def _handle_comment_deleted(session, event: Event) -> None:
    """
    Handle a comment.deleted event.

    Steps:
      1. Mark the original comment's event as deleted (for audit purposes).
      2. Cancel any deliveries for that comment that haven't been sent yet.
      3. If a DM was already delivered: leave it — we cannot un-deliver.

    Only comment_id is guaranteed to be in the payload.
    """
    comment_id = event.comment_id
    if not comment_id:
        logger.warning("comment.deleted event has no comment_id — skipping.")
        return

    logger.info("Processing comment.deleted for comment_id=%s", comment_id)

    # Mark all original events for this comment as deleted.
    original_events = (await session.execute(
        select(Event).where(
            Event.comment_id == comment_id,
            Event.event_type == "comment.created",
        )
    )).scalars().all()

    for orig in original_events:
        orig.deleted = True

    # Cancel any queued deliveries for this comment.
    pending = (await session.execute(
        select(Delivery).where(
            Delivery.comment_id == comment_id,
            Delivery.status == "queued",
        )
    )).scalars().all()

    cancelled_count = 0
    for d in pending:
        d.status = "cancelled"
        d.updated_at = datetime.now(timezone.utc)
        cancelled_count += 1

    if cancelled_count:
        logger.info(
            "Cancelled %d pending delivery(ies) for deleted comment %s.",
            cancelled_count,
            comment_id,
        )

    # Deliveries in "sending" or "delivered" are NOT cancelled:
    # - "sending": We've already made the HTTP call.  We cannot recall it.
    #   We note this in FAILURES.md as a known limitation.
    # - "delivered": The DM was already received.  Cannot be undone.
    already_delivered = (await session.execute(
        select(Delivery).where(
            Delivery.comment_id == comment_id,
            Delivery.status == "delivered",
        )
    )).scalars().all()

    if already_delivered:
        logger.info(
            "DM for comment %s was already delivered — cannot be cancelled.",
            comment_id,
        )


# ---------------------------------------------------------------------------
# Phase 3 — Send queued deliveries
# ---------------------------------------------------------------------------

async def _send_pending_deliveries(session) -> None:
    """
    Fetch deliveries ready to send and attempt each one.

    'Ready' means: status=queued AND (next_retry_at IS NULL OR next_retry_at <= now).
    """
    now = datetime.now(timezone.utc)

    result = await session.execute(
        select(Delivery).where(
            Delivery.status == "queued",
            (Delivery.next_retry_at == None)  # noqa: E711
            | (Delivery.next_retry_at <= now),
        )
    )
    deliveries = result.scalars().all()

    for delivery in deliveries:
        await _send_one_delivery(session, delivery)


async def _send_one_delivery(session, delivery: Delivery) -> None:
    """
    Attempt to send one DM.

    Key design: we do NOT block here waiting for delivery confirmation.
    After a successful 202, we set next_reconcile_at and return immediately.
    Phase 4 will check the status in the next iteration.
    """
    # Load the rule to get the message content.
    rule_result = await session.execute(
        select(Rule).where(Rule.id == delivery.rule_id)
    )
    rule = rule_result.scalar_one_or_none()
    if rule is None:
        logger.error("Rule %s not found for delivery %s", delivery.rule_id, delivery.id)
        delivery.status = "failed"
        delivery.updated_at = datetime.now(timezone.utc)
        await session.commit()
        return

    # Increment attempt count and mark as "sending" before the HTTP call.
    # This prevents another worker from picking up the same delivery.
    delivery.attempts += 1
    delivery.status = "sending"
    delivery.updated_at = datetime.now(timezone.utc)
    await session.commit()

    # Acquire a rate-limiter slot.  Sleeps if we are at the 10 req/60s limit.
    await rate_limiter.acquire()

    try:
        dm_id = await send_dm(
            recipient_user_id=delivery.user_id,
            message=rule.dm_message,
            comment_id=delivery.comment_id,
        )
    except PseudoGramServerError:
        await _schedule_retry(session, delivery)
        return
    except PseudoGramRateLimitError as e:
        await _schedule_retry_after(session, delivery, wait_seconds=e.retry_after)
        return
    except PseudoGramBadRequestError:
        delivery.status = "failed"
        delivery.updated_at = datetime.now(timezone.utc)
        await session.commit()
        logger.error("Delivery %s permanently failed (400 Bad Request).", delivery.id)
        return
    except PseudoGramError as e:
        logger.error("Unexpected PseudoGram error for delivery %s: %s", delivery.id, e)
        await _schedule_retry(session, delivery)
        return

    # 202 received — save dm_id and schedule reconciliation check.
    # We leave status as "sending" until GET /v1/dm/{dm_id} confirms delivery.
    delivery.dm_id = dm_id
    delivery.next_reconcile_at = datetime.now(timezone.utc) + timedelta(
        seconds=settings.DM_RECONCILE_INTERVAL
    )
    delivery.updated_at = datetime.now(timezone.utc)
    await session.commit()
    logger.info(
        "DM accepted by PseudoGram: dm_id=%s for delivery=%s (next check in %.0fs)",
        dm_id,
        delivery.id,
        settings.DM_RECONCILE_INTERVAL,
    )


# ---------------------------------------------------------------------------
# Phase 4 — Reconcile in-flight deliveries (non-blocking)
# ---------------------------------------------------------------------------

async def _reconcile_deliveries(session) -> None:
    """
    Check the status of all in-flight DMs (those with dm_id and a past next_reconcile_at).

    This runs on every worker iteration, separately from sending.
    GET /v1/dm/{dm_id} does NOT count against the 10 req/60s send rate limit.
    """
    now = datetime.now(timezone.utc)

    result = await session.execute(
        select(Delivery).where(
            Delivery.status == "sending",
            Delivery.dm_id != None,  # noqa: E711
            Delivery.next_reconcile_at != None,  # noqa: E711
            Delivery.next_reconcile_at <= now,
        )
    )
    deliveries = result.scalars().all()

    for delivery in deliveries:
        await _check_one_delivery_status(session, delivery)


async def _check_one_delivery_status(session, delivery: Delivery) -> None:
    """
    Poll GET /v1/dm/{dm_id} for one delivery and update its status.
    """
    try:
        status = await get_dm_status(delivery.dm_id)
    except PseudoGramError as e:
        logger.warning(
            "Error checking status for dm_id=%s (delivery=%s): %s",
            delivery.dm_id,
            delivery.id,
            e,
        )
        # Push the check forward so we don't hammer the endpoint on errors.
        delivery.next_reconcile_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.DM_RECONCILE_INTERVAL * 2
        )
        delivery.updated_at = datetime.now(timezone.utc)
        await session.commit()
        return

    logger.info("Reconciliation: dm_id=%s status=%s", delivery.dm_id, status)

    if status == "delivered":
        delivery.status = "delivered"
        delivery.next_reconcile_at = None
        delivery.updated_at = datetime.now(timezone.utc)
        await session.commit()
        logger.info("Delivery %s confirmed delivered.", delivery.id)

    elif status == "failed":
        delivery.status = "failed"
        delivery.next_reconcile_at = None
        delivery.updated_at = datetime.now(timezone.utc)
        await session.commit()
        logger.warning("Delivery %s reported failed by PseudoGram.", delivery.id)

    else:
        # Still "queued" on PseudoGram's side.  Check again later.
        delivery.next_reconcile_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.DM_RECONCILE_INTERVAL
        )
        delivery.updated_at = datetime.now(timezone.utc)
        await session.commit()


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

async def _schedule_retry(session, delivery: Delivery) -> None:
    """
    Schedule a retry with exponential backoff for HTTP 500 errors.

    Backoff schedule:
      attempt 1 → wait  1s
      attempt 2 → wait  2s
      attempt 3 → wait  4s
      attempt 4 → wait  8s
      attempt 5 → wait 16s
      attempt 6+ → mark permanently failed
    """
    if delivery.attempts >= MAX_ATTEMPTS:
        delivery.status = "failed"
        delivery.updated_at = datetime.now(timezone.utc)
        await session.commit()
        logger.error(
            "Delivery %s permanently failed after %d attempts.",
            delivery.id,
            delivery.attempts,
        )
        return

    delay_index = min(delivery.attempts - 1, len(BACKOFF_DELAYS) - 1)
    wait_seconds = BACKOFF_DELAYS[delay_index]

    delivery.status = "queued"
    delivery.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
    delivery.next_reconcile_at = None
    delivery.updated_at = datetime.now(timezone.utc)
    await session.commit()
    logger.info(
        "Delivery %s will retry in %ds (attempt %d/%d).",
        delivery.id,
        wait_seconds,
        delivery.attempts,
        MAX_ATTEMPTS,
    )


async def _schedule_retry_after(
    session, delivery: Delivery, wait_seconds: int
) -> None:
    """
    Schedule a retry after the Retry-After delay from a 429 response.

    We decrement attempts because a 429 is not our fault — it shouldn't
    count against the MAX_ATTEMPTS limit.
    """
    delivery.attempts = max(0, delivery.attempts - 1)
    delivery.status = "queued"
    delivery.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
    delivery.next_reconcile_at = None
    delivery.updated_at = datetime.now(timezone.utc)
    await session.commit()
    logger.info(
        "Delivery %s rate-limited; will retry after %ds.", delivery.id, wait_seconds
    )


# ---------------------------------------------------------------------------
# Main worker loop
# ---------------------------------------------------------------------------

async def run_worker() -> None:
    """
    Infinite background loop with four phases per iteration.

    A fresh DB session is opened for each full iteration so that:
    - Stale ORM state from the previous iteration never leaks.
    - An exception in one iteration doesn't corrupt the next.
    """
    logger.info("Background worker started.")

    # On startup, immediately recover any deliveries stuck from a prior crash.
    try:
        async with AsyncSessionLocal() as session:
            await _recover_stuck_deliveries(session)
    except Exception as e:
        logger.exception("Startup recovery failed: %s", e)

    while True:
        try:
            async with AsyncSessionLocal() as session:
                await _process_events(session)
                await _send_pending_deliveries(session)
                await _reconcile_deliveries(session)
        except Exception as e:
            logger.exception("Worker iteration failed: %s", e)

        await asyncio.sleep(settings.WORKER_POLL_INTERVAL)
