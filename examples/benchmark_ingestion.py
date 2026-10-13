"""Measured local HTTP smoke benchmark, not a production capacity claim."""
import argparse
import concurrent.futures
import json
import platform
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

def run(host, requests, concurrency, output):
    def send(index):
        trace, span = str(uuid.uuid4()), str(uuid.uuid4())
        event = lambda kind, body: {"id": str(uuid.uuid4()), "type": kind, "body": body}
        batch = [event("trace-create", {"id": trace, "name": "ingestion-benchmark", "metadata": {"synthetic": True}}),
                 event("span-create", {"id": span, "trace_id": trace, "name": "task", "start_time": "2026-10-13T00:00:00Z"}),
                 event("span-update", {"id": span, "status": "success", "end_time": "2026-10-13T00:00:01Z", "cost": .001})]
        start = time.perf_counter()
        response = client.post("/api/public/ingestion", json={"batch": batch})
        latency = (time.perf_counter() - start) * 1000
        return {"status": response.status_code, "latency_ms": latency,
                "accepted": response.json().get("accepted", 0) if response.is_success else 0}

    with httpx.Client(base_url=host, timeout=30, limits=httpx.Limits(max_connections=concurrency)) as client:
        client.get("/health").raise_for_status()
        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(send, range(requests)))
        elapsed = time.perf_counter() - start
    latencies = sorted(r["latency_ms"] for r in results)
    report = {"measured": True, "timestamp": datetime.now(timezone.utc).isoformat(), "host": host,
              "platform": platform.platform(), "python": platform.python_version(), "database": "SQLite (isolated local benchmark database)",
              "requests": requests, "concurrency": concurrency, "events_per_request": 3,
              "elapsed_seconds": elapsed, "requests_per_second": requests / elapsed,
              "accepted_events": sum(r["accepted"] for r in results), "http_errors": sum(r["status"] != 200 for r in results),
              "http_latency_ms": {"p50": latencies[len(latencies)//2], "p95": latencies[min(len(latencies)-1, int(len(latencies)*.95))], "max": max(latencies)},
              "limitations": "Single host, small HTTP batch, SQLite, no model inference. No production throughput or distributed reliability claim."}
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://127.0.0.1:8001")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", default="D:/agent-runtime/artifacts/benchmark.json")
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        parser.error("requests and concurrency must be positive")
    run(args.host, args.requests, args.concurrency, args.output)
