from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..evaluation import content_hash
from ..models import AuditEvent, Dataset, Experiment, GateReport, Job
from .evaluations import StrictModel, require

router = APIRouter(prefix="/api/public", tags=["deliveries"])


class DeliveryRequest(StrictModel):
    gate_id: str
    customer: str = Field(min_length=1, max_length=255)
    workflow: str = Field(min_length=1, max_length=1000)


@router.post("/deliveries", status_code=201)
def delivery(payload: DeliveryRequest, db: Session = Depends(get_db)):
    gate = require(db, GateReport, payload.gate_id)
    candidate = require(db, Experiment, gate.candidate_id)
    dataset = require(db, Dataset, candidate.dataset_id)
    job = db.scalar(select(Job).where(Job.experiment_id == candidate.id))
    record = {"customer": payload.customer, "workflow": payload.workflow, "gate_id": gate.id,
              "verdict": "ready-for-human-review" if gate.report["decision"] == "pass" else "blocked",
              "dataset_digest": dataset.digest, "revision": candidate.revision, "evaluator": candidate.evaluator_version,
              "corpus_digest": job.spec["corpus_digest"] if job else None,
              "pipeline_version": job.spec["pipeline_version"] if job else None,
              "samples": len(dataset.items), "groups": gate.report.get("paired_bootstrap", {}).get("groups"),
              "policy": gate.policy, "summary": candidate.summary, "checks": gate.report["checks"],
              "regressions": [p for p in gate.report["pairs"] if p["delta"] < 0],
              "scope": "Dataset-based technical acceptance only. No customer sign-off, real supplier verification, model generalization or autonomous purchasing authorization."}
    digest = content_hash(record)
    event = AuditEvent(action="delivery.prepared", detail={"digest": digest, "record": record})
    db.add(event)
    db.commit()
    return {"audit_id": event.id, "digest": digest, "record": record}
