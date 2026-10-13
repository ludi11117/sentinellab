"""一个模拟的 Research Agent，用 SDK 上报全链路 trace。

运行前先启动后端：uvicorn app.main:app --port 8000
然后：python demo_agent.py
"""

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))

from agent_trace import get_client, get_current_span, observe, trace  # noqa: E402

QUESTION = "2024 年全球 AI Agent 市场规模与主要玩家有哪些？"


@observe(as_type="generation", model="doubao-pro-32k")
def plan(question: str) -> str:
    time.sleep(random.uniform(0.2, 0.5))
    get_current_span().update(
        usage={"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165},
        cost=0.0012,
    )
    return f"计划：先搜索市场数据，再搜索主要玩家，最后汇总。问题={question}"


@observe(as_type="tool")
def search(query: str) -> str:
    time.sleep(random.uniform(0.1, 0.3))
    return f"关于「{query}」的 3 条搜索结果"


@observe(as_type="tool")
def fetch_page(url: str) -> str:
    time.sleep(random.uniform(0.05, 0.2))
    if random.random() < 0.25:
        raise RuntimeError(f"抓取超时: {url}")
    return f"{url} 的正文内容"


@observe(as_type="generation", model="doubao-pro-32k")
def summarize(notes: str) -> str:
    time.sleep(random.uniform(0.3, 0.6))
    get_current_span().update(
        usage={"prompt_tokens": 820, "completion_tokens": 160, "total_tokens": 980},
        cost=0.0061,
    )
    return "汇总完成：市场规模约 XXX 亿美元，主要玩家包括 A、B、C。"


def run_agent(question: str) -> str:
    plan_result = plan(question)

    with trace("gather-evidence", input={"queries": 3}):
        for i in range(3):
            search(f"sub-query-{i}")
            try:
                fetch_page(f"https://example.com/page-{i}")
            except RuntimeError:
                pass

    return summarize(plan_result)


if __name__ == "__main__":
    with trace(
        "research-agent",
        user_id="u_1001",
        session_id="sess_demo_001",
        input={"question": QUESTION},
        metadata={"env": "demo", "version": "0.1.0"},
    ) as root:
        answer = run_agent(QUESTION)
        root.update(output={"answer": answer})

    get_client().flush()
    print("trace 已上报，打开 http://localhost:8000 查看")
