import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..evaluation import content_hash
from ..jobqueue import cancel
from ..models import AuditEvent, CaseRun, Dataset, Job
from ..privacy import redact
from ..retrieval import PIPELINE_VERSION, REVISIONS
from .evaluations import StrictModel, require

router = APIRouter(prefix="/api/public", tags=["jobs"])


class Document(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=1000)
    body: str = Field(min_length=1, max_length=20000)
    source: str | None = Field(None, max_length=2000)
    source_sha256: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")


class NewJob(StrictModel):
    request_key: str = Field(min_length=1, max_length=64)
    dataset_id: str
    revision: str
    corpus: list[Document] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_spec(self):
        if self.revision not in REVISIONS:
            raise ValueError("choose a registered revision: " + ", ".join(sorted(REVISIONS)))
        if len({d.id for d in self.corpus}) != len(self.corpus):
            raise ValueError("document ids must be unique")
        return self


def job_json(job):
    return {"id": job.id, "dataset_id": job.dataset_id, "revision": job.revision, "state": job.state,
            "attempts": job.attempts, "fence": job.fence, "completed": job.completed, "owner": job.owner,
            "lease_until_ms": job.lease_until_ms, "experiment_id": job.experiment_id, "error": job.error,
            "corpus_digest": job.spec["corpus_digest"], "pipeline_version": job.spec["pipeline_version"]}


@router.post("/jobs", status_code=202)
def enqueue(payload: NewJob, db: Session = Depends(get_db)):
    dataset = require(db, Dataset, payload.dataset_id)
    if any(not isinstance(i["input"], dict) or not isinstance(i["input"].get("question"), str) for i in dataset.items):
        raise HTTPException(422, "registered pipeline requires input.question string")
    corpus = redact([d.model_dump() for d in payload.corpus])
    model_profile = None
    if payload.revision.startswith("llm-"):
        from ..model_provider import profile, ready
        if not ready():
            raise HTTPException(503, "configured model is unavailable; run start-model.ps1 or configure the provider")
        model_profile = profile()
    digest = content_hash({"dataset": dataset.digest, "dataset_id": dataset.id, "revision": payload.revision, "corpus": corpus,
                           "pipeline_version": PIPELINE_VERSION, "model_profile": model_profile})
    existing = db.scalar(select(Job).where(Job.request_key == payload.request_key))
    if existing:
        if existing.request_digest != digest:
            raise HTTPException(409, "request_key reused with different job content")
        return job_json(existing)
    if db.scalar(select(func.count()).select_from(Job).where(Job.state.in_(["queued", "running"]))) >= 50:
        raise HTTPException(429, "project job backlog is full; retry later")
    job = Job(id=str(uuid.uuid4()), request_key=payload.request_key, request_digest=digest, dataset_id=dataset.id,
              revision=payload.revision, spec={"corpus": corpus, "corpus_digest": content_hash(corpus), "pipeline_version": PIPELINE_VERSION,
                                             "model_profile": model_profile})
    db.add(job)
    db.add(AuditEvent(job_id=job.id, action="job.enqueued", detail={"revision": payload.revision, "dataset_digest": dataset.digest}))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Concurrent same-key submit: fetch and verify the committed request.
        existing = db.scalar(select(Job).where(Job.request_key == payload.request_key))
        if existing and existing.request_digest == digest:
            return job_json(existing)
        raise HTTPException(409, "concurrent conflicting job submission")
    return job_json(job)


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db)):
    return [job_json(j) for j in db.scalars(select(Job).order_by(Job.created_at.desc()).limit(100))]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = require(db, Job, job_id)
    return {**job_json(job), "total": len(require(db, Dataset, job.dataset_id).items),
            "cases": [c.result for c in db.scalars(select(CaseRun).where(CaseRun.job_id == job.id))]}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    require(db, Job, job_id)
    return {"cancelled": cancel(db, job_id)}


@router.get("/audit")
def audit(job_id: str | None = None, db: Session = Depends(get_db)):
    query = select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(200)
    if job_id:
        query = query.where(AuditEvent.job_id == job_id)
    return [{"id": e.id, "job_id": e.job_id, "action": e.action, "detail": e.detail} for e in db.scalars(query)]


@router.get("/model-status")
def model_status(db: Session = Depends(get_db)):
    from ..model_provider import profile, ready
    return {"ready": ready(), "profile": profile()}
