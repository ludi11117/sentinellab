from datetime import datetime
from decimal import Decimal
from typing import Any

from .models import Span, Trace
from .timeutil import as_utc


def iso(value: datetime | None) -> str | None:
    value = as_utc(value)
    return value.isoformat() if value else None


def num(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def serialize_trace(trace: Trace) -> dict:
    return {
        "id": trace.id,
        "name": trace.name,
        "user_id": trace.user_id,
        "session_id": trace.session_id,
        "input": trace.input,
        "output": trace.output,
        "tags": trace.tags,
        "metadata": trace.meta,
        "status": trace.status,
        "start_time": iso(trace.start_time),
        "end_time": iso(trace.end_time),
        "latency_ms": trace.latency_ms,
        "prompt_tokens": trace.prompt_tokens,
        "completion_tokens": trace.completion_tokens,
        "total_tokens": trace.total_tokens,
        "total_cost": num(trace.total_cost),
        "created_at": iso(trace.created_at),
    }


def serialize_span(span: Span) -> dict:
    return {
        "id": span.id,
        "trace_id": span.trace_id,
        "parent_span_id": span.parent_span_id,
        "name": span.name,
        "type": span.type,
        "input": span.input,
        "output": span.output,
        "metadata": span.meta,
        "model": span.model,
        "model_params": span.model_params,
        "status": span.status,
        "error": span.error,
        "start_time": iso(span.start_time),
        "end_time": iso(span.end_time),
        "latency_ms": span.latency_ms,
        "prompt_tokens": span.prompt_tokens,
        "completion_tokens": span.completion_tokens,
        "total_tokens": span.total_tokens,
        "cost": num(span.cost),
    }
