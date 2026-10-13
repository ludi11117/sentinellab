"""CI helper: save the audit report, then exit 0 on pass or 1 on block."""
import argparse
import json
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("baseline")
parser.add_argument("candidate")
parser.add_argument("--host", default="http://127.0.0.1:8000")
parser.add_argument("--policy", help="JSON policy file")
parser.add_argument("--output", default="D:/agent-runtime/artifacts/gate-report.json")
args = parser.parse_args()
payload = {"baseline_id": args.baseline, "candidate_id": args.candidate}
if args.policy:
    payload["policy"] = json.loads(Path(args.policy).read_text(encoding="utf-8"))
response = httpx.post(args.host.rstrip("/") + "/api/public/gates", json=payload, timeout=30)
response.raise_for_status()
report = response.json()
target = Path(args.output)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Release gate: {report['decision']}; audit report: {target}")
raise SystemExit(0 if report["decision"] == "pass" else 1)
