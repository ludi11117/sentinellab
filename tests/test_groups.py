import unittest
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.evaluation import compare, score
from app.routers.evaluations import GatePolicy


def result(identifier, value, group):
    return {"item_id": identifier, "trace_id": "trace-" + identifier, "score": value, "group": group}


def experiment(results):
    return SimpleNamespace(results=results, summary={"quality": sum(r["score"] for r in results)/len(results),
                           "mean_cost": 0, "p95_latency_ms": 1, "error_rate": 0, "samples": len(results)})


class GroupTest(unittest.TestCase):
    def test_rare_category_failure_not_hidden_by_overall_quality(self):
        baseline = experiment([result(str(i), 1, "common" if i < 20 else "critical") for i in range(21)])
        candidate = experiment([result(str(i), int(i < 20), "common" if i < 20 else "critical") for i in range(21)])
        report = compare(baseline, candidate, GatePolicy().model_dump())
        self.assertGreater(report["candidate"]["quality"], .9)
        self.assertEqual(report["paired_bootstrap"]["groups"], 2)
        check = next(c for c in report["checks"] if c["name"] == "worst_group_quality_delta")
        self.assertFalse(check["passed"])
        self.assertEqual(report["decision"], "block")

    def test_many_paraphrases_are_one_independent_group(self):
        baseline = experiment([result(str(i), 1, "one-topic") for i in range(30)])
        candidate = experiment([result(str(i), 1, "one-topic") for i in range(30)])
        report = compare(baseline, candidate, GatePolicy().model_dump())
        self.assertEqual(report["decision"], "block")
        self.assertEqual(report["paired_bootstrap"]["groups"], 1)

    def test_answer_correct_but_wrong_citation_fails(self):
        item = {"id": "a", "input": {}, "expected": "24V", "match": "contains", "required_tools": [], "forbidden_tools": [], "expected_citation": "right-document"}
        trace = SimpleNamespace(id="trace", output={"answer": "24V", "citations": ["wrong-document"]}, status="success", latency_ms=1, total_cost=0, total_tokens=0)
        scored = score(item, trace, [])
        self.assertTrue(scored["checks"]["answer"])
        self.assertFalse(scored["checks"]["citation"])
        self.assertEqual(scored["score"], 0)

    def test_holdout_policy_rejects_development_data(self):
        baseline = experiment([{**result(str(i), 1, str(i)), "split": "development"} for i in range(12)])
        candidate = experiment([{**result(str(i), 1, str(i)), "split": "development"} for i in range(12)])
        report = compare(baseline, candidate, GatePolicy(require_holdout=True).model_dump())
        self.assertEqual(report["decision"], "block")
        self.assertFalse(next(c for c in report["checks"] if c["name"] == "holdout_only")["passed"])

    def test_unknown_provider_cost_cannot_pass_cost_gate(self):
        baseline = experiment([result(str(i), 1, str(i)) for i in range(12)])
        candidate = experiment([{**result(str(i), 1, str(i)), "cost_known": False} for i in range(12)])
        report = compare(baseline, candidate, GatePolicy().model_dump())
        self.assertFalse(next(c for c in report["checks"] if c["name"] == "mean_cost")["passed"])


if __name__ == "__main__":
    unittest.main()
