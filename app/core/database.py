import logging

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.core.config import DATABASE_URL, DEV_MODE


logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# DEV_MODE uses smaller pools to stay within Supabase free-tier session
# pooler connection limits (MaxClientsInSessionMode). With proper shutdown
# (close_asyncpg_pool on lifespan exit), connections are cleaned up on restart.
_SA_POOL_SIZE = 3 if DEV_MODE else 10
_SA_MAX_OVERFLOW = 5 if DEV_MODE else 20
_ASYNCPG_MIN_SIZE = 2 if DEV_MODE else 5
_ASYNCPG_MAX_SIZE = 8 if DEV_MODE else 20

engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=_SA_POOL_SIZE,
    max_overflow=_SA_MAX_OVERFLOW,
    echo=False,
    connect_args={
        "statement_cache_size": 0,
    },
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create all tables and enum types if they don't already exist."""
    logger.info("init_db: starting database initialization")
    try:
        # Import models so they register with Base.metadata
        logger.debug("init_db: importing ORM models")
        import app.models.db  # noqa: F401

        logger.debug("init_db: opening engine connection and creating metadata")
        async with engine.begin() as conn:
            # Enable pgvector before create_all so VECTOR columns resolve
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)

        logger.info("init_db: database initialization completed successfully")
    except Exception as exc:
        logger.exception("init_db: database initialization failed", exc_info=exc)
        raise


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields a database session per request."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Raw asyncpg pool for high-performance batch upserts in workers
# ---------------------------------------------------------------------------

_asyncpg_pool: asyncpg.Pool | None = None


def _raw_dsn() -> str:
    """Convert SQLAlchemy DATABASE_URL to a plain PostgreSQL DSN for asyncpg."""
    dsn = DATABASE_URL
    if dsn.startswith("postgresql+asyncpg://"):
        dsn = "postgresql://" + dsn[len("postgresql+asyncpg://"):]
    return dsn


async def init_asyncpg_pool() -> asyncpg.Pool:
    """Create the raw asyncpg pool. Called once during app startup."""
    global _asyncpg_pool
    if _asyncpg_pool is None:
        _asyncpg_pool = await asyncpg.create_pool(
            _raw_dsn(),
            min_size=_ASYNCPG_MIN_SIZE,
            max_size=_ASYNCPG_MAX_SIZE,
        )
    return _asyncpg_pool


async def get_asyncpg_pool() -> asyncpg.Pool:
    """Return the existing asyncpg pool, initialising lazily if needed."""
    global _asyncpg_pool
    if _asyncpg_pool is None:
        return await init_asyncpg_pool()
    return _asyncpg_pool


async def close_asyncpg_pool() -> None:
    """Shut down the asyncpg pool. Called during app shutdown."""
    global _asyncpg_pool
    if _asyncpg_pool is not None:
        await _asyncpg_pool.close()
        _asyncpg_pool = None
