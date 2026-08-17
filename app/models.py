"""
models.py — SQLAlchemy ORM models (database tables).

Each class maps to one table.  Column types, constraints, and relationships
are defined here.  SQLAlchemy uses these definitions both to generate SQL
CREATE TABLE statements and to let us query the DB with Python objects.

Important constraints explained:
- Event.event_id UNIQUE      → PseudoGram can resend the same event_id;
                               the DB rejects the second insert, giving us
                               free idempotency.
- Delivery UNIQUE(rule_id, user_id) → Guarantees a user never gets the
                               same rule's DM more than once, even if the
                               worker runs multiple times.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    """Generate a new UUID4 string.  Used as primary key default."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------

class Rule(Base):
    """
    A user-defined trigger rule.

    When a new comment's text contains `keyword` (case-insensitive),
    the system sends `dm_message` to the commenter.
    """

    __tablename__ = "rules"

    # UUID string primary key — universally unique, safe to expose in API.
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    # The trigger word or phrase (e.g. "PRICE").
    keyword: Mapped[str] = mapped_column(String(255), nullable=False)
    # The message body to DM when the keyword matches.
    dm_message: Mapped[str] = mapped_column(Text, nullable=False)
    # When this rule was created — stored in UTC.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )

    # Back-reference: all delivery attempts linked to this rule.
    deliveries: Mapped[list["Delivery"]] = relationship(
        "Delivery", back_populates="rule"
    )


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

class Event(Base):
    """
    A raw webhook event received from PseudoGram.

    We persist the event immediately (before any processing) so we can
    return HTTP 200 in under 5 seconds and process it asynchronously.

    The UNIQUE constraint on event_id prevents double-processing if
    PseudoGram sends the same event more than once.

    deleted: set to True when a comment.deleted event arrives for this
    comment_id.  The worker checks this before sending a DM.
    """

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    # PseudoGram's own event identifier — must be unique in our DB.
    event_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # comment_id can be None for event types that don't carry it (e.g. future types).
    comment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    post_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Full text of the comment — we'll do keyword matching against this.
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When PseudoGram says it sent this event.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When our server received it.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    # False = not yet processed by the worker; True = worker has handled it.
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    # True if a comment.deleted event arrived for this comment.
    # Any pending deliveries for this comment will be cancelled.
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------
# Delivery status enum
# ---------------------------------------------------------------------------

# Valid values for Delivery.status.
# "cancelled" is used when a comment.deleted event arrives before sending.
DELIVERY_STATUSES = ("queued", "sending", "delivered", "failed", "cancelled")


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

class Delivery(Base):
    """
    One attempt to send a DM for a specific (rule, user) pair.

    KEY CONSTRAINT: UNIQUE(rule_id, user_id)
    This is the database-level guarantee that one user never gets the
    same rule's DM twice.  Even if the worker has a bug and tries to
    insert a duplicate, PostgreSQL will reject it with an IntegrityError.

    next_reconcile_at: After POSTing to /v1/dm/send, we save the dm_id and
    set next_reconcile_at to "check in 2 seconds".  The reconciliation loop
    (separate from the send loop) polls GET /v1/dm/{dm_id}.  This means
    the sender is never blocked waiting for status; it can move on and send
    the next DM immediately.
    """

    __tablename__ = "deliveries"

    # Composite unique constraint defined at the table level.
    __table_args__ = (
        UniqueConstraint("rule_id", "user_id", name="uq_delivery_rule_user"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    # Which rule triggered this delivery.
    rule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("rules.id"), nullable=False
    )
    # Who to send the DM to.
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # The comment that triggered the match.
    comment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # The dm_id returned by PseudoGram after a successful POST /v1/dm/send.
    # Nullable because we don't have it until after the send call.
    dm_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Current state in the delivery lifecycle.
    status: Mapped[str] = mapped_column(
        # native_enum=False: use a VARCHAR column instead of a DB-native ENUM type.
        # This makes the column work identically on both PostgreSQL (production)
        # and SQLite (in-memory test database).
        Enum(*DELIVERY_STATUSES, name="delivery_status", native_enum=False),
        default="queued",
        nullable=False,
    )
    # How many times we've tried to send this DM.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # When to next attempt a send or retry.
    # None = ready to send immediately.
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When to next poll GET /v1/dm/{dm_id} for status.
    # Set after a successful POST /v1/dm/send returns a dm_id.
    # Null = not yet sent, or status already final.
    next_reconcile_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    # ORM relationship so we can do delivery.rule.keyword etc.
    rule: Mapped["Rule"] = relationship("Rule", back_populates="deliveries")


# ---------------------------------------------------------------------------
# DuplicateBlock
# ---------------------------------------------------------------------------

class DuplicateBlock(Base):
    """
    Each row represents one duplicate that was blocked.

    When the worker tries to insert a Delivery row but gets an
    IntegrityError (because UNIQUE(rule_id, user_id) is violated),
    it inserts one row here instead.  GET /stats counts these rows for
    the 'duplicates_blocked' field.

    This is simpler than an in-memory counter that resets on restart.
    """

    __tablename__ = "duplicate_blocks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    # Keep track of what was blocked for debugging.
    rule_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )


# ---------------------------------------------------------------------------
# RateLimitToken
# ---------------------------------------------------------------------------

class RateLimitToken(Base):
    """
    Database-backed tokens for distributed rate limiting.

    We enforce 10 requests per 60 seconds.
    On app startup, exactly 10 rows (id=1..10) are created.
    When a worker needs to send a DM, it looks for the oldest token where
    used_at < now - 60s, locks it (FOR UPDATE SKIP LOCKED), and updates used_at.

    This ensures multiple workers running simultaneously cannot exceed the
    10 req/60s limit globally.
    """

    __tablename__ = "rate_limit_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The exact UTC time this token was last used.
    # We initialize it to the distant past (e.g., 2000-01-01) so they are immediately available.
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
