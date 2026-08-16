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

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
