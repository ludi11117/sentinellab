from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..evaluation import EVALUATOR_VERSION, compare, content_hash, score, summarize
from ..models import Dataset, Experiment, GateReport, Span, Trace
from ..privacy import redact
from ..serializers import iso

router = APIRouter(prefix="/api/public", tags=["evaluations"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DatasetItem(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    input: Any
    expected: str = Field(min_length=1, max_length=10000)
    match: Literal["exact", "contains"] = "exact"
    required_tools: list[str] = Field(default_factory=list, max_length=100)
    forbidden_tools: list[str] = Field(default_factory=list, max_length=100)
    source_trace_id: str | None = None
    group: str | None = Field(None, min_length=1, max_length=64)
    expected_citation: str | None = Field(None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def consistent(self):
        if not self.expected.strip():
            raise ValueError("expected must not be blank")
        if set(self.required_tools) & set(self.forbidden_tools):
            raise ValueError("tool cannot be both required and forbidden")
        return self


class DatasetRequest(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    items: list[DatasetItem] = Field(min_length=1, max_length=1000)


class Observation(StrictModel):
    item_id: str
    trace_id: str


class ExperimentRequest(StrictModel):
    dataset_id: str
    name: str = Field(min_length=1, max_length=255)
    revision: str = Field(min_length=1, max_length=255)
    observations: list[Observation] = Field(min_length=1, max_length=1000)


class GatePolicy(StrictModel):
    min_samples: int = Field(10, ge=2, le=1000)
    min_quality: float = Field(.9, ge=0, le=1)
    max_quality_drop: float = Field(.05, ge=0, le=1)
    max_mean_cost: float = Field(.02, ge=0)
    max_p95_latency_ms: float = Field(3000, gt=0)
    max_error_rate: float = Field(0, ge=0, le=1)
    min_groups: int = Field(2, ge=1, le=1000)
    max_group_quality_drop: float = Field(.2, ge=0, le=1)


class GateRequest(StrictModel):
    baseline_id: str
    candidate_id: str
    policy: GatePolicy = Field(default_factory=GatePolicy)


def require(db, model, identifier):
    record = db.get(model, identifier)
    if record is None:
        raise HTTPException(404, f"{model.__name__} not found")
    return record


def dataset_json(d):
    return {"id": d.id, "name": d.name, "digest": d.digest, "items": d.items, "created_at": iso(d.created_at)}


def experiment_json(e):
    return {"id": e.id, "name": e.name, "dataset_id": e.dataset_id, "revision": e.revision,
            "evaluator_version": e.evaluator_version, "summary": e.summary, "created_at": iso(e.created_at)}


@router.post("/datasets", status_code=201)
def create_dataset(payload: DatasetRequest, db: Session = Depends(get_db)):
    items = redact([item.model_dump() for item in payload.items])
    if len({i["id"] for i in items}) != len(items):
        raise HTTPException(422, "dataset item ids must be unique")
    for item in items:
        if item["source_trace_id"]:
            trace = require(db, Trace, item["source_trace_id"])
            if trace.input != item["input"]:
                raise HTTPException(422, "source trace input differs from dataset item")
    record = Dataset(name=payload.name, items=items, digest=content_hash(items))
    db.add(record)
    db.commit()
    return dataset_json(record)


@router.get("/datasets")
def list_datasets(db: Session = Depends(get_db)):
    return [dataset_json(d) for d in db.scalars(select(Dataset).order_by(Dataset.created_at.desc()).limit(100))]


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, db: Session = Depends(get_db)):
    return dataset_json(require(db, Dataset, dataset_id))


@router.post("/experiments", status_code=201)
def create_experiment(payload: ExperimentRequest, db: Session = Depends(get_db)):
    dataset = require(db, Dataset, payload.dataset_id)
    mapping = {o.item_id: o.trace_id for o in payload.observations}
    if len(mapping) != len(payload.observations) or set(mapping) != {i["id"] for i in dataset.items}:
        raise HTTPException(422, "each dataset item must have exactly one observation; no omissions or extras")
    if len(set(mapping.values())) != len(mapping):
        raise HTTPException(422, "trace cannot be reused for multiple cases")
    results = []
    for item in dataset.items:
        trace = require(db, Trace, mapping[item["id"]])
        if trace.status == "running" or trace.latency_ms is None:
            raise HTTPException(409, "trace must be finalized before evaluation")
        if trace.input != item["input"]:
            raise HTTPException(422, "trace input differs from dataset input")
        spans = db.scalars(select(Span).where(Span.trace_id == trace.id)).all()
        results.append(score(item, trace, spans))
    record = Experiment(dataset_id=dataset.id, name=payload.name, revision=payload.revision,
                        evaluator_version=EVALUATOR_VERSION, results=results, summary=summarize(results))
    db.add(record)
    db.commit()
    return {**experiment_json(record), "results": results}


@router.get("/experiments")
def list_experiments(db: Session = Depends(get_db)):
    return [experiment_json(e) for e in db.scalars(select(Experiment).order_by(Experiment.created_at.desc()).limit(100))]


@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str, db: Session = Depends(get_db)):
    record = require(db, Experiment, experiment_id)
    return {**experiment_json(record), "results": record.results}


@router.post("/gates", status_code=201)
def release_gate(payload: GateRequest, db: Session = Depends(get_db)):
    baseline = require(db, Experiment, payload.baseline_id)
    candidate = require(db, Experiment, payload.candidate_id)
    if baseline.id == candidate.id:
        raise HTTPException(422, "baseline and candidate must differ")
    if baseline.dataset_id != candidate.dataset_id or baseline.evaluator_version != candidate.evaluator_version:
        raise HTTPException(422, "experiments must use the same dataset snapshot and evaluator version")
    if {r["trace_id"] for r in baseline.results} & {r["trace_id"] for r in candidate.results}:
        raise HTTPException(422, "baseline and candidate must use independent executions")
    report = compare(baseline, candidate, payload.policy.model_dump())
    record = GateReport(baseline_id=baseline.id, candidate_id=candidate.id, policy=payload.policy.model_dump(), report=report)
    db.add(record)
    db.commit()
    return {"id": record.id, "baseline_id": baseline.id, "candidate_id": candidate.id, "policy": record.policy, **report}


@router.get("/gates")
def list_gates(db: Session = Depends(get_db)):
    return [{"id": g.id, "baseline_id": g.baseline_id, "candidate_id": g.candidate_id, "created_at": iso(g.created_at),
             "policy": g.policy, **g.report} for g in db.scalars(select(GateReport).order_by(GateReport.created_at.desc()).limit(100))]
