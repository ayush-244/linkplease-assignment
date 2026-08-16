"""
schemas.py — Pydantic models for request/response validation.

Pydantic validates incoming JSON automatically when FastAPI sees a
route parameter typed as one of these classes.  If validation fails,
FastAPI returns a 422 error with a clear message — no manual checks needed.

Naming convention:
  <Resource>Create  → used for incoming POST body
  <Resource>Response → used for outgoing JSON response
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

class RuleCreate(BaseModel):
    """Body expected by POST /rules."""

    keyword: str = Field(
        ...,
        min_length=1,
        description="The trigger keyword (case-insensitive match anywhere in comment text).",
        examples=["PRICE"],
    )
    dm_message: str = Field(
        ...,
        min_length=1,
        description="The DM to send when the keyword is found.",
        examples=["Here's the price list: ..."],
    )


class RuleResponse(BaseModel):
    """Body returned by POST /rules (HTTP 201)."""

    rule_id: str = Field(description="UUID of the created rule.")
    keyword: str
    dm_message: str

    # Tell Pydantic to read values from ORM object attributes,
    # not just dict keys.  Required when returning SQLAlchemy models.
    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

class WebhookFromUser(BaseModel):
    """Nested 'from' object inside WebhookData (comment.created only)."""

    user_id: str
    username: str


class WebhookData(BaseModel):
    """The 'data' field inside a comment.created webhook payload."""

    comment_id: str
    post_id: str
    text: str
    created_at: datetime
    # 'from' is a reserved word in Python, so we alias it.
    # In the JSON the key is literally "from".
    from_: WebhookFromUser = Field(alias="from")

    model_config = {"populate_by_name": True}


class WebhookDeletedData(BaseModel):
    """The 'data' field inside a comment.deleted webhook payload."""

    comment_id: str


class WebhookPayload(BaseModel):
    """
    Full webhook body sent by PseudoGram.

    'data' is typed as dict so we can handle both comment.created and
    comment.deleted without requiring all fields to be present.
    We parse the specific fields we need in the route handler after
    inspecting event_type.
    """

    event_id: str
    event_type: str
    sent_at: datetime
    data: dict  # parsed flexibly per event_type


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class StatsResponse(BaseModel):
    """Body returned by GET /stats."""

    sent: int = Field(description="Deliveries confirmed as 'delivered' by PseudoGram.")
    failed: int = Field(description="Deliveries that permanently failed.")
    queued: int = Field(description="Deliveries waiting to be sent or retried.")
    duplicates_blocked: int = Field(
        description="Times a duplicate (same rule + same user) was prevented."
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    """Body returned by GET /health."""

    status: str = "ok"
