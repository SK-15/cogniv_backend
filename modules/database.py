import asyncio
import asyncpg
from modules.config import settings


# asyncpg pools are bound to an event loop; cache per-loop for correctness
# (especially in FastAPI TestClient where the loop may differ between requests).
_pools: dict[int, asyncpg.Pool] = {}

# Temporary placeholder to avoid import-time crashes while migrating away
# from the Supabase client.
supabase = None


async def get_pool() -> asyncpg.Pool:
    """
    Create (lazily) and reuse an asyncpg pool for Neon Postgres.
    """
    loop = asyncio.get_running_loop()
    key = id(loop)
    pool = _pools.get(key)
    if pool is None:
        pool = await asyncpg.create_pool(
            dsn=settings.neon_database_url,
            min_size=1,
            max_size=10,
        )
        _pools[key] = pool
    return pool


async def fetch_all(query: str, *args) -> list[dict]:
    """
    Fetch multiple rows and return them as plain dictionaries.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [dict(r) for r in rows]


async def fetch_one(query: str, *args) -> dict | None:
    """
    Fetch a single row and return it as a plain dictionary (or None).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, *args)
    return dict(row) if row else None


async def execute(query: str, *args) -> str:
    """
    Execute a statement (INSERT/UPDATE/DELETE) and return the command status.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)
