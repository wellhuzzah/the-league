import asyncpg
from dotenv import load_dotenv
import os

load_dotenv()

# The API's own read-only role. No fallback to DATABASE_URL (the scripts' writer role).
DATABASE_URL = os.getenv("API_DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("API_DATABASE_URL is not set in .env; refusing to start")

pool = None

async def get_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL, ssl=False)
    return pool
