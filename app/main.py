import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import seasons, records, draft, matchups, teams, boxscores
from app.routers import tower
from app.routers import transactions

# /docs, /redoc and /openapi.json only with API_DOCS=1 (local debugging); off in production
docs = os.getenv("API_DOCS") == "1"
app = FastAPI(
    docs_url="/docs" if docs else None,
    redoc_url="/redoc" if docs else None,
    openapi_url="/openapi.json" if docs else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5174",
        "http://192.168.1.4:5174",
        "https://theleague.letsfriggen.party",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(seasons.router)
app.include_router(records.router)
app.include_router(draft.router)
app.include_router(matchups.router)
app.include_router(teams.router)
app.include_router(boxscores.router)
app.include_router(tower.router)
app.include_router(transactions.router)

@app.get("/")
async def root():
    return {"status": "ok", "message": "Rockwood API is running"}
