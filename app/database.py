"""
database.py — SQLAlchemy async engine and session factory.

Key concepts:
- We use the *async* variant of SQLAlchemy so that database I/O never
  blocks the event loop (and therefore never delays HTTP responses).
- create_async_engine  → the connection pool.
- async_sessionmaker   → a factory that creates AsyncSession objects.
- get_db()             → a FastAPI dependency that opens a session per
                         request and always closes it when done.
- init_db()            → called once at startup to create all tables.
"""

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
# echo=False in production so SQL statements aren't printed to stdout.
# pool_pre_ping=True makes SQLAlchemy test each connection before using it,
# which prevents "connection closed" errors after the DB restarts.
# Safe diagnostic: verify the scheme of the DATABASE_URL before engine creation.
_scheme = settings.DATABASE_URL.split("://")[0] if "://" in settings.DATABASE_URL else "unknown"
print(f"DATABASE_SCHEME={_scheme}")

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
# expire_on_commit=False means ORM objects remain usable after commit.
# Without this, accessing obj.id after a commit would trigger a lazy load,
# which doesn't work in async mode.
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ---------------------------------------------------------------------------
# Base class for all ORM models
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------
async def get_db() -> AsyncSession:
    """
    Yield an AsyncSession for one HTTP request.

    Usage in a route:
        async def my_route(db: AsyncSession = Depends(get_db)):
            ...

    The 'async with' block guarantees the session is closed even if an
    exception is raised inside the route handler.
    """
    async with AsyncSessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# Table creation
# ---------------------------------------------------------------------------
async def init_db() -> None:
    """
    Create all tables that are declared in models.py.

    Called once when the app starts (see main.py lifespan).
    In production you'd use Alembic migrations instead, but for an
    assignment this is simpler and perfectly safe.
    """
    # Import models so SQLAlchemy knows about them before we call create_all.
    import app.models  # noqa: F401
    from sqlalchemy import select
    from datetime import datetime, timezone

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Initialize exactly 10 rate limit tokens for the distributed rate limiter.
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(app.models.RateLimitToken).limit(1))
        if not result.scalars().first():
            past = datetime(2000, 1, 1, tzinfo=timezone.utc)
            for i in range(1, 11):
                session.add(app.models.RateLimitToken(id=i, used_at=past))
            await session.commit()
