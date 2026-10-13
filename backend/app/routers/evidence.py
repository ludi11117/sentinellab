from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Acceptance, Dataset, Experiment, GateReport, Job, ReviewRound
from .reviews import summary

router = APIRouter(prefix="/api/public", tags=["evidence"])


@router.get("/evidence")
def evidence(db: Session = Depends(get_db)):
    runs = []
    for job in db.scalars(select(Job).where(Job.state == "succeeded").order_by(Job.created_at.desc()).limit(100)):
        if not job.spec.get("model_profile"):
            continue
        experiment = db.get(Experiment, job.experiment_id)
        dataset = db.get(Dataset, job.dataset_id)
        totals = sum(r["tokens"] for r in experiment.results)
        runs.append({"job_id": job.id, "experiment_id": experiment.id, "dataset_id": dataset.id, "name": dataset.name,
                     "revision": experiment.revision, "summary": experiment.summary, "tokens": totals,
                     "holdout": all(r.get("split") == "holdout" for r in experiment.results),
                     "groups": len({r["group"] for r in experiment.results}), "provider": job.spec["model_profile"],
                     "failures": [{"item_id": r["item_id"], "question": r["input"]["question"], "expected": r["expected"],
                                   "answer": r["output"].get("answer"), "checks": r["checks"], "trace_id": r["trace_id"]}
                                  for r in experiment.results if not r["score"]]})
    reviews = [summary(db, r) for r in db.scalars(select(ReviewRound).order_by(ReviewRound.created_at.desc()).limit(100))]
    model_experiments = {r["experiment_id"] for r in runs}
    gates = [{"id": g.id, "baseline_id": g.baseline_id, "candidate_id": g.candidate_id, **g.report}
             for g in db.scalars(select(GateReport).where(GateReport.candidate_id.in_(model_experiments)).order_by(GateReport.created_at.desc()).limit(20))]
    return {"runs": runs, "gates": gates, "reviews": reviews, "acceptances": [{"id": a.id, "record": a.record} for a in db.scalars(select(Acceptance).limit(100))],
            "scope": "Real model inference and curated documentation assertions; no customer ROI or production generalization claim."}
