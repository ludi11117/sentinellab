"""Deterministic SYNTHETIC fixtures, not LLM output or measured performance."""
import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx


def seed(host, output=None):
    client = httpx.Client(base_url=host, timeout=30)

    def post(path, body):
        response = client.post("/api/public/" + path, json=body)
        response.raise_for_status()
        return response.json()

    items = [{"id": f"case-{i:02d}", "input": {"question": f"订单 #{i:02d} 未发货，退款需要几天？"},
              "expected": "1-3个工作日", "match": "contains", "required_tools": ["policy.lookup"],
              "forbidden_tools": ["payment.transfer"]} for i in range(20)]
    dataset = post("datasets", {"name": "退款客服 · synthetic-fixture-v1", "items": items})
    experiments = {}
    # All durations, costs, tokens and answers below are intentionally synthetic.
    for revision, bad_cases, duration, cost in [("baseline-v1", 0, 1800, .012), ("optimized-v2", 0, 900, .005),
                                                ("regressed-v3", 6, 800, .004), ("expensive-v4", 0, 4200, .045)]:
        observations = []
        for i, item in enumerate(items):
            tid, root, tool, gen = [str(uuid.uuid4()) for _ in range(4)]
            start = datetime.now(timezone.utc)
            stamp = lambda ms: (start + timedelta(milliseconds=ms)).isoformat()
            event = lambda kind, body: {"id": str(uuid.uuid4()), "type": kind, "body": body}
            answer = "立即到账" if i < bad_cases else "未发货订单退款通常需要1-3个工作日。"
            events = [event("trace-create", {"id": tid, "name": "refund-support", "input": item["input"],
                                           "metadata": {"synthetic": True, "revision": revision}, "tags": ["synthetic", revision]}),
                      event("span-create", {"id": root, "trace_id": tid, "name": "refund-support", "start_time": stamp(0)}),
                      event("span-create", {"id": tool, "trace_id": tid, "parent_span_id": root, "name": "policy.lookup", "type": "tool", "start_time": stamp(20)}),
                      event("span-update", {"id": tool, "status": "success", "end_time": stamp(200), "output": {"policy": "1-3个工作日"}}),
                      event("span-create", {"id": gen, "trace_id": tid, "parent_span_id": root, "name": "answer", "type": "generation", "model": "synthetic-fixture", "start_time": stamp(200)}),
                      event("span-update", {"id": gen, "status": "success", "end_time": stamp(duration), "output": {"answer": answer}, "prompt_tokens": 400, "completion_tokens": 60, "cost": cost}),
                      event("span-update", {"id": root, "status": "success", "end_time": stamp(duration), "output": {"answer": answer}})]
            post("ingestion", {"batch": events})
            observations.append({"item_id": item["id"], "trace_id": tid})
        experiments[revision] = post("experiments", {"dataset_id": dataset["id"], "name": revision,
                                                    "revision": revision, "observations": observations})
    reports = []
    for revision in ["optimized-v2", "regressed-v3", "expensive-v4"]:
        report = post("gates", {"baseline_id": experiments["baseline-v1"]["id"], "candidate_id": experiments[revision]["id"]})
        reports.append({"revision": revision, "report": report})
        print(f"{revision}: {report['decision']} | quality={report['candidate']['quality']:.0%}")
    artifact = {"synthetic": True, "dataset_id": dataset["id"], "reports": reports}
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    client.close()
    return artifact


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="D:/agent-runtime/artifacts/demo-report.json")
    args = parser.parse_args()
    seed(args.host, args.output)
