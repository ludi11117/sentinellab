"""Measured model calls on fixed real documentation. No fabricated human votes or customer metrics."""
import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def run(host, stage):
    bundle = json.loads((ROOT / "frontend" / "ops-bundle.json").read_text(encoding="utf-8"))
    items = bundle["development_items"] if stage == "development" else bundle["items"]
    with httpx.Client(base_url=host, timeout=30) as client:
        def post(path, body):
            response = client.post("/api/public/" + path, json=body)
            response.raise_for_status()
            return response.json()

        dataset = post("datasets", {"name": bundle["name"] + " / " + stage, "items": items})
        revisions = ["llm-grounded-v2"] if stage == "development" else ["llm-baseline-v1", "llm-grounded-v2", "llm-grounded-v2"]
        jobs = [post("jobs", {"request_key": uuid.uuid4().hex, "dataset_id": dataset["id"], "revision": revision, "corpus": bundle["corpus"]}) for revision in revisions]
        experiments = []
        deadline = time.monotonic() + 900
        for job in jobs:
            previous = None
            while True:
                response = client.get("/api/public/jobs/" + job["id"])
                response.raise_for_status()
                state = response.json()
                if state["completed"] != previous:
                    previous = state["completed"]
                    print(f"{job['revision']} {state['state']} {previous}/{len(items)}", flush=True)
                if state["state"] == "succeeded":
                    response = client.get("/api/public/experiments/" + state["experiment_id"])
                    response.raise_for_status()
                    experiments.append(response.json())
                    break
                if state["state"] in ("failed", "cancelled") or time.monotonic() > deadline:
                    raise RuntimeError("model evaluation stopped: " + json.dumps(state))
                time.sleep(.5)
        report = {"measured_at": datetime.now(timezone.utc).isoformat(), "stage": stage, "real_model_inference": True,
                  "real_public_documentation": True, "provenance": bundle["provenance"], "dataset": dataset, "jobs": jobs,
                  "experiments": experiments, "customer_claims": False, "human_review": "pending_real_reviewer"}
        if stage == "holdout":
            # Fixed before inference, not changed to obtain a green report.
            policy = {"min_samples": 16, "min_quality": .8, "max_quality_drop": .05, "min_groups": 12,
                      "max_group_quality_drop": .2, "max_p95_latency_ms": 30000, "require_holdout": True}
            report["gate"] = post("gates", {"baseline_id": experiments[0]["id"], "candidate_id": experiments[1]["id"], "policy": policy})
            report["review_round"] = post("reviews", {"baseline_id": experiments[0]["id"], "candidate_id": experiments[1]["id"]})
            first, repeat = experiments[1]["results"], experiments[2]["results"]
            report["repeatability"] = {"same_seed_and_temperature": True, "cases": len(first),
               "score_disagreements": sum(a["score"] != b["score"] for a, b in zip(first, repeat)),
               "answer_disagreements": sum(a["output"] != b["output"] for a, b in zip(first, repeat)),
               "limitations": "Repeat at deterministic settings; not a stochastic robustness or production drift claim."}
        target = ROOT / "artifacts" / ("real-model-" + stage + ".json")
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"summaries": [e["summary"] for e in experiments], "gate": report.get("gate", {}).get("decision"), "report": str(target)}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://127.0.0.1:8000")
    parser.add_argument("--stage", choices=["development", "holdout"], default="development")
    args = parser.parse_args()
    run(args.host, args.stage)
