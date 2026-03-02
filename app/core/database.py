import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.core.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
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
    # Import models so they register with Base.metadata
    import app.models.db  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
            min_size=5,
            max_size=20,
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
