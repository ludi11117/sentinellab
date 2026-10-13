"""追踪原语：@observe 装饰器、trace 上下文管理器、Span。"""

from __future__ import annotations

import contextvars
import functools
import inspect
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable

from .client import get_client

_current_span: contextvars.ContextVar["Span | None"] = contextvars.ContextVar(
    "agent_trace_current_span", default=None
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Span:
    def __init__(
        self,
        name: str,
        as_type: str = "span",
        input: Any = None,
        model: str | None = None,
        model_params: dict | None = None,
        metadata: dict | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.id = str(uuid.uuid4())
        self.name = name
        self.type = as_type
        self.input = input
        self.model = model
        self.model_params = model_params
        self.metadata = metadata
        self.user_id = user_id
        self.session_id = session_id
        self.trace_id = trace_id
        self.parent_span_id: str | None = None

        self._output: Any = None
        self._usage: dict[str, Any] | None = None
        self._token: contextvars.Token | None = None
        self._client = get_client()

    def __enter__(self) -> "Span":
        parent = _current_span.get()
        if parent is not None:
            self.parent_span_id = parent.id
            self.trace_id = parent.trace_id
        else:
            self.trace_id = self.trace_id or str(uuid.uuid4())
            self._client.enqueue(
                {
                    "type": "trace-create",
                    "body": {
                        "id": self.trace_id,
                        "name": self.name,
                        "user_id": self.user_id,
                        "session_id": self.session_id,
                        "input": self.input,
                        "metadata": self.metadata,
                        "start_time": _now(),
                    },
                }
            )

        self._token = _current_span.set(self)
        self._client.enqueue(
            {
                "type": "span-create",
                "body": {
                    "id": self.id,
                    "trace_id": self.trace_id,
                    "parent_span_id": self.parent_span_id,
                    "name": self.name,
                    "type": self.type,
                    "input": self.input,
                    "metadata": self.metadata,
                    "model": self.model,
                    "model_params": self.model_params,
                    "start_time": _now(),
                },
            }
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        body: dict[str, Any] = {
            "id": self.id,
            "end_time": _now(),
            "status": "error" if exc_type else "success",
        }
        if exc is not None:
            body["error"] = f"{exc_type.__name__}: {exc}"
        if self._output is not None:
            body["output"] = self._output
        if self._usage:
            body.update(self._usage)
        if self.metadata is not None:
            body["metadata"] = self.metadata

        self._client.enqueue({"type": "span-update", "body": body})

        if self._token is not None:
            _current_span.reset(self._token)
        return False

    def update(
        self,
        output: Any = None,
        usage: dict[str, int] | None = None,
        cost: float | None = None,
        metadata: dict | None = None,
    ) -> "Span":
        if output is not None:
            self._output = output
        if usage:
            self._usage = {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)),
            }
        if cost is not None:
            self._usage = self._usage or {}
            self._usage["cost"] = cost
        if metadata is not None:
            self.metadata = metadata
        return self


def get_current_span() -> Span | None:
    return _current_span.get()


def get_current_trace_id() -> str | None:
    span = _current_span.get()
    return span.trace_id if span else None


@contextmanager
def trace(
    name: str,
    user_id: str | None = None,
    session_id: str | None = None,
    input: Any = None,
    metadata: dict | None = None,
):
    span = Span(
        name,
        input=input,
        metadata=metadata,
        user_id=user_id,
        session_id=session_id,
    )
    with span:
        yield span


def observe(
    name: str | None = None,
    as_type: str = "span",
    model: str | None = None,
    model_params: dict | None = None,
) -> Callable:
    def decorator(func: Callable) -> Callable:
        span_name = name or func.__name__

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                with Span(
                    span_name, as_type=as_type, model=model, model_params=model_params
                ) as span:
                    result = await func(*args, **kwargs)
                    span.update(output=result)
                    return result

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with Span(
                span_name, as_type=as_type, model=model, model_params=model_params
            ) as span:
                result = func(*args, **kwargs)
                span.update(output=result)
                return result

        return wrapper

    return decorator
