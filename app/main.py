"""
main.py — FastAPI application entry point.

Defines:
  - The FastAPI app with a lifespan (startup + shutdown logic).
  - POST /rules   — create a keyword trigger rule.
  - POST /webhook — receive PseudoGram events (comment.created + comment.deleted).
  - GET  /stats   — read delivery statistics from the database.
  - GET  /health  — lightweight liveness check for Render / load balancers.

Lifespan pattern:
  FastAPI's @asynccontextmanager lifespan replaces the old @app.on_event
  approach.  Code before 'yield' runs at startup; code after 'yield' runs
  at shutdown.  This is where we create DB tables and start the worker.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db, init_db
from app.models import Delivery, DuplicateBlock, Event, Rule
from app.schemas import HealthResponse, RuleCreate, RuleResponse, StatsResponse, WebhookPayload
from app.security import verify_signature
from app.worker import run_worker

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup / shutdown logic.

    Startup:
      1. Create all DB tables (idempotent — skips tables that already exist).
      2. Launch the background worker as an asyncio Task.

    Shutdown:
      Cancel the worker task gracefully.
    """
    logger.info("Starting up: initialising database...")
    await init_db()
    logger.info("Database ready.")

    logger.info("Starting background worker...")
    worker_task = asyncio.create_task(run_worker())

    yield  # Application is running and accepting requests.

    logger.info("Shutting down background worker...")
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass  # Expected — task was cancelled cleanly.
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------

app = FastAPI(
    title="LinkPlease",
    description="Keyword-triggered DM automation via PseudoGram.",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness check",
    tags=["Infrastructure"],
)
async def health_check():
    """
    Returns {"status": "ok"} if the application is running.

    Used by Render, Docker health checks, and load balancers.
    Does NOT check database connectivity (use /stats for that).
    """
    return HealthResponse(status="ok")


# ---------------------------------------------------------------------------
# POST /rules
# ---------------------------------------------------------------------------

