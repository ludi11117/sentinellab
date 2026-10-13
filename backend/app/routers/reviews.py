import secrets
from collections import Counter
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..evaluation import content_hash
from ..models import Acceptance, AuditEvent, Experiment, GateReport, Job, ReviewRound, ReviewVote
from ..privacy import redact
from ..retrieval import BM25
from .evaluations import StrictModel, require

router = APIRouter(prefix="/api/public", tags=["human-review"])


class NewRound(StrictModel):
    baseline_id: str
    candidate_id: str


class Vote(StrictModel):
    item_id: str
    reviewer_name: str = Field(min_length=1, max_length=255)
    choice: Literal["left", "right", "tie", "neither"]
    rationale: str = Field(min_length=5, max_length=3000)


class Signoff(StrictModel):
    gate_id: str
    review_round_id: str
    notes: str = Field(min_length=5, max_length=3000)


def identity(db):
    return getattr(db.info.get("principal"), "key_id", "local")


def public_round(round):
    return {"id": round.id, "samples": len(round.ordering), "blind": True}


@router.post("/reviews", status_code=201)
def create_round(payload: NewRound, db: Session = Depends(get_db)):
    base = require(db, Experiment, payload.baseline_id)
    candidate = require(db, Experiment, payload.candidate_id)
    if base.id == candidate.id or base.dataset_id != candidate.dataset_id or base.evaluator_version != candidate.evaluator_version:
        raise HTTPException(422, "review requires distinct compatible experiments on the same dataset")
    record = ReviewRound(baseline_id=base.id, candidate_id=candidate.id,
                         ordering={r["item_id"]: bool(secrets.randbits(1)) for r in base.results})
    db.add(record)
    db.commit()
    return public_round(record)


@router.get("/reviews")
def list_rounds(db: Session = Depends(get_db)):
    return [public_round(r) for r in db.scalars(select(ReviewRound).order_by(ReviewRound.created_at.desc()).limit(100))]


@router.get("/reviews/{round_id}/tasks")
def tasks(round_id: str, db: Session = Depends(get_db)):
    round = require(db, ReviewRound, round_id)
    base = require(db, Experiment, round.baseline_id)
    candidate = require(db, Experiment, round.candidate_id)
    base_map, cand_map = ({r["item_id"]: r for r in e.results} for e in (base, candidate))
    own_votes = {v.item_id for v in db.scalars(select(ReviewVote).where(ReviewVote.round_id == round.id, ReviewVote.reviewer_key_id == identity(db)))}
    jobs = [db.scalar(select(Job).where(Job.experiment_id == e.id)) for e in (base, candidate)]

    def answer(result, job):
        output = result["output"] if isinstance(result["output"], dict) else {"answer": result["output"]}
        value = {k: output.get(k) for k in ("answer", "citations", "quote", "abstain")}
        value["sources"] = [d for d in (job.spec["corpus"] if job else []) if d["id"] in (output.get("citations") or [])]
        return value

    result = []
    for item_id, swap in round.ordering.items():
        pair = [answer(base_map[item_id], jobs[0]), answer(cand_map[item_id], jobs[1])]
        if swap:
            pair.reverse()
        result.append({"item_id": item_id, "input": base_map[item_id]["input"], "left": pair[0], "right": pair[1], "voted": item_id in own_votes})
    corpus = jobs[1].spec["corpus"] if jobs[1] else []
    for item in result:
        hits = BM25(corpus).search(item["input"].get("question", ""), 2) if corpus else []
        item["reference_documents"] = [{"id": hit["id"], "body": hit["body"]} for hit in hits]
    return {**public_round(round), "tasks": result}


@router.post("/reviews/{round_id}/votes", status_code=201)
def vote(round_id: str, payload: Vote, db: Session = Depends(get_db)):
    round = require(db, ReviewRound, round_id)
    if payload.item_id not in round.ordering:
        raise HTTPException(422, "item does not belong to this review round")
    record = ReviewVote(round_id=round.id, item_id=payload.item_id, reviewer_key_id=identity(db),
                        reviewer_name=payload.reviewer_name, choice=payload.choice, rationale=redact(payload.rationale))
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "this key already submitted an immutable vote for this item")
    return {"id": record.id, "recorded": True}


def summary(db, round):
    votes = list(db.scalars(select(ReviewVote).where(ReviewVote.round_id == round.id)))
    counts = Counter()
    for vote in votes:
        if vote.choice in ("tie", "neither"):
            counts[vote.choice] += 1
        else:
            candidate_side = "left" if round.ordering[vote.item_id] else "right"
            counts["candidate" if vote.choice == candidate_side else "baseline"] += 1
    return {"round_id": round.id, "samples": len(round.ordering), "reviewed_items": len({v.item_id for v in votes}),
            "reviewer_keys": len({v.reviewer_key_id for v in votes}), "votes": len(votes), "preferences": dict(counts),
            "complete": set(round.ordering) == {v.item_id for v in votes}}


@router.get("/reviews/{round_id}/summary")
def review_summary(round_id: str, db: Session = Depends(get_db)):
    return summary(db, require(db, ReviewRound, round_id))


@router.post("/acceptances", status_code=201)
def acceptance(payload: Signoff, db: Session = Depends(get_db)):
    gate = require(db, GateReport, payload.gate_id)
    round = require(db, ReviewRound, payload.review_round_id)
    if (gate.baseline_id, gate.candidate_id) != (round.baseline_id, round.candidate_id):
        raise HTTPException(422, "gate and review must reference the exact same experiments")
    report = summary(db, round)
    if gate.report["decision"] != "pass" or not report["complete"]:
        raise HTTPException(409, "automated gate must pass and all review items must be covered")
    counts = report["preferences"]
    if counts.get("neither", 0) or counts.get("baseline", 0) > counts.get("candidate", 0):
        raise HTTPException(409, "human review does not support candidate acceptance")
    candidate = require(db, Experiment, gate.candidate_id)
    record = {"verdict": "human-reviewed-technical-acceptance", "candidate_id": candidate.id, "gate_id": gate.id,
              "review": report, "notes": redact(payload.notes), "signed_key_id": identity(db),
              "scope": "A key-authenticated technical review. Not a real customer contract, supplier approval or business outcome proof."}
    digest = content_hash(record)
    accepted = Acceptance(gate_id=gate.id, review_round_id=round.id, record=record, digest=digest)
    db.add(accepted)
    db.add(AuditEvent(action="acceptance.signed", detail={"digest": digest, "record": record}))
    db.commit()
    return {"id": accepted.id, "digest": digest, "record": record}


@router.get("/acceptances")
def acceptances(db: Session = Depends(get_db)):
    return [{"id": a.id, "digest": a.digest, "record": a.record} for a in db.scalars(select(Acceptance).limit(100))]
