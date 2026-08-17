"""
rate_limiter.py — Database-backed sliding-window rate limiter.

PseudoGram allows 10 requests per rolling 60-second window.

How the sliding window works:
  We maintain exactly 10 token rows in the DB.
  When we want to send a DM, we try to acquire the oldest token that was
  last used more than 60 seconds ago.

  If we get it, we update `used_at` to now() and proceed.
  If we don't, it means all 10 tokens were used within the last 60 seconds,
  so we hit the limit. We find the oldest token's `used_at` and calculate
  how long until it expires, so we can schedule a retry.

Why DB-backed?
  An in-memory deque only works for a single worker process. If deployed
  with multiple workers (horizontal scaling), they must coordinate via the DB.
  We use `FOR UPDATE SKIP LOCKED` (where supported, e.g. PostgreSQL) or a
  simple `FOR UPDATE` (SQLite test DB fallback) to ensure safe concurrent access.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import DBAPIError

from app.models import RateLimitToken

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Database-backed sliding-window rate limiter.

    Args:
        max_calls: Maximum number of calls allowed in the window.
        period:    Length of the rolling window in seconds.
    """

    def __init__(self, max_calls: int = 10, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period

    async def try_acquire(self, session: AsyncSession) -> tuple[bool, float]:
        """
        Attempt to acquire a rate limit token.

        Returns:
            (acquired, wait_seconds)
            If acquired == True, you can proceed immediately (wait_seconds=0.0).
            If acquired == False, the limit is reached. wait_seconds tells you
            how long until the next token becomes available.
        """
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(seconds=self.period)

        # Try to find one token older than window_start and lock it.
        # SQLite doesn't fully support SKIP LOCKED in all versions used by SQLAlchemy,
        # but for production PostgreSQL, SKIP LOCKED is highly recommended for concurrency.
        # We will use with_for_update() and handle the token.

        # 1. First, check if there is an available token.
        stmt = select(RateLimitToken).where(RateLimitToken.used_at <= window_start).order_by(RateLimitToken.used_at.asc()).limit(1).with_for_update(skip_locked=True)

        try:
            result = await session.execute(stmt)
            token = result.scalar_one_or_none()
        except DBAPIError as e:
            # Fallback for SQLite which may throw syntax error on SKIP LOCKED depending on driver version.
            # In our tests, SQLite might fail here. We fallback to simple FOR UPDATE.
            logger.debug(f"SKIP LOCKED failed (likely SQLite), falling back to normal FOR UPDATE: {e}")
            await session.rollback()
            stmt = select(RateLimitToken).where(RateLimitToken.used_at <= window_start).order_by(RateLimitToken.used_at.asc()).limit(1).with_for_update()
            result = await session.execute(stmt)
            token = result.scalar_one_or_none()

        if token:
            # Acquired! Update used_at to now.
            token.used_at = now
            await session.commit()
            return True, 0.0

        # 2. Limit reached (all 10 tokens used in last 60s).
        # We must NOT wait inside this transaction. Find the oldest token to calculate wait time.
        # No lock needed just to read the oldest token.
        oldest_stmt = select(RateLimitToken).order_by(RateLimitToken.used_at.asc()).limit(1)
        oldest_res = await session.execute(oldest_stmt)
        oldest_token = oldest_res.scalar_one_or_none()

        if not oldest_token:
            # Defensive fallback if the table wasn't initialized.
            return False, self.period

        # Calculate how long until the oldest token falls outside the window.
        used_at = oldest_token.used_at
        if used_at.tzinfo is None:
            used_at = used_at.replace(tzinfo=timezone.utc)

        wait_seconds = self.period - (now - used_at).total_seconds()

        # Ensure we return at least a small positive wait time if there are clock skew issues.
        wait_seconds = max(0.1, wait_seconds)

        return False, wait_seconds


# Single global instance
rate_limiter = RateLimiter(max_calls=10, period=60.0)
