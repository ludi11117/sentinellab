import json
import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "sdk")]
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import EventReceipt, Trace
from agent_trace.client import TraceClient


def events(tid="trace-1", answer="ok", cost=.001, latency=100, inp=None):
    return [
        {"id": tid + "-create", "type": "trace-create", "body": {"id": tid, "name": "agent", "input": inp or {"q": "hello"}}},
        {"id": tid + "-span", "type": "span-create", "body": {"id": tid + "-root", "trace_id": tid, "name": "agent", "start_time": "2026-10-13T00:00:00Z"}},
        {"id": tid + "-end", "type": "span-update", "body": {"id": tid + "-root", "status": "success", "output": {"answer": answer},
           "end_time": f"2026-10-13T00:00:{latency // 1000:02d}.{latency % 1000:03d}Z", "cost": cost, "prompt_tokens": 10, "completion_tokens": 5}},
    ]


class SystemTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)

        def db():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as session:
                yield session

        app.dependency_overrides[get_db] = db
        self.api = TestClient(app)

    def tearDown(self):
        self.api.close()
        app.dependency_overrides.clear()
        self.engine.dispose()

    def post(self, path, payload, status=201):
        r = self.api.post("/api/public/" + path, json=payload)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def ingest(self, batch, status=200):
        return self.post("ingestion", {"batch": batch}, status)

    def test_retry_deduplicates_and_does_not_double_cost(self):
        batch = events()
        self.assertEqual(self.ingest(batch)["accepted"], 3)
        self.assertEqual(self.ingest(batch)["duplicates"], 3)
        result = self.api.get("/api/public/traces/trace-1").json()
        self.assertEqual(result["trace"]["total_cost"], .001)
        self.assertEqual(result["trace"]["total_tokens"], 15)
        self.assertEqual(result["trace"]["status"], "success")

    def test_id_collision_does_not_change_trace(self):
        batch = events()
        self.ingest(batch)
        batch[0]["body"]["name"] = "tampered"
        self.ingest(batch, 409)
        self.assertEqual(self.api.get("/api/public/traces/trace-1").json()["trace"]["name"], "agent")

    def test_missing_update_is_not_acknowledged(self):
        batch = [events()[2]]
        self.ingest(batch, 409)
        with Session(self.engine) as db:
            self.assertEqual(list(db.scalars(select(EventReceipt))), [])

    def test_atomic_rollback(self):
        batch = events() + [{"type": "span-update", "body": {"id": "unknown"}}]
        self.ingest(batch, 409)
        with Session(self.engine) as db:
            self.assertIsNone(db.get(Trace, "trace-1"))

    def test_same_batch_out_of_order(self):
        self.ingest(list(reversed(events())))
        self.assertEqual(self.api.get("/api/public/traces/trace-1").json()["trace"]["status"], "success")

    def test_duplicate_in_same_batch(self):
        batch = events()
        self.assertEqual(self.ingest(batch + batch)["duplicates"], 3)

    def test_invalid_input_rejected(self):
        for field, value in [("cost", -1), ("cost", "NaN"), ("prompt_tokens", -2), ("end_time", "not-a-date")]:
            batch = events()
            batch[2]["body"][field] = value
            self.ingest(batch, 422)

    def test_negative_duration(self):
        batch = events()
        batch[2]["body"]["end_time"] = "2025-01-01T00:00:00Z"
        self.ingest(batch, 422)

    def test_parent_cycle_and_cross_trace_rejected(self):
        batch = events()
        batch[1]["body"]["parent_span_id"] = "trace-1-root"
        self.ingest(batch, 422)
        batch[1]["body"]["parent_span_id"] = "missing"
        self.ingest(batch, 409)

    def test_credentials_redacted(self):
        batch = events(inp={"email": "person@example.com", "api_key": "secret-value", "nested": {"password": "123"}})
        self.ingest(batch)
        stored = self.api.get("/api/public/traces/trace-1").json()["trace"]["input"]
        self.assertEqual(stored, {"email": "[EMAIL]", "api_key": "[REDACTED]", "nested": {"password": "[REDACTED]"}})

    def fixture(self, prefix, bad=0, cost=.001, latency=100, count=12):
        dataset = getattr(self, "dataset", None)
        if dataset is None:
            self.dataset = self.post("datasets", {"name": "test", "items": [{"id": str(i), "input": {"q": "hello"}, "expected": "ok"} for i in range(count)]})
        observations = []
        for i in range(count):
            tid = f"{prefix}-{i}"
            self.ingest(events(tid, "bad" if i < bad else "ok", cost, latency))
            observations.append({"item_id": str(i), "trace_id": tid})
        return self.post("experiments", {"dataset_id": self.dataset["id"], "name": prefix, "revision": prefix, "observations": observations})

    def test_release_pass_quality_block_and_cost_block(self):
        baseline = self.fixture("base")
        for prefix, bad, cost, decision in [("good", 0, .001, "pass"), ("bad", 4, .001, "block"), ("pricey", 0, .03, "block")]:
            candidate = self.fixture(prefix, bad, cost)
            gate = self.post("gates", {"baseline_id": baseline["id"], "candidate_id": candidate["id"]})
            self.assertEqual(gate["decision"], decision)
            if bad:
                self.assertEqual(len([p for p in gate["pairs"] if p["delta"] < 0]), bad)

    def test_latency_gate(self):
        baseline = self.fixture("base")
        candidate = self.fixture("slow", latency=5000)
        gate = self.post("gates", {"baseline_id": baseline["id"], "candidate_id": candidate["id"]})
        self.assertEqual(gate["decision"], "block")

    def test_reject_missing_case_wrong_input_and_running_trace(self):
        baseline = self.fixture("base")
        payload = {"dataset_id": self.dataset["id"], "name": "x", "revision": "x", "observations": [{"item_id": "0", "trace_id": "base-0"}]}
        self.post("experiments", payload, 422)
        payload["observations"] = [{"item_id": str(i), "trace_id": f"base-{i}"} for i in range(12)]
        self.ingest(events("different", inp={"q": "other"}))
        payload["observations"][0]["trace_id"] = "different"
        self.post("experiments", payload, 422)
        self.ingest(events("pending")[:2])
        payload["observations"][0]["trace_id"] = "pending"
        self.post("experiments", payload, 409)
        self.post("gates", {"baseline_id": baseline["id"], "candidate_id": baseline["id"]}, 422)

    def test_tool_contracts(self):
        self.dataset = self.post("datasets", {"name": "tools", "items": [{"id": "0", "input": {"q": "hello"}, "expected": "ok", "required_tools": ["lookup"]}]})
        result = self.fixture("no-tool", count=1)
        self.assertEqual(result["summary"]["quality"], 0)
        self.assertFalse(result["results"][0]["checks"]["required_tools"])

    def test_dataset_mismatch_and_min_samples(self):
        baseline = self.fixture("base", count=2)
        candidate = self.fixture("small", count=2)
        gate = self.post("gates", {"baseline_id": baseline["id"], "candidate_id": candidate["id"]})
        self.assertEqual(gate["decision"], "block")
        self.dataset = self.post("datasets", {"name": "different", "items": [{"id": str(i), "input": {"q": "hello"}, "expected": "ok"} for i in range(2)]})
        different = self.fixture("other", count=2)
        self.post("gates", {"baseline_id": baseline["id"], "candidate_id": different["id"]}, 422)

    def test_snapshot_unchanged_after_trace_updates(self):
        baseline = self.fixture("base", count=2)
        self.ingest([{"id": "late-update", "type": "span-update", "body": {"id": "base-0-root", "output": {"answer": "changed"}}}])
        result = self.api.get("/api/public/experiments/" + baseline["id"]).json()
        self.assertEqual(result["results"][0]["output"], {"answer": "ok"})

    def test_forbidden_tool_and_execution_error_are_scored(self):
        dataset = self.post("datasets", {"name": "contract", "items": [{"id": "0", "input": {"q": "hello"}, "expected": "ok", "forbidden_tools": ["unsafe"]}]})
        batch = events()
        batch += [{"type": "span-create", "body": {"id": "unsafe", "trace_id": "trace-1", "parent_span_id": "trace-1-root", "name": "unsafe", "type": "tool"}},
                  {"type": "span-update", "body": {"id": "unsafe", "status": "error", "error": "denied"}}]
        self.ingest(batch)
        experiment = self.post("experiments", {"dataset_id": dataset["id"], "name": "unsafe", "revision": "1", "observations": [{"item_id": "0", "trace_id": "trace-1"}]})
        result = experiment["results"][0]
        self.assertFalse(result["checks"]["forbidden_tools"])
        self.assertFalse(result["checks"]["execution"])

    def test_async_context_isolation(self):
        tracing = importlib.import_module("agent_trace.trace")
        collected = []
        fake_client = type("Client", (), {"enqueue": lambda _, event: collected.append(event)})()

        @tracing.observe(name="async-tool", as_type="tool")
        async def tool():
            await asyncio.sleep(.001)
            return "ok"

        async def root(name):
            with tracing.trace(name, input={"q": name}):
                await asyncio.gather(tool(), tool())

        async def exercise():
            await asyncio.gather(root("alpha"), root("beta"))

        with patch("agent_trace.trace.get_client", return_value=fake_client):
            asyncio.run(exercise())
        self.ingest(collected)
        traces = self.api.get("/api/public/traces").json()
        self.assertEqual(len(traces), 2)
        for trace in traces:
            spans = self.api.get("/api/public/traces/" + trace["id"]).json()["spans"]
            self.assertEqual(len(spans), 3)
            root_span = next(s for s in spans if s["parent_span_id"] is None)
            self.assertTrue(all(s["parent_span_id"] == root_span["id"] for s in spans if s["type"] == "tool"))

    def test_runner_records_failed_case_and_continues(self):
        dataset = self.post("datasets", {"name": "runner", "items": [{"id": str(i), "input": {"n": i}, "expected": "ok"} for i in range(2)]})
        from agent_trace.experiment import run_experiment

        def send(url, **kwargs):
            return self.api.post(url.removeprefix("http://testserver"), json=kwargs["json"])

        def read(url, **kwargs):
            return self.api.get(url.removeprefix("http://testserver"))

        def task(value):
            if value["n"] == 0:
                raise ValueError("task failed")
            return "ok"

        with tempfile.TemporaryDirectory() as directory, patch("agent_trace.client.httpx.post", side_effect=send), patch("agent_trace.experiment.httpx.get", side_effect=read):
            client = TraceClient(host="http://testserver", outbox_path=Path(directory) / "runner.db", flush_interval=.01)
            try:
                with patch("agent_trace.trace.get_client", return_value=client), patch("agent_trace.experiment.get_client", return_value=client):
                    result = run_experiment(dataset["id"], "runner", "v1", task)
                self.assertEqual(result["summary"]["quality"], .5)
                self.assertEqual(result["summary"]["error_rate"], .5)
                self.assertEqual(len(result["results"]), 2)
            finally:
                client.shutdown()


