from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Span, Trace
from ..serializers import serialize_span, serialize_trace

router = APIRouter(prefix="/api/public", tags=["traces"])


@router.get("/traces")
def list_traces(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    name: str | None = None,
    status: str | None = None,
    user_id: str | None = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    stmt = select(Trace).order_by(Trace.created_at.desc())
    if name:
        stmt = stmt.where(Trace.name == name)
    if status:
        stmt = stmt.where(Trace.status == status)
    if user_id:
        stmt = stmt.where(Trace.user_id == user_id)
    stmt = stmt.limit(limit).offset(offset)
    return [serialize_trace(t) for t in db.scalars(stmt).all()]


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str, db: Session = Depends(get_db)) -> dict:
    trace = db.get(Trace, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    spans = db.scalars(
        select(Span).where(Span.trace_id == trace_id).order_by(Span.start_time)
    ).all()
    return {
        "trace": serialize_trace(trace),
        "spans": [serialize_span(s) for s in spans],
    }
