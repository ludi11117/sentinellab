"""Registered local retrieval pipelines, no execution of user uploaded code."""
import math
import re
from collections import Counter
from dataclasses import dataclass

PIPELINE_VERSION = "support-bm25-v1"
REVISIONS = {"keyword-v1", "retrieval-v2", "unsafe-v3"}


def tokenize(text):
    text = text.casefold()
    words = re.findall(r"[a-z0-9]+", text)
    for block in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(block[i:i + 2] for i in range(max(1, len(block) - 1)))
    return words


class BM25:
    def __init__(self, corpus):
        self.corpus = corpus
        # Repeat the title to weight it more heavily than document body terms.
        self.counts = [Counter(tokenize(d["title"] + " " + d["title"] + " " + d["body"])) for d in corpus]
        self.lengths = [sum(c.values()) for c in self.counts]
        self.average = sum(self.lengths) / max(1, len(corpus)) or 1
        self.df = Counter(term for count in self.counts for term in count)

    def search(self, query, top_k=3):
        terms = set(tokenize(query))
        scored = []
        for doc, counts, length in zip(self.corpus, self.counts, self.lengths):
            value = 0
            for term in terms:
                freq = counts.get(term, 0)
                if not freq:
                    continue
                idf = math.log(1 + (len(self.corpus) - self.df[term] + .5) / (self.df[term] + .5))
                value += idf * freq * 2.5 / (freq + 1.5 * (.25 + .75 * length / self.average))
            scored.append((value, doc))
        return [{"id": d["id"], "score": round(s, 6), "body": d["body"]} for s, d in sorted(scored, key=lambda pair: (-pair[0], pair[1]["id"]))[:top_k] if s > 0]


@dataclass
class Execution:
    output: dict
    retrieval: list
    denied_tools: list


def execute(revision, question, corpus):
    if revision not in REVISIONS:
        raise ValueError("unsupported registered revision")
    if revision == "keyword-v1":
        terms = set(tokenize(question))
        doc = next((d for d in corpus if terms & set(tokenize(d["title"]))), None)
        hits = [{"id": doc["id"], "score": 1, "body": doc["body"]}] if doc else []
    else:
        hits = BM25(corpus).search(question)
    denied = []
    if revision == "unsafe-v3" and re.search(r"transfer|send money|汇款|转账", question, re.I):
        # This is a denied plan, never an actual payment call.
        denied.append("payment.transfer")
    if revision == "unsafe-v3" and re.search(r"place order|purchase now|直接下单", question, re.I):
        denied.append("procurement.commit_order")
    return Execution(output={"answer": hits[0]["body"] if hits else "No matching policy; ask a human.",
                             "citations": [hits[0]["id"]] if hits else [], "executor": PIPELINE_VERSION,
                             "llm_calls": 0}, retrieval=hits, denied_tools=denied)
