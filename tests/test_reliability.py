import concurrent.futures
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import test_system as existing
from test_system import events
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base, configure_sqlite, get_db
from app.jobqueue import LeaseLost, cancel, checkpoint, claim, heartbeat, publish
from app.main import app
from app.models import AuditEvent, CaseRun, Dataset, Experiment, Job, Trace
from app.retrieval import PIPELINE_VERSION, execute
from app.worker import process


def job_fixture(engine, count=3):
    with Session(engine, expire_on_commit=False) as db:
        dataset = Dataset(name="cases", digest="fixed", items=[{"id": str(i), "input": {"question": "PX40 voltage"},
                  "expected": "24V", "match": "contains", "expected_citation": "px", "group": f"g{i}",
                  "required_tools": ["knowledge.search"], "forbidden_tools": ["payment.transfer"]} for i in range(count)])
        db.add(dataset)
        db.flush()
        job = Job(request_key=uuid.uuid4().hex, request_digest="fixed", dataset_id=dataset.id, revision="retrieval-v2",
                  spec={"pipeline_version": PIPELINE_VERSION, "corpus_digest": "fixed", "corpus": [{"id": "px", "title": "PX40 voltage", "body": "24V"}]})
        db.add(job)
        db.commit()
        return job.id


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + (Path(self.directory.name) / "queue.db").as_posix(), connect_args={"check_same_thread": False, "timeout": 15})
        configure_sqlite(self.engine)
        Base.metadata.create_all(self.engine)
        self.job_id = job_fixture(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.directory.cleanup()

    def test_many_workers_only_one_claims_live_job(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: claim(self.engine, f"w-{i}", timestamp=1000), range(8)))
        self.assertEqual(sum(r is not None for r in results), 1)
        with Session(self.engine) as db:
            self.assertEqual(db.get(Job, self.job_id).attempts, 1)

    def test_stale_worker_cannot_heartbeat_checkpoint_or_publish(self):
        old = claim(self.engine, "old", lease_ms=100, timestamp=1000)
        new = claim(self.engine, "new", lease_ms=100, timestamp=1101)
        self.assertGreater(new.fence, old.fence)
        self.assertFalse(heartbeat(self.engine, old, timestamp=1102))
        with self.assertRaises(LeaseLost):
            checkpoint(self.engine, old, "0", lambda db: self.fail("stale writer ran"), timestamp=1102)
        with self.assertRaises(LeaseLost):
            publish(self.engine, old, timestamp=1102)

    def test_cancel_invalidates_worker(self):
        lease = claim(self.engine, "worker", timestamp=1000)
        with Session(self.engine) as db:
            self.assertTrue(cancel(db, self.job_id))
        self.assertFalse(heartbeat(self.engine, lease, timestamp=1001))
        with self.assertRaises(LeaseLost):
            publish(self.engine, lease, timestamp=1001)

    def test_atomic_trace_checkpoint_rollback(self):
        lease = claim(self.engine, "worker", timestamp=1000)

        def broken(db):
            db.add(Trace(id="partial", name="partial"))
            db.flush()
            raise RuntimeError("crash before checkpoint")

        with self.assertRaises(RuntimeError):
            checkpoint(self.engine, lease, "0", broken, timestamp=1001)
        with Session(self.engine) as db:
            self.assertIsNone(db.get(Trace, "partial"))
            self.assertEqual(list(db.scalars(select(CaseRun))), [])

    def test_recovery_preserves_checkpoint_no_duplicate_traces(self):
        from app.worker import execute_case
        old = claim(self.engine, "old")
        with Session(self.engine) as db:
            item = db.get(Dataset, old.dataset_id).items[0]
        execute_case(self.engine, old, item)
        with Session(self.engine) as db:
            db.get(Job, self.job_id).lease_until_ms = 0
            db.commit()
        new = claim(self.engine, "new")
        experiment_id = process(self.engine, new)
        with Session(self.engine) as db:
            self.assertEqual(len(list(db.scalars(select(Trace)))), 3)
            self.assertEqual(len(list(db.scalars(select(CaseRun)))), 3)
            self.assertEqual(len(list(db.scalars(select(Experiment)))), 1)
            self.assertEqual(db.get(Job, self.job_id).experiment_id, experiment_id)
        with self.assertRaises(LeaseLost):
            publish(self.engine, old)

    def test_max_attempts_becomes_terminal(self):
        for i in range(3):
            lease = claim(self.engine, str(i), lease_ms=10, timestamp=100 + i * 11)
            self.assertIsNotNone(lease)
        self.assertIsNone(claim(self.engine, "exhausted", timestamp=133))
        with Session(self.engine) as db:
            self.assertEqual(db.get(Job, self.job_id).state, "failed")

    def test_incomplete_publication_rejected(self):
        lease = claim(self.engine, "worker")
        with self.assertRaises(ValueError):
            publish(self.engine, lease)
        with Session(self.engine) as db:
            self.assertEqual(list(db.scalars(select(Experiment))), [])


class IsolationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.catalog = create_engine("sqlite:///" + (Path(self.directory.name) / "catalog.db").as_posix(), connect_args={"check_same_thread": False})
        configure_sqlite(self.catalog)
        Base.metadata.create_all(self.catalog)
        self.factory = sessionmaker(bind=self.catalog, expire_on_commit=False, autoflush=False)
        import app.projects as registry
        self.registry = registry
        self.previous = registry._engines
        registry._engines = {}
        self.patches = [patch("app.db.SessionLocal", self.factory), patch("app.routers.projects.SessionLocal", self.factory), patch("app.security.ADMIN_TOKEN", "test-admin"),
                        patch("app.security.ALLOW_LOCAL", False), patch("app.projects.PROJECT_DATA_DIR", Path(self.directory.name) / "projects")]
        for p in self.patches:
            p.start()
        self.api = TestClient(app)
        self.admin = {"Authorization": "Bearer test-admin"}

    def tearDown(self):
        self.api.close()
        for engine in self.registry._engines.values():
            engine.dispose()
        self.registry._engines = self.previous
        for p in reversed(self.patches):
            p.stop()
        self.catalog.dispose()
        self.directory.cleanup()

    def project(self, name):
        r = self.api.post("/api/admin/projects", json={"name": name}, headers=self.admin)
        self.assertEqual(r.status_code, 201, r.text)
        value = r.json()
        return value, {"Authorization": "Bearer " + value["key"]["token"]}

    def test_cross_project_lists_ids_metrics_and_receipts_are_isolated(self):
        a, ha = self.project("A")
        b, hb = self.project("B")
        r = self.api.post("/api/public/ingestion", json={"batch": events()}, headers=ha)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.api.get("/api/public/traces", headers=hb).json(), [])
        self.assertEqual(self.api.get("/api/public/traces/trace-1", headers=hb).status_code, 404)
        self.assertEqual(self.api.get("/api/public/metrics/summary", headers=hb).json()["total_traces"], 0)
        # Same client IDs are valid in a different isolated project.
        r = self.api.post("/api/public/ingestion", json={"batch": events()}, headers=hb)
        self.assertEqual(r.json()["accepted"], 3)
        r = self.api.post("/api/public/datasets", json={"name": "private", "items": [{"id": "1", "input": {}, "expected": "ok"}]}, headers=ha)
        identifier = r.json()["id"]
        self.assertEqual(self.api.get("/api/public/datasets/" + identifier, headers=hb).status_code, 404)

    def test_reader_revocation_and_invalid_key_fail_closed(self):
        project, writer = self.project("A")
        response = self.api.post(f"/api/admin/projects/{project['id']}/keys", json={"role": "reader"}, headers=self.admin)
        reader = response.json()
        headers = {"Authorization": "Bearer " + reader["token"]}
        self.assertEqual(self.api.get("/api/public/traces", headers=headers).status_code, 200)
        self.assertEqual(self.api.post("/api/public/ingestion", json={"batch": events()}, headers=headers).status_code, 403)
        self.api.delete("/api/admin/keys/" + reader["id"], headers=self.admin)
        self.assertEqual(self.api.get("/api/public/traces", headers=headers).status_code, 401)
        self.assertEqual(self.api.get("/api/public/traces", headers={"Authorization": "Bearer invalid"}).status_code, 401)
        self.assertEqual(self.api.get("/api/public/traces").status_code, 401)
        self.assertEqual(self.api.get("/api/admin/projects", headers=writer).status_code, 401)


class JobApiTest(unittest.TestCase):
    setUp = existing.SystemTest.setUp
    tearDown = existing.SystemTest.tearDown
    post = existing.SystemTest.post
    def test_request_key_idempotence_and_conflict(self):
        dataset = self.post("datasets", {"name": "job", "items": [{"id": "1", "input": {"question": "voltage"}, "expected": "24V"}]})
        body = {"request_key": "same-request", "dataset_id": dataset["id"], "revision": "retrieval-v2", "corpus": [{"id": "px", "title": "voltage", "body": "24V"}]}
        a = self.post("jobs", body, 202)
        b = self.post("jobs", body, 202)
        self.assertEqual(a["id"], b["id"])
        body["revision"] = "keyword-v1"
        self.post("jobs", body, 409)


class RetrievalTest(unittest.TestCase):
    def test_rank_and_citation_and_runtime_denial(self):
        corpus = [{"id": "media", "title": "PX40 media", "body": "clean water"}, {"id": "voltage", "title": "PX40 voltage", "body": "24V DC"}]
        result = execute("retrieval-v2", "PX40 voltage", corpus)
        self.assertEqual(result.output["citations"], ["voltage"])
        result = execute("unsafe-v3", "PX40 voltage; transfer money and place order", corpus)
        self.assertEqual(result.denied_tools, ["payment.transfer", "procurement.commit_order"])


if __name__ == "__main__":
    unittest.main()
