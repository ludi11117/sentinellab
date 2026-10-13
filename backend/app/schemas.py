from typing import Any, Literal
from datetime import datetime

from pydantic import BaseModel, Field, ConfigDict, model_validator


class TraceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=36, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=255)
    user_id: str | None = None
    session_id: str | None = None
    input: Any = None
    output: Any = None
    metadata: Any = None
    tags: list[str] | None = None
    status: Literal["running", "success", "error"] | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None


class SpanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=36, pattern=r"^[a-zA-Z0-9_-]+$")
    trace_id: str = Field(min_length=1, max_length=36)
    parent_span_id: str | None = None
    name: str
    type: Literal["span", "generation", "tool", "chain", "event"] = "span"
    input: Any = None
    metadata: Any = None
    model: str | None = None
    model_params: Any = None
    start_time: datetime | None = None


class SpanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=36)
    output: Any = None
    metadata: Any = None
    status: Literal["running", "success", "error"] | None = None
    error: str | None = None
    end_time: datetime | None = None
    prompt_tokens: int | None = Field(None, ge=0)
    completion_tokens: int | None = Field(None, ge=0)
    total_tokens: int | None = Field(None, ge=0)
    cost: float | None = Field(None, ge=0, allow_inf_nan=False)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(None, min_length=1, max_length=64)
    type: Literal["trace-create", "span-create", "span-update"]
    body: dict[str, Any]

    @model_validator(mode="after")
    def validate_body(self):
        schema = {"trace-create": TraceCreate, "span-create": SpanCreate, "span-update": SpanUpdate}[self.type]
        self.body = schema.model_validate(self.body).model_dump(mode="json", exclude_unset=True)
        return self


class IngestionBatch(BaseModel):
    batch: list[Event] = Field(min_length=1, max_length=1000)
