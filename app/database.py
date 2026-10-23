import asyncio

import asyncpg
from dotenv import load_dotenv
import os

load_dotenv()

# The API's own read-only role. No fallback to DATABASE_URL (the scripts' writer role).
DATABASE_URL = os.getenv("API_DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("API_DATABASE_URL is not set in .env; refusing to start")

pool = None
_pool_lock = asyncio.Lock()


async def open_pool():
    """Called once by the app's lifespan handler at startup."""
    return await get_pool()


async def close_pool():
    """Called by the lifespan handler at shutdown, so connections close cleanly."""
    global pool
    if pool is not None:
        await pool.close()
        pool = None


async def get_pool():
    # The API opens the pool at startup (main.py lifespan). Scripts that import this module
    # (scripts/seeding_check.py) get it lazily; the lock stops concurrent first callers each
    # creating a pool and overrunning rockwood_api's CONNECTION LIMIT 20.
    global pool
    if pool is None:
        async with _pool_lock:
            if pool is None:
                pool = await asyncpg.create_pool(DATABASE_URL, ssl=False, min_size=2, max_size=10)
    return pool


async def concurrently(*jobs):
    """Run independent queries at once, each on its own pooled connection, instead of one after
    another on a single connection. A job is an async function that takes the connection, e.g.
    `lambda db: db.fetch(SQL, year)`. Results come back in the order the jobs were given."""
    p = await get_pool()

    async def run(job):
        async with p.acquire() as db:
            return await job(db)
    return await asyncio.gather(*(run(job) for job in jobs))