class OutboxTest(unittest.TestCase):
    def test_restart_recovers_unacknowledged_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.db"
            with patch("agent_trace.client.httpx.post", side_effect=httpx_error()):
                client = TraceClient(outbox_path=path, flush_interval=.01)
                client.enqueue(events()[0])
                with self.assertRaises(TimeoutError):
                    client.flush(timeout=.1)
                client.shutdown()
            response = type("Response", (), {"status_code": 200, "raise_for_status": lambda self: None,
                                             "json": lambda self: {"accepted": 0, "duplicates": 1}})()
            with patch("agent_trace.client.httpx.post", return_value=response) as send:
                client = TraceClient(outbox_path=path, flush_interval=.01)
                client.flush(timeout=3)
                self.assertEqual(client.stats()["pending"], 0)
                self.assertEqual(send.call_args.kwargs["json"]["batch"][0]["id"], "trace-1-create")
                client.shutdown()

    def test_rejection_retained_in_dead_letter(self):
        with tempfile.TemporaryDirectory() as directory:
            response = type("Response", (), {"status_code": 422, "text": "invalid event"})()
            with patch("agent_trace.client.httpx.post", return_value=response):
                client = TraceClient(outbox_path=Path(directory) / "outbox.db", flush_interval=.01)
                client.enqueue(events()[0])
                with self.assertRaises(RuntimeError):
                    client.flush(timeout=3)
                self.assertEqual(client.stats()["dead_letter"], 1)
                client.shutdown()


def httpx_error():
    import httpx
    return httpx.ConnectError("simulated offline")


if __name__ == "__main__":
    unittest.main()
