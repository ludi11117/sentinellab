from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from . import models  # noqa: F401  (register ORM models before create_all)
from .db import Base, engine
from .routers import deliveries, evaluations, evidence, ingestion, jobs, metrics, projects, reviews, traces
from .security import Principal, principal


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="SentinelLab — Agent Delivery Verification", version="0.5.0", lifespan=lifespan)

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
app.include_router(reviews.router)
app.include_router(evidence.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@app.get("/api/public/context")
def context(identity: Principal = Depends(principal)):
    return {"project_id": identity.project_id, "role": identity.role}


def legacy_redirect(view):
    def redirect():
        return RedirectResponse("/#" + view, status_code=307)
    return redirect


for filename, view in {"delivery": "setup", "releases": "compare", "evidence": "compare", "review": "review", "learn": "guide"}.items():
    app.add_api_route("/" + filename + ".html", legacy_redirect(view), include_in_schema=False)

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