@app.post(
    "/rules",
    response_model=RuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a keyword trigger rule",
    tags=["Rules"],
)
async def create_rule(
    body: RuleCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new rule.

    When a future comment contains `keyword` (case-insensitive, anywhere in
    the text), the system will send `dm_message` to the commenter.
    """
    rule = Rule(keyword=body.keyword, dm_message=body.dm_message)
    db.add(rule)
    await db.commit()

    logger.info("Rule created: id=%s keyword='%s'", rule.id, rule.keyword)

    return RuleResponse(
        rule_id=rule.id,
        keyword=rule.keyword,
        dm_message=rule.dm_message,
    )


# ---------------------------------------------------------------------------
# POST /webhook
# ---------------------------------------------------------------------------

@app.post(
    "/webhook",
    status_code=status.HTTP_200_OK,
    summary="Receive PseudoGram webhook events",
    tags=["Webhook"],
)
async def receive_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    # Optional so we can return 401 (not 422) when it's missing.
    x_pseudogram_signature: Optional[str] = Header(
        default=None,
        alias="X-PseudoGram-Signature",
        description="HMAC-SHA256 signature: sha256=<hex>",
    ),
):
    """
    Accept a webhook event from PseudoGram.

    Supports:
      - event_type = "comment.created"  → match rules, queue DMs
      - event_type = "comment.deleted"  → cancel pending DMs for that comment

    Steps (must complete in under 5 seconds):
      1. Read RAW body bytes (before JSON parsing — signature covers raw bytes).
      2. Reject immediately if signature header is missing.
      3. Verify the HMAC-SHA256 signature.
      4. Parse the JSON.
      5. Persist the event to the DB.
      6. Return 200 immediately.

    The background worker processes the event asynchronously.
    """
    # Step 1: Read raw bytes.
    # CRITICAL: We read the body BEFORE any JSON parsing.
    # Pydantic/JSON parsers may reorder keys or change whitespace, which
    # would break the HMAC check (signature was computed over original bytes).
    raw_body = await request.body()

    # Step 2: Missing signature → 401, not 422.
    # FastAPI defaults to returning 422 (Unprocessable Entity) for missing
    # required headers.  But for security failures, 401 is more appropriate.
    if x_pseudogram_signature is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-PseudoGram-Signature header.",
        )

    # Step 3: Verify the HMAC signature.
    if not verify_signature(raw_body, x_pseudogram_signature, settings.PSEUDOGRAM_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature.",
        )

    # Step 4: Parse JSON.
    try:
        payload_dict = json.loads(raw_body)
        payload = WebhookPayload.model_validate(payload_dict)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook payload.",
        )

    # Step 5: Build the Event row based on event_type.
    data = payload.data

    if payload.event_type == "comment.created":
        # All fields are expected for comment.created.
        try:
            from_user = data["from"]
            event = Event(
                event_id=payload.event_id,
                event_type=payload.event_type,
                comment_id=data["comment_id"],
                post_id=data.get("post_id"),
                user_id=from_user["user_id"],
                username=from_user["username"],
                text=data["text"],
                sent_at=payload.sent_at,
                processed=False,
                deleted=False,
            )
        except KeyError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Missing field in comment.created payload: {e}",
            )

    elif payload.event_type == "comment.deleted":
        # For comment.deleted, only comment_id is guaranteed.
        comment_id = data.get("comment_id")
        if not comment_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="comment.deleted payload missing comment_id.",
            )
        event = Event(
            event_id=payload.event_id,
            event_type=payload.event_type,
            comment_id=comment_id,
            post_id=None,
            user_id=None,
            username=None,
            text=None,
            sent_at=payload.sent_at,
            processed=False,
            deleted=False,
        )

    else:
        # Unknown event_type: persist it anyway so we don't silently drop data,
        # but mark as processed so the worker ignores it.
        event = Event(
            event_id=payload.event_id,
            event_type=payload.event_type,
            comment_id=data.get("comment_id"),
            sent_at=payload.sent_at,
            processed=True,  # Skip processing — we don't know this type.
            deleted=False,
        )
        logger.info("Unknown event_type '%s' received — persisted but not processed.", payload.event_type)

    db.add(event)

    try:
        await db.commit()
        logger.info(
            "Event persisted: event_id=%s event_type=%s",
            payload.event_id,
            payload.event_type,
        )
    except IntegrityError:
        # Duplicate event_id — already seen this exact event.
        await db.rollback()
        logger.info("Duplicate event ignored: event_id=%s", payload.event_id)

    # Step 6: Return 200 immediately.
    return {"status": "received"}


# ---------------------------------------------------------------------------
# GET /stats
# ---------------------------------------------------------------------------

@app.get(
    "/stats",
    response_model=StatsResponse,
    summary="Get delivery statistics",
    tags=["Stats"],
)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """
    Return real-time delivery statistics calculated from the database.

    All counts come from DB queries — no in-memory counters — so they
    survive server restarts and are always accurate.

    Definitions:
      sent:               Deliveries confirmed as 'delivered' by GET /v1/dm/{dm_id}.
      failed:             Permanently failed (after retries or 400 error).
      queued:             Waiting to send, retry, or being reconciled.
      duplicates_blocked: Times UNIQUE(rule_id, user_id) blocked a repeat DM.
    """
    sent_result = await db.execute(
        select(func.count()).select_from(Delivery).where(Delivery.status == "delivered")
    )
    sent = sent_result.scalar() or 0

    failed_result = await db.execute(
        select(func.count()).select_from(Delivery).where(Delivery.status == "failed")
    )
    failed = failed_result.scalar() or 0

    # "queued" in stats includes both queued (waiting to send) and sending
    # (sent but awaiting reconciliation) — they are all "in-flight" from
    # the user's perspective.
    queued_result = await db.execute(
        select(func.count()).select_from(Delivery).where(
            Delivery.status.in_(["queued", "sending"])
        )
    )
    queued = queued_result.scalar() or 0

    duplicates_result = await db.execute(
        select(func.count()).select_from(DuplicateBlock)
    )
    duplicates_blocked = duplicates_result.scalar() or 0

    return StatsResponse(
        sent=sent,
        failed=failed,
        queued=queued,
        duplicates_blocked=duplicates_blocked,
    )
