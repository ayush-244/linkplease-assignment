"""
conftest.py — Shared pytest fixtures.

Fixtures defined here are automatically available to every test file
without needing to import them explicitly.

Key design decisions:
  - We use an in-memory SQLite database for tests so tests are fast,
    isolated, and don't need a running PostgreSQL server.
  - We override FastAPI's get_db() dependency so routes use the test DB.
  - The worker is NOT started during tests — we test worker functions
    directly by calling them in the test body.
  - pytest-asyncio mode=auto means no @pytest.mark.asyncio needed per test.
"""

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Inject test environment variables BEFORE importing the app.
#
# app/config.py creates `settings = Settings()` at module import time.
# Pydantic requires DATABASE_URL and PSEUDOGRAM_API_KEY to be present.
# We inject safe test values here so tests run without a real .env file.
# setdefault() means real env vars (e.g. from CI) are not overwritten.
# ---------------------------------------------------------------------------
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("PSEUDOGRAM_API_KEY", "test_secret_key")
os.environ.setdefault("PSEUDOGRAM_BASE_URL", "https://pseudogram-api.onrender.com")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app

# SQLite in-memory URL — fast, isolated, no PostgreSQL needed.
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="function")
async def db_engine():
    """
    Create a fresh in-memory SQLite engine for each test function.

    'scope=function' means every test gets its own empty database,
    guaranteeing complete isolation.
    """
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Initialize rate limit tokens for the test DB
    from app.models import RateLimitToken
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy import select
    async with AsyncSession(engine, expire_on_commit=False) as init_session:
        # Check if tokens exist
        res = await init_session.execute(select(RateLimitToken))
        if not res.scalars().first():
            # Create 10 tokens with a really old timestamp so they are immediately available
            epoch = datetime.now(timezone.utc) - timedelta(days=1)
            init_session.add_all([RateLimitToken(used_at=epoch) for _ in range(10)])
            await init_session.commit()

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(db_engine):
    """
    Yield an AsyncSession for the in-memory test database.

    Use this to insert rows directly or inspect the DB after an operation.
    """
    TestSessionLocal = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with TestSessionLocal() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def client(db_engine):
    """
    Yield an async HTTP test client pointed at the FastAPI app.

    The get_db() dependency is overridden so every route uses the test DB.
    ASGITransport lets httpx call the app in-process — no real HTTP server.
    """
    TestSessionLocal = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def override_get_db():
        async with TestSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

def make_webhook_payload(
    event_id: str = "evt_test_001",
    event_type: str = "comment.created",
    comment_text: str = "test comment",
    user_id: str = "usr_test",
    username: str = "testuser",
    comment_id: str = "cmt_test_001",
    post_id: str = "post_test_001",
) -> dict:
    """Return a dict matching the PseudoGram webhook schema."""
    if event_type == "comment.deleted":
        return {
            "event_id": event_id,
            "event_type": "comment.deleted",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "data": {
                "comment_id": comment_id,
            },
        }
    return {
        "event_id": event_id,
        "event_type": event_type,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "data": {
            "comment_id": comment_id,
            "post_id": post_id,
            "text": comment_text,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "from": {
                "user_id": user_id,
                "username": username,
            },
        },
    }


def sign_payload(payload: dict, secret: str = "test_secret_key") -> str:
    """
    Compute the HMAC-SHA256 signature for a webhook payload dict.
    Returns the header value in "sha256=<hex>" format.
    """
    raw = json.dumps(payload).encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def valid_headers(payload: dict) -> dict:
    """Return headers dict with correct HMAC signature."""
    return {"X-PseudoGram-Signature": sign_payload(payload)}


def invalid_headers() -> dict:
    """Return headers dict with wrong signature."""
    return {"X-PseudoGram-Signature": "sha256=0000000000000000deadbeef"}
