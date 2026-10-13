from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import models  # noqa: F401  (register ORM models before create_all)
from .db import Base, engine
from .routers import deliveries, evaluations, ingestion, jobs, metrics, projects, traces


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="SentinelLab — Agent Delivery Verification", version="0.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingestion.router)
app.include_router(traces.router)
app.include_router(metrics.router)
app.include_router(evaluations.router)
app.include_router(projects.router)
app.include_router(jobs.router)
app.include_router(deliveries.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
