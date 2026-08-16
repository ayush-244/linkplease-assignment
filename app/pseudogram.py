"""
pseudogram.py — Async HTTP client for the PseudoGram API.

All outgoing HTTP calls go through this module.  Using a single place
for API calls makes it easy to:
  - Mock in tests (replace httpx calls without touching business logic).
  - Add retries or timeouts in one place.
  - Log every request/response for debugging.

We use httpx.AsyncClient so that HTTP I/O is non-blocking and doesn't
stall the asyncio event loop while we wait for a response.

Security note: The API key is NEVER logged.  Only status codes and
non-sensitive fields are included in log messages.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# How long (in seconds) to wait for PseudoGram to respond before giving up.
REQUEST_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Custom exception types
# ---------------------------------------------------------------------------
# Using specific exception classes lets the worker decide what to do
# without parsing HTTP status codes in multiple places.

class PseudoGramError(Exception):
    """Base class for all PseudoGram API errors."""


class PseudoGramServerError(PseudoGramError):
    """HTTP 500 — transient server error; should be retried."""


class PseudoGramRateLimitError(PseudoGramError):
    """
    HTTP 429 — we are sending too fast.
    The retry_after attribute tells us how many seconds to wait.
    """
    def __init__(self, retry_after: int = 60):
        super().__init__(f"Rate limited. Retry after {retry_after}s.")
        self.retry_after = retry_after


class PseudoGramBadRequestError(PseudoGramError):
    """HTTP 400 — our request is invalid; do NOT retry."""
    def __init__(self, detail: str = ""):
        super().__init__(f"Bad request: {detail}")


# ---------------------------------------------------------------------------
# API functions
# ---------------------------------------------------------------------------

async def send_dm(
    recipient_user_id: str,
    message: str,
    comment_id: str,
) -> str:
    """
    POST /v1/dm/send — Ask PseudoGram to queue a DM.

    Returns:
        dm_id (str): The ID assigned by PseudoGram.
                     We use this later to check delivery status.

    Raises:
        PseudoGramServerError     on HTTP 500
        PseudoGramRateLimitError  on HTTP 429
        PseudoGramBadRequestError on HTTP 400

    IMPORTANT: HTTP 202 means "accepted for delivery", NOT "delivered".
    We must poll GET /v1/dm/{dm_id} to find out if it was actually delivered.
    """
    url = f"{settings.PSEUDOGRAM_BASE_URL}/v1/dm/send"
    payload = {
        "recipient_user_id": recipient_user_id,
        "message": message,
        "comment_id": comment_id,
    }
    headers = {
        # PseudoGram authenticates us via this header.
        # NOTE: The key value is intentionally NOT logged anywhere.
        "X-Api-Key": settings.PSEUDOGRAM_API_KEY,
        "Content-Type": "application/json",
    }

    logger.info("Sending DM to user_id=%s for comment_id=%s", recipient_user_id, comment_id)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(url, json=payload, headers=headers)

    # Log only the status code — not the full response body, which could
    # contain sensitive information or the API key in error messages.
    logger.info("PseudoGram /send responded with status %s", response.status_code)

    if response.status_code in (200, 202):
        # Success — PseudoGram accepted the DM.
        data = response.json()
        return data["dm_id"]

    if response.status_code == 500:
        raise PseudoGramServerError("PseudoGram returned 500.")

    if response.status_code == 429:
        # Read how long to wait.  Default to 60s if the header is missing.
        retry_after = int(response.headers.get("Retry-After", 60))
        raise PseudoGramRateLimitError(retry_after=retry_after)

    if response.status_code == 400:
        # Log detail for debugging but never log the API key.
        try:
            detail = response.json().get("detail", "")
        except Exception:
            detail = ""
        raise PseudoGramBadRequestError(detail=detail)

    # Any other status code is unexpected.
    raise PseudoGramError(
        f"Unexpected status code from PseudoGram: {response.status_code}"
    )


async def get_dm_status(dm_id: str) -> str:
    """
    GET /v1/dm/{dm_id} — Check the current delivery status of a DM.

    This call does NOT count against the send rate limit (per assignment spec).

    Returns:
        status (str): One of "queued", "delivered", or "failed".

    Raises:
        PseudoGramError on unexpected responses.
    """
    url = f"{settings.PSEUDOGRAM_BASE_URL}/v1/dm/{dm_id}"
    headers = {"X-Api-Key": settings.PSEUDOGRAM_API_KEY}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(url, headers=headers)

    if response.status_code == 200:
        data = response.json()
        return data["status"]  # "queued" | "delivered" | "failed"

    raise PseudoGramError(
        f"Unexpected status code when checking DM status: {response.status_code}"
    )
