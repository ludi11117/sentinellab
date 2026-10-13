import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# Postgres 用 JSONB，SQLite 等回退到通用 JSON
JSONType = JSON().with_variant(JSONB, "postgresql")


def _uuid() -> str:
    return str(uuid.uuid4())


class Trace(Base):
    """一次 Agent 执行 = 一条 trace。"""

    __tablename__ = "traces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), index=True)
    user_id: Mapped[str | None] = mapped_column(String(255), index=True)
    session_id: Mapped[str | None] = mapped_column(String(255), index=True)

    input: Mapped[dict | None] = mapped_column(JSONType)
    output: Mapped[dict | None] = mapped_column(JSONType)
    tags: Mapped[list | None] = mapped_column(JSONType)
    meta: Mapped[dict | None] = mapped_column("metadata", JSONType)

    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost: Mapped[float] = mapped_column(Numeric(14, 6), default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    spans: Mapped[list["Span"]] = relationship(
        back_populates="trace", cascade="all, delete-orphan"
    )


class Span(Base):
    """Trace 内部的一个步骤：generation / tool / chain / span / event。"""

    __tablename__ = "spans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("traces.id", ondelete="CASCADE"), index=True
    )
    parent_span_id: Mapped[str | None] = mapped_column(String(36), index=True)

    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(32), default="span", index=True)

    input: Mapped[dict | None] = mapped_column(JSONType)
    output: Mapped[dict | None] = mapped_column(JSONType)
    meta: Mapped[dict | None] = mapped_column("metadata", JSONType)

    model: Mapped[str | None] = mapped_column(String(128), index=True)
    model_params: Mapped[dict | None] = mapped_column(JSONType)

    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    error: Mapped[str | None] = mapped_column(Text)

    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Numeric(14, 6), default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    trace: Mapped[Trace] = relationship(back_populates="spans")


Index("ix_spans_trace_start", Span.trace_id, Span.start_time)
Index("ix_traces_created", Trace.created_at)


class EventReceipt(Base):
    __tablename__ = "event_receipts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    # Immutable snapshot: experiments refer to the exact content hash.
    digest: Mapped[str] = mapped_column(String(64), index=True)
    items: Mapped[list] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Experiment(Base):
    __tablename__ = "experiments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    revision: Mapped[str] = mapped_column(String(255))
    evaluator_version: Mapped[str] = mapped_column(String(64))
    results: Mapped[list] = mapped_column(JSONType)
    summary: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GateReport(Base):
    __tablename__ = "gate_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    baseline_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"))
    candidate_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"))
    policy: Mapped[dict] = mapped_column(JSONType)
    report: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(Base):
    """Control catalog; each project uses its own data database."""
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccessKey(Base):
    __tablename__ = "access_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16))
    revoked: Mapped[int] = mapped_column(Integer, default=0)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"))
    revision: Mapped[str] = mapped_column(String(64))
    # Bundle snapshot includes corpus and implementation version for reproducibility.
    spec: Mapped[dict] = mapped_column(JSONType)
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    fence: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str | None] = mapped_column(String(64))
    lease_until_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CaseRun(Base):
    __tablename__ = "case_runs"
    __table_args__ = (UniqueConstraint("job_id", "item_id", name="uq_job_case"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    item_id: Mapped[str] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(ForeignKey("traces.id"))
    result: Mapped[dict] = mapped_column(JSONType)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewRound(Base):
    __tablename__ = "review_rounds"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    baseline_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"))
    candidate_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"))
    ordering: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewVote(Base):
    __tablename__ = "review_votes"
    __table_args__ = (UniqueConstraint("round_id", "item_id", "reviewer_key_id", name="uq_review_vote"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    round_id: Mapped[str] = mapped_column(ForeignKey("review_rounds.id"))
    item_id: Mapped[str] = mapped_column(String(64))
    reviewer_key_id: Mapped[str] = mapped_column(String(36))
    reviewer_name: Mapped[str] = mapped_column(String(255))
    choice: Mapped[str] = mapped_column(String(16))
    rationale: Mapped[str] = mapped_column(Text)


class SelfReviewVote(Base):
    """Developer/agent dogfooding never satisfies human acceptance coverage."""
    __tablename__ = "self_review_votes"
    __table_args__ = (UniqueConstraint("round_id", "item_id", "reviewer_key_id", name="uq_self_review_vote"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    round_id: Mapped[str] = mapped_column(ForeignKey("review_rounds.id"))
    item_id: Mapped[str] = mapped_column(String(64))
    reviewer_key_id: Mapped[str] = mapped_column(String(36))
    reviewer_name: Mapped[str] = mapped_column(String(255))
    choice: Mapped[str] = mapped_column(String(16))
    rationale: Mapped[str] = mapped_column(Text)


class Acceptance(Base):
    __tablename__ = "acceptances"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    gate_id: Mapped[str] = mapped_column(ForeignKey("gate_reports.id"))
    review_round_id: Mapped[str] = mapped_column(ForeignKey("review_rounds.id"))
    record: Mapped[dict] = mapped_column(JSONType)
    digest: Mapped[str] = mapped_column(String(64))
