"""Database CAS leases with fencing, atomic case checkpoints, and fenced publication."""
import time
from dataclasses import dataclass

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from .models import AuditEvent, CaseRun, Dataset, Experiment, Job
from .evaluation import EVALUATOR_VERSION, summarize


def now_ms():
    return int(time.time() * 1000)


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    job_id: str
    owner: str
    fence: int
    dataset_id: str
    revision: str
    spec: dict


def available(now):
    return or_(Job.state == "queued", and_(Job.state == "running", Job.lease_until_ms <= now))


def ownership(lease, now):
    return and_(Job.id == lease.job_id, Job.state == "running", Job.owner == lease.owner,
                Job.fence == lease.fence, Job.lease_until_ms > now)


def claim(engine, owner, lease_ms=10000, timestamp=None):
    now = now_ms() if timestamp is None else timestamp
    with Session(engine, expire_on_commit=False) as db:
        expired = db.scalars(select(Job).where(Job.state == "running", Job.lease_until_ms <= now, Job.attempts >= Job.max_attempts)).all()
        for job in expired:
            changed = db.execute(update(Job).where(Job.id == job.id, Job.state == "running", Job.fence == job.fence, Job.lease_until_ms <= now)
                                 .values(state="failed", fence=Job.fence + 1, error="lease expired after max attempts")).rowcount
            if changed:
                db.add(AuditEvent(job_id=job.id, action="job.exhausted", detail={"attempts": job.attempts}))
        db.commit()
        ids = list(db.scalars(select(Job.id).where(available(now), Job.attempts < Job.max_attempts).order_by(Job.created_at, Job.id).limit(20)))
        db.rollback()  # Release read transaction before competing for a write.
        for identifier in ids:
            changed = db.execute(update(Job).where(Job.id == identifier, available(now), Job.attempts < Job.max_attempts)
                                 .values(state="running", owner=owner, lease_until_ms=now + lease_ms,
                                         attempts=Job.attempts + 1, fence=Job.fence + 1, error=None)).rowcount
            if not changed:
                db.rollback()
                continue
            job = db.get(Job, identifier)
            lease = Lease(job.id, owner, job.fence, job.dataset_id, job.revision, job.spec)
            db.add(AuditEvent(job_id=job.id, action="job.claimed", detail={"owner": owner, "fence": job.fence, "attempt": job.attempts}))
            db.commit()
            return lease
    return None


def heartbeat(engine, lease, lease_ms=10000, timestamp=None):
    now = now_ms() if timestamp is None else timestamp
    with Session(engine) as db:
        changed = db.execute(update(Job).where(ownership(lease, now)).values(lease_until_ms=now + lease_ms)).rowcount
        db.commit()
    return bool(changed)


def checkpoint(engine, lease, item_id, writer, lease_ms=10000, timestamp=None):
    """writer(db) writes trace/spans and returns score; same transaction as checkpoint."""
    now = now_ms() if timestamp is None else timestamp
    with Session(engine) as db:
        if not db.execute(update(Job).where(ownership(lease, now)).values(lease_until_ms=now + lease_ms)).rowcount:
            db.rollback()
            raise LeaseLost("checkpoint rejected: lease expired, cancelled or taken over")
        existing = db.scalar(select(CaseRun).where(CaseRun.job_id == lease.job_id, CaseRun.item_id == item_id))
        if existing:
            db.commit()
            return existing.result
        result = writer(db)
        db.add(CaseRun(job_id=lease.job_id, item_id=item_id, trace_id=result["trace_id"], result=result))
        # Recheck after the computation so a long operation cannot publish an expired lease.
        check_time = now_ms() if timestamp is None else timestamp
        changed = db.execute(update(Job).where(ownership(lease, check_time)).values(completed=Job.completed + 1)).rowcount
        if not changed:
            db.rollback()
            raise LeaseLost("checkpoint exceeded lease; transaction rolled back")
        db.add(AuditEvent(job_id=lease.job_id, action="case.checkpoint", detail={"item_id": item_id, "trace_id": result["trace_id"], "fence": lease.fence}))
        db.commit()
        return result


def publish(engine, lease, timestamp=None):
    now = now_ms() if timestamp is None else timestamp
    with Session(engine, expire_on_commit=False) as db:
        if not db.execute(update(Job).where(ownership(lease, now)).values(lease_until_ms=Job.lease_until_ms)).rowcount:
            db.rollback()
            raise LeaseLost("publication rejected: stale lease")
        dataset = db.get(Dataset, lease.dataset_id)
        cases = {c.item_id: c.result for c in db.scalars(select(CaseRun).where(CaseRun.job_id == lease.job_id))}
        if set(cases) != {item["id"] for item in dataset.items}:
            db.rollback()
            raise ValueError("cannot publish incomplete experiment")
        results = [cases[item["id"]] for item in dataset.items]
        experiment = Experiment(dataset_id=dataset.id, name="Delivery evaluation", revision=lease.revision,
                                evaluator_version=EVALUATOR_VERSION, results=results, summary=summarize(results))
        db.add(experiment)
        db.flush()
        check_time = now_ms() if timestamp is None else timestamp
        if not db.execute(update(Job).where(ownership(lease, check_time)).values(state="succeeded", experiment_id=experiment.id, owner=None, lease_until_ms=0)).rowcount:
            db.rollback()
            raise LeaseLost("publication exceeded lease")
        db.add(AuditEvent(job_id=lease.job_id, action="job.published", detail={"experiment_id": experiment.id, "fence": lease.fence}))
        db.commit()
        return experiment.id


def fail(engine, lease, error, timestamp=None):
    now = now_ms() if timestamp is None else timestamp
    with Session(engine) as db:
        job = db.get(Job, lease.job_id)
        state = "queued" if job and job.attempts < job.max_attempts else "failed"
        changed = db.execute(update(Job).where(ownership(lease, now)).values(state=state, owner=None, lease_until_ms=0, error=str(error)[:1000], fence=Job.fence + 1)).rowcount
        if changed:
            db.add(AuditEvent(job_id=lease.job_id, action="job.retry" if state == "queued" else "job.failed", detail={"error": str(error)[:1000]}))
        db.commit()
        return bool(changed)


def cancel(db, job_id):
    changed = db.execute(update(Job).where(Job.id == job_id, Job.state.in_(["queued", "running"]))
                         .values(state="cancelled", fence=Job.fence + 1, owner=None, lease_until_ms=0)).rowcount
    if changed:
        db.add(AuditEvent(job_id=job_id, action="job.cancelled", detail={}))
    db.commit()
    return bool(changed)
