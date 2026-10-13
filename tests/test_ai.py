import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_system as existing
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base, configure_sqlite
from app.evaluation import content_hash
from app.jobqueue import LeaseLost, cancel, claim
from app.model_provider import ModelAnswer, generate, profile
from app.models import Acceptance, Dataset, Experiment, GateReport, Job, ReviewVote
from app.worker import LeaseKeeper
from test_reliability import job_fixture


def response(answer, evidence_id="E0", abstain=False):
    raw = {"choices": [{"message": {"content": json.dumps({"answer": answer, "evidence_id": evidence_id, "abstain": abstain})}, "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 60, "completion_tokens": 20, "total_tokens": 80}, "id": "test-response"}
    return type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: raw})()


class ProviderTest(unittest.TestCase):
    def test_true_quote_tokens_and_no_expected_label_in_request(self):
        with patch("app.model_provider.httpx.post", return_value=response("24V DC")) as post:
            result = generate("What supply voltage?", [{"id": "voltage", "body": "24V DC only"}], True, profile())
        self.assertTrue(result["output"]["grounding"]["quote_valid"])
        self.assertEqual(result["usage"]["total_tokens"], 80)
        request = post.call_args.kwargs["json"]
        self.assertNotIn("expected", json.dumps(request))
        self.assertEqual(request["response_format"]["schema"]["properties"]["evidence_id"]["enum"], ["E0", "NONE"])

    def test_missing_evidence_fails_closed_and_keeps_raw_response(self):
        with patch("app.model_provider.httpx.post", return_value=response("220V AC", evidence_id="NONE")):
            result = generate("voltage", [{"id": "voltage", "body": "24V DC only"}], True, profile())
        self.assertTrue(result["output"]["abstain"])
        self.assertEqual(result["output"]["citations"], [])
        self.assertEqual(result["raw_output"]["answer"], "220V AC")

    def test_unknown_citation_fails_closed(self):
        with patch("app.model_provider.httpx.post", return_value=response("24V", evidence_id="not-in-context")):
            result = generate("voltage", [{"id": "voltage", "body": "24V DC"}], True, profile())
        self.assertTrue(result["output"]["grounding"]["rejected"])

    def test_frozen_profile_prevents_configuration_drift(self):
        modified = {**profile(), "seed": 999}
        with patch("app.model_provider.httpx.post") as post:
            with self.assertRaises(ValueError):
                generate("question", [], True, modified)
            post.assert_not_called()

    def test_boolean_string_is_not_a_valid_abstention(self):
        with self.assertRaises(ValueError):
            ModelAnswer.model_validate({"answer": "x", "quote": "x", "citation": "x", "abstain": "false"})

    def test_flag_formatter_preserves_raw_model_and_uses_only_selected_quote(self):
        hits = [{"id": "keys", "body": "--api-key-file FNAME: one key per line\n--api-key KEY: inline key"}]
        with patch("app.model_provider.httpx.post", return_value=response("Use a filename")) as post:
            value = generate("How to load keys from a file?", hits, True, profile("llm-grounded-v3"), True)
        self.assertEqual(value["raw_output"]["answer"], "Use a filename")
        self.assertEqual(value["output"]["answer_transform"]["appended_flags"], ["--api-key-file"])
        self.assertIn("--api-key-file", value["output"]["answer"])
        self.assertEqual(value["output"]["quote"], hits[0]["body"].splitlines()[0])
        self.assertNotIn("expected", json.dumps(post.call_args.kwargs["json"]))

    def test_flag_formatter_does_not_turn_abstention_into_an_answer(self):
        with patch("app.model_provider.httpx.post", return_value=response("invented", abstain=True)):
            value = generate("unknown", [{"id":"x", "body":"--api-key-file"}], True, profile("llm-grounded-v3"), True)
        self.assertEqual(value["output"]["answer"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(value["output"]["answer_transform"]["appended_flags"], [])

    def test_public_configuration_prompt_is_versioned_and_preserves_abstention(self):
        with patch("app.model_provider.httpx.post", return_value=response("real secrets are unknown", evidence_id="NONE", abstain=True)) as post:
            value = generate("What is our actual key?", [{"id":"x", "body":"--key KEY"}], True, profile("llm-grounded-v4"), True, True)
        self.assertTrue(value["output"]["abstain"])
        self.assertEqual(value["profile"]["prompt_version"], "evidence-selection-v4-public-configuration")
        self.assertIn("not a request for an actual secret", post.call_args.kwargs["json"]["messages"][0]["content"])

    def test_extractive_contract_excludes_model_paraphrase_and_keeps_original(self):
        hits=[{"id":"x", "body":"--limit N: maximum items (default: 50)"}]
        with patch("app.model_provider.httpx.post",return_value=response("wrong default 999")):
            result=generate("default?",hits,True,profile("llm-grounded-v5"),public_configuration=True,extractive_answer=True)
        self.assertEqual(result["output"]["answer"],hits[0]["body"])
        self.assertEqual(result["raw_output"]["answer"],"wrong default 999")
        self.assertEqual(result["output"]["answer_transform"]["original_answer"],"wrong default 999")
        self.assertNotIn("999",result["output"]["answer"])


class ReviewTest(unittest.TestCase):
    setUp = existing.SystemTest.setUp
    tearDown = existing.SystemTest.tearDown
    post = existing.SystemTest.post

    def fixtures(self, gate_pass=True, split="holdout"):
        with Session(self.engine, expire_on_commit=False) as db:
            dataset = Dataset(name="review", digest="fixed", items=[{"id": str(i), "input": {"question": "voltage"}} for i in range(2)])
            db.add(dataset)
            db.flush()
            results = [{"item_id": str(i), "trace_id": "t-"+str(i), "input": {"question": "voltage"}, "output": {"answer": "24V", "citations": ["doc"]},
                       "score": 1, "split": split, "group": str(i)} for i in range(2)]
            base = Experiment(dataset_id=dataset.id, name="hidden-base", revision="hidden-base", evaluator_version="v", results=results, summary={"quality": 1})
            cand = Experiment(dataset_id=dataset.id, name="hidden-candidate", revision="hidden-candidate", evaluator_version="v", results=results, summary={"quality": 1})
            db.add_all([base, cand])
            db.flush()
            gate = GateReport(baseline_id=base.id, candidate_id=cand.id, policy={}, report={"decision": "pass" if gate_pass else "block"})
            db.add(gate)
            db.commit()
            return base.id, cand.id, gate.id

    def test_blinded_tasks_do_not_expose_versions_or_scores(self):
        base, candidate, gate = self.fixtures()
        review = self.post("reviews", {"baseline_id": base, "candidate_id": candidate})
        tasks = self.api.get("/api/public/reviews/" + review["id"] + "/tasks").json()
        serialized = json.dumps(tasks)
        self.assertNotIn("hidden-base", serialized)
        self.assertNotIn(base, serialized)
        self.assertNotIn("trace_id", serialized)
        self.assertNotIn('"score"', serialized)

    def test_no_signoff_before_actual_votes_and_gate_pass(self):
        base, candidate, gate = self.fixtures()
        review = self.post("reviews", {"baseline_id": base, "candidate_id": candidate})
        body = {"gate_id": gate, "review_round_id": review["id"], "notes": "technical review only"}
        self.post("acceptances", body, 409)
        for i in range(2):
            self.post("reviews/" + review["id"] + "/votes", {"item_id": str(i), "reviewer_name": "TEST ONLY", "choice": "tie", "rationale": "Both correct in isolated test fixture"})
        accepted = self.post("acceptances", body)
        self.assertEqual(accepted["digest"], content_hash(accepted["record"]))
        self.assertEqual(accepted["record"]["review"]["reviewed_items"], 2)

    def test_vote_is_immutable_and_duplicate_rejected(self):
        base, candidate, gate = self.fixtures()
        review = self.post("reviews", {"baseline_id": base, "candidate_id": candidate})
        body = {"item_id": "0", "reviewer_name": "TEST ONLY", "choice": "neither", "rationale": "Neither is acceptable"}
        self.post("reviews/" + review["id"] + "/votes", body)
        self.post("reviews/" + review["id"] + "/votes", body, 409)

    def test_developer_self_test_never_completes_human_acceptance(self):
        base, candidate, gate = self.fixtures()
        review = self.post("reviews", {"baseline_id": base, "candidate_id": candidate})
        for i in range(2):
            body = {"item_id": str(i), "reviewer_name": "DEVELOPER TEST", "reviewer_kind": "developer_agent", "choice": "tie", "rationale": "Developer checked the example source"}
            self.post("reviews/" + review["id"] + "/votes", body)
            self.post("reviews/" + review["id"] + "/votes", body, 409)
        report = self.api.get("/api/public/reviews/" + review["id"] + "/summary").json()
        self.assertEqual(report["self_reviewed_items"], 2)
        self.assertEqual(report["reviewed_items"], 0)
        self.assertFalse(report["complete"])
        tasks = self.api.get("/api/public/reviews/" + review["id"] + "/tasks").json()["tasks"]
        self.assertTrue(all(t["self_reviewed"] and not t["voted"] for t in tasks))
        self.post("acceptances", {"gate_id": gate, "review_round_id": review["id"], "notes":"self-test cannot sign acceptance"}, 409)
        # The same key can still submit a distinct human-review record later.
        for i in range(2):
            self.post("reviews/" + review["id"] + "/votes", {"item_id": str(i), "reviewer_name":"HUMAN FIXTURE", "choice":"tie", "rationale":"Isolated human fixture record"})
        self.assertTrue(self.api.get("/api/public/reviews/" + review["id"] + "/summary").json()["complete"])

    def test_legacy_pages_redirect_to_one_workspace(self):
        for old, view in {"delivery":"setup", "releases":"compare", "evidence":"compare", "review":"review", "learn":"guide"}.items():
            response = self.api.get("/" + old + ".html", follow_redirects=False)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers["location"], "/#" + view)

    def test_human_neither_prevents_acceptance(self):
        base, candidate, gate = self.fixtures()
        review = self.post("reviews", {"baseline_id": base, "candidate_id": candidate})
        for i in range(2):
            self.post("reviews/" + review["id"] + "/votes", {"item_id": str(i), "reviewer_name": "TEST ONLY", "choice": "neither", "rationale": "Both insufficient"})
        self.post("acceptances", {"gate_id": gate, "review_round_id": review["id"], "notes": "cannot accept these answers"}, 409)

    def test_failed_automated_gate_not_bypassed_by_human_vote(self):
        base, candidate, gate = self.fixtures(False)
        review = self.post("reviews", {"baseline_id": base, "candidate_id": candidate})
        for i in range(2):
            self.post("reviews/" + review["id"] + "/votes", {"item_id": str(i), "reviewer_name": "TEST ONLY", "choice": "tie", "rationale": "both seem acceptable"})
        self.post("acceptances", {"gate_id": gate, "review_round_id": review["id"], "notes": "cannot bypass failed gate"}, 409)


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + (Path(self.directory.name)/"rpc.db").as_posix(), connect_args={"check_same_thread": False})
        configure_sqlite(self.engine)
        Base.metadata.create_all(self.engine)
        self.job = job_fixture(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.directory.cleanup()

    def test_background_heartbeat_preserves_lease_during_long_rpc(self):
        lease = claim(self.engine, "original", lease_ms=500)
        with LeaseKeeper(self.engine, lease, lease_ms=500, interval=.05):
            time.sleep(.7)
            self.assertIsNone(claim(self.engine, "competing-worker"))

    def test_cancel_during_rpc_discards_response(self):
        lease = claim(self.engine, "original")
        with self.assertRaises(LeaseLost):
            with LeaseKeeper(self.engine, lease, interval=.02):
                with Session(self.engine) as db:
                    cancel(db, self.job)
                time.sleep(.1)


if __name__ == "__main__":
    unittest.main()
