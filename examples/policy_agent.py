"""Executable local retrieval agent. No LLM calls; zero provider cost, measured timing."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk"))

POLICIES = [
    ("未发货退款", ["未发货", "没发货", "未寄出", "还没寄"], "1-3个工作日"),
    ("已发货退货", ["已发货", "寄出了", "已经发出", "收到货"], "签收后7天内申请"),
    ("运费", ["运费", "邮费", "快递费", "配送费"], "质量问题由商家承担运费"),
    ("发票", ["发票", "开票", "票据", "报销凭证"], "订单完成后30天内申请电子发票"),
    ("地址", ["地址", "收货地", "送货地", "寄到哪里"], "未发货订单可以修改收货地址"),
]


def main(host, output):
    os.environ["AGENT_TRACE_HOST"] = host
    os.environ["AGENT_TRACE_OUTBOX"] = str(ROOT / "artifacts" / "policy-agent-outbox.db")
    import httpx
    from agent_trace import observe, get_current_span
    from agent_trace.client import get_client
    from agent_trace.experiment import run_experiment

    @observe(name="policy.lookup", as_type="tool")
    def lookup(question, robust):
        for topic, synonyms, answer in POLICIES:
            if any(word in question for word in (synonyms if robust else [synonyms[0]])):
                return {"topic": topic, "answer": answer}
        return {"topic": None, "answer": "请联系人工客服"}

    def task(robust):
        @observe(name="support-agent", as_type="chain")
        def run(value):
            result = lookup(value["question"], robust)
            get_current_span().update(metadata={"executor": "local-retrieval", "llm_calls": 0})
            return {"answer": result["answer"]}
        return run

    items = []
    for topic, synonyms, answer in POLICIES:
        for term in synonyms:
            items.append({"id": f"case-{len(items):02d}", "input": {"question": f"请问{term}的处理规则是什么？"},
                          "expected": answer, "required_tools": ["policy.lookup"], "forbidden_tools": ["payment.transfer"]})
    response = httpx.post(host + "/api/public/datasets", json={"name": "Local policy retrieval · 20 curated cases", "items": items}, timeout=15)
    response.raise_for_status()
    dataset = response.json()
    baseline = run_experiment(dataset["id"], "policy-support", "local-exact-route", task(False))
    candidate = run_experiment(dataset["id"], "policy-support", "local-synonym-route", task(True))
    response = httpx.post(host + "/api/public/gates", json={"baseline_id": baseline["id"], "candidate_id": candidate["id"]}, timeout=30)
    response.raise_for_status()
    report = response.json()
    artifact = {"synthetic": False, "executor": "local-retrieval", "llm_calls": 0,
                "dataset": dataset, "baseline": baseline, "candidate": candidate, "gate": report,
                "limitations": "Curated toy policy corpus; no LLM or external business system. Timings are measured locally; cost is zero because no paid model is called."}
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Actual SDK execution: {baseline['summary']['quality']:.0%} -> {candidate['summary']['quality']:.0%}; gate={report['decision']}")
    get_client().shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default=str(ROOT / "artifacts" / "policy-agent-report.json"))
    args = parser.parse_args()
    main(args.host.rstrip("/"), args.output)
