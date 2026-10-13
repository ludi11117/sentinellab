import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import EventReceipt, Span, Trace
from ..privacy import redact
from ..schemas import IngestionBatch
from ..timeutil import as_utc, parse_dt

router = APIRouter(prefix="/api/public", tags=["ingestion"])


def _trace_create(db: Session, body: dict) -> str:
    trace = db.get(Trace, body["id"])
    if trace is None:
        trace = Trace(id=body["id"])
        db.add(trace)

    if body.get("name") is not None:
        trace.name = body["name"]
    if body.get("user_id") is not None:
        trace.user_id = body["user_id"]
    if body.get("session_id") is not None:
        trace.session_id = body["session_id"]
    if body.get("input") is not None:
        trace.input = body["input"]
    if body.get("output") is not None:
        trace.output = body["output"]
    if body.get("tags") is not None:
        trace.tags = body["tags"]
    if body.get("metadata") is not None:
        trace.meta = body["metadata"]
    if body.get("status") is not None:
        trace.status = body["status"]
    if body.get("start_time") is not None:
        trace.start_time = parse_dt(body["start_time"])
    if body.get("end_time") is not None:
        trace.end_time = parse_dt(body["end_time"])
    return trace.id


def _span_create(db: Session, body: dict) -> str:
    if db.get(Trace, body["trace_id"]) is None:
        raise HTTPException(409, "trace-create must be delivered before span-create")
    span = db.get(Span, body["id"])
    if span is not None:
        if span.trace_id != body["trace_id"]:
            raise HTTPException(409, "span id belongs to another trace")
        # A retried create must never reset a finished span.
        return span.trace_id
    if span is None:
        span = Span(id=body["id"])
        db.add(span)

    span.trace_id = body["trace_id"]
    span.parent_span_id = body.get("parent_span_id")
    span.name = body.get("name", "span")
    span.type = body.get("type", "span")
    span.input = body.get("input")
    span.meta = body.get("metadata")
    span.model = body.get("model")
    span.model_params = body.get("model_params")
    span.start_time = parse_dt(body.get("start_time"))
    span.status = "running"
    return span.trace_id


def _span_update(db: Session, body: dict) -> str | None:
    span = db.get(Span, body["id"])
    if span is None:
        raise HTTPException(409, "span-create must be delivered before span-update")

    if body.get("output") is not None:
        span.output = body["output"]
    if body.get("metadata") is not None:
        span.meta = body["metadata"]
    if body.get("status") is not None:
        span.status = body["status"]
    if body.get("error") is not None:
        span.error = body["error"]
    if body.get("prompt_tokens") is not None:
        span.prompt_tokens = body["prompt_tokens"]
    if body.get("completion_tokens") is not None:
        span.completion_tokens = body["completion_tokens"]
    if body.get("total_tokens") is not None:
        span.total_tokens = body["total_tokens"]
    if body.get("cost") is not None:
        span.cost = body["cost"]

    end_time = parse_dt(body.get("end_time"))
    if end_time is not None:
        span.end_time = end_time
        if span.start_time is not None:
            delta = as_utc(span.end_time) - as_utc(span.start_time)
            if delta.total_seconds() < 0:
                raise HTTPException(422, "end_time is earlier than start_time")
            span.latency_ms = int(delta.total_seconds() * 1000)
    if "total_tokens" not in body and ("prompt_tokens" in body or "completion_tokens" in body):
        span.total_tokens = (span.prompt_tokens or 0) + (span.completion_tokens or 0)
    return span.trace_id


def recompute_trace(db: Session, trace_id: str) -> None:
    """把 span 的 token / 成本 / 时间 / 状态聚合回 trace。"""
    trace = db.get(Trace, trace_id)
    if trace is None:
        return

    spans = db.scalars(select(Span).where(Span.trace_id == trace_id)).all()
    if not spans:
        return

    trace.prompt_tokens = sum(s.prompt_tokens or 0 for s in spans)
    trace.completion_tokens = sum(s.completion_tokens or 0 for s in spans)
    trace.total_tokens = sum(s.total_tokens or 0 for s in spans)
    trace.total_cost = sum(float(s.cost or 0) for s in spans)

    starts = [as_utc(s.start_time) for s in spans if s.start_time is not None]
    ends = [as_utc(s.end_time) for s in spans if s.end_time is not None]
    if starts:
        trace.start_time = min(starts + ([as_utc(trace.start_time)] if trace.start_time else []))
    if ends:
        trace.end_time = max(ends)
    if trace.start_time and trace.end_time:
        trace.latency_ms = int(
            (as_utc(trace.end_time) - as_utc(trace.start_time)).total_seconds() * 1000
        )

    statuses = {s.status for s in spans}
    if "running" in statuses:
        trace.status = "running"
    elif "error" in statuses:
        trace.status = "error"
    else:
        trace.status = "success"

    roots = [s for s in spans if not s.parent_span_id]
    if roots and roots[0].output is not None:
        trace.output = roots[0].output


@router.post("/ingestion")
def ingest(payload: IngestionBatch, db: Session = Depends(get_db)) -> dict:
    affected: set[str] = set()
    fresh = []
    seen = {}
    duplicates = 0
    try:
        for event in payload.batch:
            digest = hashlib.sha256(json.dumps({"type": event.type, "body": event.body}, sort_keys=True).encode()).hexdigest()
            if event.id:
                receipt = db.get(EventReceipt, event.id)
                existing = receipt.digest if receipt else seen.get(event.id)
                if existing:
                    if existing != digest:
                        raise HTTPException(409, "event id reused with different content")
                    duplicates += 1
                    continue
                seen[event.id] = digest
                db.add(EventReceipt(id=event.id, digest=digest))
            fresh.append(event)
        # Allow same-batch update-before-create; cross-batch dependencies return 409.
        for kind, handler in [("trace-create", _trace_create), ("span-create", _span_create), ("span-update", _span_update)]:
            for event in fresh:
                if event.type == kind:
                    affected.add(handler(db, redact(event.body)))
                    db.flush()
        for trace_id in affected:
            spans = db.scalars(select(Span).where(Span.trace_id == trace_id)).all()
            parents = {s.id: s.parent_span_id for s in spans}
            for span in spans:
                visited = {span.id}
                parent = span.parent_span_id
                while parent:
                    if parent in visited:
                        raise HTTPException(422, "span parent cycle")
                    visited.add(parent)
                    if parent not in parents:
                        raise HTTPException(409, "parent span is missing or belongs to another trace")
                    parent = parents[parent]
            recompute_trace(db, trace_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "concurrent event conflict; retry the batch")
    except Exception:
        db.rollback()
        raise
    return {"accepted": len(fresh), "duplicates": duplicates}
