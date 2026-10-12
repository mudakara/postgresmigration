from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import catalog, comparison, connections, inventory, jobs, projects, scans, sequences, system
from app.config import get_settings
from app.security import get_cipher


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_cipher()  # fail fast on a missing or malformed CREDENTIALS_KEY
    yield


app = FastAPI(title="Migration Studio", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (system, connections, projects, scans, inventory, comparison, catalog, sequences, jobs):
    app.include_router(module.router, prefix="/api")
