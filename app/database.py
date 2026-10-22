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

async def get_pool():
    # The lock stops concurrent first requests each creating a pool: the extra pools leaked
    # their connections and overran rockwood_api's CONNECTION LIMIT 20 (500s after a restart).
    global pool
    if pool is None:
        async with _pool_lock:
            if pool is None:
                pool = await asyncpg.create_pool(DATABASE_URL, ssl=False, min_size=2, max_size=10)
    return pool
