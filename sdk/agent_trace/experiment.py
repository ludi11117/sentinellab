"""Run a user supplied callable against an immutable dataset and evaluate linked traces."""
import httpx

from .client import get_client
from .trace import trace


class _TaskFailure(Exception):
    """Distinguish task failure from SDK/outbox failure, which must abort the run."""


def run_experiment(dataset_id, name, revision, task):
    """task(input) -> str or {'answer': str}. Instrument task internals with @observe."""
    client = get_client()
    response = httpx.get(f"{client.host}/api/public/datasets/{dataset_id}", headers=client.headers, timeout=15)
    response.raise_for_status()
    dataset = response.json()
    observations = []
    for item in dataset["items"]:
        try:
            with trace(name, input=item["input"], metadata={"revision": revision, "dataset_id": dataset_id}) as root:
                try:
                    result = task(item["input"])
                except Exception as exc:
                    raise _TaskFailure(f"{type(exc).__name__}: {exc}") from exc
                root.update(output={"answer": result} if isinstance(result, str) else result)
        except _TaskFailure:
            # The trace context records the exception; continue evaluating other cases.
            pass
        observations.append({"item_id": item["id"], "trace_id": root.trace_id})
    client.flush(timeout=60)
    response = httpx.post(f"{client.host}/api/public/experiments", json={"dataset_id": dataset_id,
                          "name": name, "revision": revision, "observations": observations}, headers=client.headers, timeout=30)
    response.raise_for_status()
    return response.json()
