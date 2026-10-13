"""Standalone worker; multiple processes may compete via database leases."""
import argparse
import os
import time
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Base, engine
from .evaluation import score
from .jobqueue import LeaseLost, checkpoint, claim, fail, heartbeat, publish
from .models import CaseRun, Dataset, Project, Span, Trace
from .projects import project_engine
from .retrieval import PIPELINE_VERSION, execute


class LeaseKeeper:
    """Keep ownership during long model RPCs; cannot revive expired/cancelled fences."""
    def __init__(self, engine, lease, lease_ms=10000, interval=2):
        self.engine, self.lease = engine, lease
        self.lease_ms, self.interval = lease_ms, interval
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.wait(self.interval):
            try:
                if not heartbeat(self.engine, self.lease, self.lease_ms):
                    self.lost.set()
                    return
            except Exception:
                self.lost.set()
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=16)
        if self.lost.is_set() and args[0] is None:
            raise LeaseLost("ownership lost during model call; discard returned response")


def execute_case(engine, lease, item):
    start = datetime.now(timezone.utc)
    tick = time.perf_counter()
    with LeaseKeeper(engine, lease):
        execution = execute(lease.revision, item["input"]["question"], lease.spec["corpus"], lease.spec.get("model_profile"))
    end = datetime.now(timezone.utc)
    duration = max(1, round((time.perf_counter() - tick) * 1000))

    def write(db):
        generation = execution.generation
        usage = generation.get("usage", {}) if generation else {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        tid = str(uuid.uuid5(uuid.UUID(lease.job_id), item["id"]))
        trace = Trace(id=tid, name="delivery-evaluation", input=item["input"], output=execution.output,
                      status="error" if execution.denied_tools else "success", start_time=start, end_time=end,
                      latency_ms=duration, total_cost=0, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                      total_tokens=usage.get("total_tokens", prompt_tokens + completion_tokens),
                      meta={"job_id": lease.job_id, "revision": lease.revision, "executor": lease.spec["pipeline_version"], "synthetic": False,
                            "llm_calls": int(generation is not None), "provider": generation.get("profile") if generation else None,
                            "cost_basis": generation.get("cost_basis") if generation else "no_model"})
        db.add(trace)
        db.flush()
        root = Span(id=str(uuid.uuid4()), trace_id=tid, name="quotation-agent", type="chain", status=trace.status,
                    start_time=start, end_time=end, latency_ms=duration, input=item["input"], output=execution.output)
        retrieval = Span(id=str(uuid.uuid4()), trace_id=tid, parent_span_id=root.id, name="knowledge.search", type="tool",
                         status="success", start_time=start, end_time=end, latency_ms=duration, output={"hits": execution.retrieval})
        spans = [root, retrieval]
        if generation:
            spans.append(Span(id=str(uuid.uuid4()), trace_id=tid, parent_span_id=root.id, name="model.answer", type="generation",
                              model=generation["profile"]["model"], model_params=generation["profile"], status="success",
                              start_time=start, end_time=end, latency_ms=generation["latency_ms"], prompt_tokens=prompt_tokens,
                              completion_tokens=completion_tokens, total_tokens=trace.total_tokens,
                              output=generation["raw_output"], meta={"request_digest": generation["request_digest"],
                              "finish_reason": generation["finish_reason"], "response_id": generation["response_id"]}))
        for name in execution.denied_tools:
            spans.append(Span(id=str(uuid.uuid4()), trace_id=tid, parent_span_id=root.id, name=name, type="tool", status="error",
                              start_time=end, end_time=end, latency_ms=0, error="runtime policy denied execution; no external side effect", meta={"executed": False, "policy": "read-and-draft-only"}))
        db.add_all(spans)
        db.flush()
        return score(item, trace, spans)

    return checkpoint(engine, lease, item["id"], write)


def process(engine, lease, pause_after=0, pause_seconds=0, marker=None):
    if lease.spec["pipeline_version"] != PIPELINE_VERSION:
        raise ValueError("worker implementation differs from the job snapshot; use the matching worker version")
    with Session(engine) as db:
        items = db.get(Dataset, lease.dataset_id).items
        done = set(db.scalars(select(CaseRun.item_id).where(CaseRun.job_id == lease.job_id)))
    executed = 0
    for item in items:
        if item["id"] in done:
            continue
        if not heartbeat(engine, lease):
            raise LeaseLost("worker lost ownership before execution")
        execute_case(engine, lease, item)
        executed += 1
        if pause_after and executed == pause_after:
            if marker:
                Path(marker).write_text(lease.job_id, encoding="utf-8")
            # Fault-injection hook: no heartbeat while paused, so lease may expire.
            time.sleep(pause_seconds)
    return publish(engine, lease)


def run(once=False, idle_seconds=.2, pause_after=0, pause_seconds=0, marker=None):
    owner = f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    Base.metadata.create_all(engine)
    while True:
        with Session(engine) as db:
            project_ids = ["local"] + list(db.scalars(select(Project.id)))
        claimed = False
        for project_id in project_ids:
            target = project_engine(project_id)
            lease = claim(target, owner)
            if not lease:
                continue
            claimed = True
            try:
                experiment_id = process(target, lease, pause_after, pause_seconds, marker)
                print(f"published {lease.job_id} -> {experiment_id}", flush=True)
            except LeaseLost as exc:
                print(f"fenced {lease.job_id}: {exc}", flush=True)
            except Exception as exc:
                fail(target, lease, exc)
                print(f"failed {lease.job_id}: {type(exc).__name__}: {exc}", flush=True)
            if once:
                return
        if once:
            return
        if not claimed:
            time.sleep(idle_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--pause-after", type=int, default=0, help="test only: pause after N committed cases")
    parser.add_argument("--pause-seconds", type=float, default=0)
    parser.add_argument("--marker", help="test only: write marker when paused")
    args = parser.parse_args()
    run(args.once, pause_after=args.pause_after, pause_seconds=args.pause_seconds, marker=args.marker)
