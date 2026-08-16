"""
test_rules.py — Tests for POST /rules.
"""
import pytest
from sqlalchemy import select

from app.models import Rule


@pytest.mark.asyncio
async def test_create_rule_returns_201(client):
    """POST /rules with valid data → HTTP 201 with rule_id, keyword, dm_message."""
    response = await client.post(
        "/rules",
        json={"keyword": "PRICE", "dm_message": "Here's the price list!"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["rule_id"]
    assert body["keyword"] == "PRICE"
    assert body["dm_message"] == "Here's the price list!"


@pytest.mark.asyncio
async def test_create_rule_persists_in_db(client, db_session):
    """After creating a rule, it must exist in the database."""
    response = await client.post(
        "/rules",
        json={"keyword": "LINK", "dm_message": "Here is the link!"},
    )
    assert response.status_code == 201
    rule_id = response.json()["rule_id"]

    result = await db_session.execute(select(Rule).where(Rule.id == rule_id))
    rule = result.scalar_one_or_none()
    assert rule is not None
    assert rule.keyword == "LINK"


@pytest.mark.asyncio
async def test_create_rule_rejects_empty_keyword(client):
    """Empty keyword → HTTP 422 validation error."""
    response = await client.post(
        "/rules",
        json={"keyword": "", "dm_message": "Some message"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_rule_rejects_empty_message(client):
    """Empty dm_message → HTTP 422 validation error."""
    response = await client.post(
        "/rules",
        json={"keyword": "PRICE", "dm_message": ""},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_health_check(client):
    """GET /health → HTTP 200 with {status: ok}."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
