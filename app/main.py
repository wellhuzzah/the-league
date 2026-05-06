from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import seasons, records, draft

app = FastAPI()

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

@app.get("/")
async def root():
    return {"status": "ok", "message": "Rockwood API is running"}
