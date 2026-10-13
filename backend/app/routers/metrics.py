from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Trace

router = APIRouter(prefix="/api/public/metrics", tags=["metrics"])


@router.get("/summary")
def summary(db: Session = Depends(get_db)) -> dict:
    total = db.scalar(select(func.count(Trace.id))) or 0
    cost = db.scalar(select(func.coalesce(func.sum(Trace.total_cost), 0)))
    tokens = db.scalar(select(func.coalesce(func.sum(Trace.total_tokens), 0)))
    avg_latency = db.scalar(select(func.avg(Trace.latency_ms)))
    errors = db.scalar(select(func.count(Trace.id)).where(Trace.status == "error")) or 0

    return {
        "total_traces": total,
        "total_cost": float(cost or 0),
        "total_tokens": int(tokens or 0),
        "avg_latency_ms": round(float(avg_latency), 1) if avg_latency is not None else 0.0,
        "error_traces": errors,
        "error_rate": round(errors / total, 4) if total else 0.0,
    }


@router.get("/timeline")
def timeline(
    days: int = Query(7, ge=1, le=90), db: Session = Depends(get_db)
) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    if db.bind.dialect.name == "sqlite":
        day = func.strftime("%Y-%m-%d", Trace.created_at).label("day")
    else:
        day = func.date_trunc("day", Trace.created_at).label("day")

    stmt = (
        select(
            day,
            func.count(Trace.id).label("count"),
            func.coalesce(func.sum(Trace.total_cost), 0).label("cost"),
            func.coalesce(func.sum(Trace.total_tokens), 0).label("tokens"),
        )
        .where(Trace.created_at >= cutoff)
        .group_by(day)
        .order_by(day)
    )

    result = []
    for row in db.execute(stmt).all():
        day_value = row.day
        day_str = day_value.isoformat() if hasattr(day_value, "isoformat") else str(day_value)
        result.append(
            {
                "day": day_str,
                "count": row.count,
                "cost": float(row.cost),
                "tokens": int(row.tokens),
            }
        )
    return result
