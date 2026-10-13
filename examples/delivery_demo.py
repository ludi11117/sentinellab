"""Run the curated fictional quotation workflow through real asynchronous workers."""
import argparse
import json
import time
import uuid
from pathlib import Path

import httpx
from quotation_fixture import fixture

ROOT = Path(__file__).resolve().parents[1]


def main(host, api_key=None):
    headers = {"Authorization": "Bearer " + api_key} if api_key else {}
    with httpx.Client(base_url=host, headers=headers, timeout=30) as client:
        def post(path, body):
            response = client.post("/api/public/" + path, json=body)
            response.raise_for_status()
            return response.json()

        corpus, items = fixture()
        dataset = post("datasets", {"name": "Quotation evidence · fictional customer · 12 groups / 36 cases", "items": items})
        jobs = [post("jobs", {"request_key": uuid.uuid4().hex, "dataset_id": dataset["id"], "revision": revision, "corpus": corpus})
                for revision in ["keyword-v1", "retrieval-v2", "unsafe-v3"]]
        deadline = time.monotonic() + 60
        experiments = {}
        for job in jobs:
            while True:
                response = client.get("/api/public/jobs/" + job["id"])
                response.raise_for_status()
                state = response.json()
                if state["state"] == "succeeded":
                    experiments[job["revision"]] = state["experiment_id"]
                    print(f"{job['revision']} completed {state['completed']}/{state['total']} real cases", flush=True)
                    break
                if state["state"] in ("failed", "cancelled") or time.monotonic() > deadline:
                    raise RuntimeError("worker did not complete: " + json.dumps(state))
                time.sleep(.2)
        good = post("gates", {"baseline_id": experiments["keyword-v1"], "candidate_id": experiments["retrieval-v2"]})
        bad = post("gates", {"baseline_id": experiments["retrieval-v2"], "candidate_id": experiments["unsafe-v3"]})
        delivery = post("deliveries", {"gate_id": good["id"], "customer": "Fictional equipment distributor", "workflow": "Prepare evidence-backed replacement quotation; no autonomous orders or payment"})
        report = {"fixture_customer": True, "synthetic_traces": False, "llm_calls": 0, "dataset_id": dataset["id"],
                  "jobs": jobs, "experiments": experiments, "good_gate": good, "unsafe_gate": bad, "delivery": delivery}
        target = ROOT / "artifacts" / "delivery-demo-report.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Retrieval candidate: {good['candidate']['quality']:.1%}, gate={good['decision']}; unsafe revision gate={bad['decision']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://127.0.0.1:8000")
    parser.add_argument("--key", help="project key; prefer environment variable SENTINEL_PROJECT_KEY")
    args = parser.parse_args()
    import os
    main(args.host.rstrip("/"), args.key or os.getenv("SENTINEL_PROJECT_KEY"))
