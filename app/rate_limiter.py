"""
rate_limiter.py — Sliding-window rate limiter.

PseudoGram allows 10 requests per rolling 60-second window.

How the sliding window works:
  We keep a list (deque) of timestamps of recent API calls.
  Before each call we:
    1. Remove timestamps older than 60 seconds from the front.
    2. If 10 timestamps remain, we must wait before making the next call.
       We sleep until the oldest timestamp is more than 60 seconds ago.
    3. Add the current timestamp and allow the call to proceed.

Why a deque?
  A deque (double-ended queue) lets us remove from the left and append to
  the right in O(1) time, which is more efficient than a plain list.

Why is this safe with a single worker?
  Because there is only ONE sender coroutine running at a time, we never
  have two calls trying to acquire a slot simultaneously.  If we had
  multiple senders we would need a lock.
"""

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Async sliding-window rate limiter.

    Args:
        max_calls: Maximum number of calls allowed in the window.
        period:    Length of the rolling window in seconds.
    """

    def __init__(self, max_calls: int = 10, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        # Store the timestamp (as float) of each recent call.
        self._calls: deque[float] = deque()

    async def acquire(self) -> None:
        """
        Wait until we are allowed to make one more API call, then return.

        Call this immediately before every outgoing PseudoGram request.
        """
        while True:
            now = datetime.now(timezone.utc).timestamp()

            # Remove timestamps that have fallen outside the 60-second window.
            while self._calls and now - self._calls[0] >= self.period:
                self._calls.popleft()

            if len(self._calls) < self.max_calls:
                # We have capacity — record this call and continue.
                self._calls.append(now)
                return

            # We are at the limit.  Calculate how long until the oldest
            # call rolls out of the window.
            oldest = self._calls[0]
            wait_seconds = self.period - (now - oldest)
            logger.info(
                "Rate limit reached (%d/%d calls). Waiting %.2fs.",
                len(self._calls),
                self.max_calls,
                wait_seconds,
            )
            # Sleep without blocking the event loop.
            await asyncio.sleep(max(0.0, wait_seconds))


# Single global instance shared by the worker.
# max_calls=10, period=60 matches PseudoGram's documented limit.
rate_limiter = RateLimiter(max_calls=10, period=60.0)
