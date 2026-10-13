"""Real process kill and multi-process recovery proof, in an isolated D-drive database."""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAB = Path("D:/CodexWork/temp") / ("sentinellab-fault-" + uuid.uuid4().hex)
LAB.mkdir(parents=True)
os.environ["DATABASE_URL"] = "sqlite:///" + (LAB / "queue.db").as_posix()
os.environ["SENTINEL_PROJECT_DATA"] = str(LAB / "projects")
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db import Base, engine
from app.evaluation import content_hash
from app.jobqueue import Lease, LeaseLost, heartbeat, publish
from app.models import CaseRun, Dataset, Experiment, Job, Trace
from app.retrieval import PIPELINE_VERSION
from quotation_fixture import fixture


def create_job():
    corpus, items = fixture()
    with Session(engine, expire_on_commit=False) as db:
        dataset = Dataset(name="fault-recovery", digest=content_hash(items), items=items)
        db.add(dataset)
        db.flush()
        job = Job(request_key=uuid.uuid4().hex, request_digest="fault-test", dataset_id=dataset.id, revision="retrieval-v2",
                  spec={"corpus": corpus, "corpus_digest": content_hash(corpus), "pipeline_version": PIPELINE_VERSION})
        db.add(job)
        db.commit()
        return job.id, len(items)


def spawn(name, args):
    log = open(LAB / (name + ".log"), "w", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-m", "app.worker", "--once", *args], cwd=ROOT / "backend", env=os.environ.copy(),
                               stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return process, log


def kill_tree(process):
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=True, capture_output=True)
        else:
            process.kill()
        process.wait(timeout=10)


def main():
    Base.metadata.create_all(engine)
    job_id, total = create_job()
    marker = LAB / "paused.txt"
    crashed, log = spawn("crashed", ["--pause-after", "5", "--pause-seconds", "60", "--marker", str(marker)])
    try:
        deadline = time.monotonic() + 15
        while not marker.exists():
            if crashed.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("worker failed to reach fault checkpoint; inspect " + str(LAB))
            time.sleep(.05)
        with Session(engine) as db:
            job = db.get(Job, job_id)
            old = Lease(job.id, job.owner, job.fence, job.dataset_id, job.revision, job.spec)
            before = job.completed
            expiry = job.lease_until_ms
        kill_tree(crashed)
    finally:
        kill_tree(crashed)
        log.close()
    print(f"Killed real worker after {before}/{total} committed checkpoints; waiting for lease expiry", flush=True)
    while int(time.time() * 1000) <= expiry:
        time.sleep(.1)
    recovery_start = time.perf_counter()
    recovered, log = spawn("recovered", [])
    try:
        assert recovered.wait(timeout=30) == 0
    finally:
        kill_tree(recovered)
        log.close()
    recovery_seconds = time.perf_counter() - recovery_start
    with Session(engine) as db:
        job = db.get(Job, job_id)
        cases = list(db.scalars(select(CaseRun).where(CaseRun.job_id == job_id)))
        traces = list(db.scalars(select(Trace)))
        experiments = list(db.scalars(select(Experiment)))
        assert job.state == "succeeded" and job.attempts == 2
        assert len(cases) == total and len(traces) == total and len(experiments) == 1
        assert len({c.item_id for c in cases}) == total
    assert not heartbeat(engine, old)
    rejected = False
    try:
        publish(engine, old)
    except LeaseLost:
        rejected = True
    assert rejected
    # Independent multi-process competition test.
    competing_job, count = create_job()
    workers = [spawn("competition-" + str(i), []) for i in range(2)]
    try:
        for worker, handle in workers:
            assert worker.wait(timeout=30) == 0
    finally:
        for worker, handle in workers:
            kill_tree(worker)
            handle.close()
    with Session(engine) as db:
        race = db.get(Job, competing_job)
        assert race.state == "succeeded" and race.attempts == 1
        assert len(list(db.scalars(select(CaseRun).where(CaseRun.job_id == competing_job)))) == count
        assert len(list(db.scalars(select(Experiment)))) == 2
    report = {"real_process_fault": True, "mocked": False, "database": str(LAB / "queue.db"),
              "killed_after_checkpoints": before, "total_cases": total, "recovered_cases": total - before,
              "final_trace_count": len(traces), "final_experiment_count": len(experiments), "attempts": 2,
              "stale_publication_rejected": rejected, "recovery_seconds_after_expiry": recovery_seconds,
              "two_process_claim_attempts": race.attempts,
              "limitations": "Single host SQLite; real process interruption after checkpoint, not machine power failure or a multi-host distributed test."}
    target = ROOT / "artifacts" / "fault-lab-report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
