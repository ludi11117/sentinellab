"""Versioned deterministic evaluators and paired release gates; no model judge involved."""
import hashlib
import json
import math
import random
import unicodedata

EVALUATOR_VERSION = "evidence-v3"


def content_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def normalize(value):
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def percentile(values, q):
    values = sorted(values)
    position = (len(values) - 1) * q
    lo = math.floor(position)
    hi = math.ceil(position)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def score(item, trace, spans):
    output = trace.output
    answer = output.get("answer", "") if isinstance(output, dict) else output
    expected = item["expected"]
    if item["match"] == "exact":
        correct = normalize(answer) == normalize(expected)
    else:
        correct = normalize(expected) in normalize(answer)
    tools = {s.name for s in spans if s.type == "tool" and s.status == "success"}
    checks = {
        "answer": correct,
        "required_tools": set(item["required_tools"]).issubset(tools),
        "forbidden_tools": not bool(set(item["forbidden_tools"]) & {s.name for s in spans if s.type == "tool"}),
        "execution": trace.status == "success",
    }
    if item.get("expected_citation"):
        checks["citation"] = isinstance(output, dict) and item["expected_citation"] in output.get("citations", [])
    if item.get("must_abstain"):
        checks["abstention"] = isinstance(output, dict) and output.get("abstain") is True and not output.get("citations")
    if item.get("require_quote") and not item.get("must_abstain"):
        checks["verbatim_evidence"] = isinstance(output, dict) and output.get("grounding", {}).get("quote_valid") is True
    return {
        "item_id": item["id"], "trace_id": trace.id,
        "input": item["input"], "expected": expected, "output": output,
        "checks": checks, "score": int(all(checks.values())),
        "latency_ms": trace.latency_ms, "cost": float(trace.total_cost),
        "tokens": trace.total_tokens,
        "group": item.get("group") or item["id"],
        "split": item.get("split", "development"),
        "cost_known": not (trace.meta or {}).get("cost_basis") == "provider_cost_not_reconciled" if hasattr(trace, "meta") else True,
    }


def summarize(results):
    return {
        "samples": len(results),
        "quality": sum(r["score"] for r in results) / len(results),
        "mean_cost": sum(r["cost"] for r in results) / len(results),
        "p95_latency_ms": percentile([r["latency_ms"] for r in results], .95),
        "error_rate": sum(not r["checks"]["execution"] for r in results) / len(results),
    }


def compare(baseline, candidate, policy):
    base = {r["item_id"]: r for r in baseline.results}
    pairs = [{"item_id": r["item_id"], "baseline_trace_id": base[r["item_id"]]["trace_id"],
              "candidate_trace_id": r["trace_id"], "baseline_score": base[r["item_id"]]["score"],
              "candidate_score": r["score"], "delta": r["score"] - base[r["item_id"]]["score"]}
             for r in candidate.results]
    groups = {}
    for pair in pairs:
        group = base[pair["item_id"]].get("group") or pair["item_id"]
        groups.setdefault(group, []).append(pair["delta"])
        pair["group"] = group
    rng = random.Random(42)
    boot = []
    for _ in range(2000):
        selected = rng.choices(list(groups), k=len(groups))
        sampled = [delta for group in selected for delta in groups[group]]
        boot.append(sum(sampled) / len(sampled))
    lower = percentile(boot, .05)
    b, c = baseline.summary, candidate.summary
    checks = [
        {"name": "sample_size", "actual": len(pairs), "limit": policy["min_samples"], "passed": len(pairs) >= policy["min_samples"]},
        {"name": "quality_floor", "actual": c["quality"], "limit": policy["min_quality"], "passed": c["quality"] >= policy["min_quality"]},
        {"name": "paired_quality_lower_bound", "actual": lower, "limit": -policy["max_quality_drop"], "passed": lower >= -policy["max_quality_drop"]},
        {"name": "mean_cost", "actual": c["mean_cost"], "limit": policy["max_mean_cost"],
         "passed": c["mean_cost"] <= policy["max_mean_cost"] and all(r.get("cost_known", True) for r in candidate.results)},
        {"name": "p95_latency_ms", "actual": c["p95_latency_ms"], "limit": policy["max_p95_latency_ms"], "passed": c["p95_latency_ms"] <= policy["max_p95_latency_ms"]},
        {"name": "error_rate", "actual": c["error_rate"], "limit": policy["max_error_rate"], "passed": c["error_rate"] <= policy["max_error_rate"]},
        {"name": "independent_groups", "actual": len(groups), "limit": policy.get("min_groups", 2), "passed": len(groups) >= policy.get("min_groups", 2)},
        {"name": "worst_group_quality_delta", "actual": min(sum(v)/len(v) for v in groups.values()), "limit": -policy.get("max_group_quality_drop", .2),
         "passed": min(sum(v)/len(v) for v in groups.values()) >= -policy.get("max_group_quality_drop", .2)},
    ]
    if policy.get("require_holdout"):
        checks.append({"name": "holdout_only", "actual": int(all(r.get("split") == "holdout" for r in candidate.results + baseline.results)),
                       "limit": 1, "passed": all(r.get("split") == "holdout" for r in candidate.results + baseline.results)})
    return {"decision": "pass" if all(c["passed"] for c in checks) else "block", "checks": checks,
            "gate_version": "cluster-bootstrap-v2",
            "baseline": b, "candidate": c, "quality_delta": c["quality"] - b["quality"],
            "paired_bootstrap": {"lower_95_one_sided": lower, "resamples": 2000, "seed": 42, "unit": "group", "groups": len(groups)},
            "group_deltas": {k: sum(v)/len(v) for k, v in groups.items()},
            "pairs": pairs,
            "limitations": "Checks apply only to this fixed dataset. Bootstrap assumes independent cases; small or unrepresentative sets cannot establish production safety."}
