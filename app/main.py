import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from app.database import close_pool, open_pool
from app.routers import seasons, records, draft, matchups, teams, boxscores, players
from app.routers import tower
from app.routers import transactions


@asynccontextmanager
async def lifespan(app):
    # One connection pool for the process: opened before the first request, closed on shutdown
    await open_pool()
    yield
    await close_pool()


# /docs, /redoc and /openapi.json only with API_DOCS=1 (local debugging); off in production
docs = os.getenv("API_DOCS") == "1"
app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if docs else None,
    redoc_url="/redoc" if docs else None,
    openapi_url="/openapi.json" if docs else None,
)

# No CORS middleware: the site calls /api on its own origin (Caddy in production, the Vite dev
# server's proxy in development), so no cross-origin request ever reaches the API.

app.include_router(seasons.router)
app.include_router(records.router)
app.include_router(draft.router)
app.include_router(matchups.router)
app.include_router(teams.router)
app.include_router(boxscores.router)
app.include_router(players.router)
app.include_router(tower.router)
app.include_router(transactions.router)

@app.get("/")
async def root():
    return {"status": "ok", "message": "Rockwood API is running"}
