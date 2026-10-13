"""Durable FIFO outbox: commit locally, retry remotely, delete only after acknowledgement."""
from __future__ import annotations

import atexit
import json
import hashlib
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


class TraceClient:
    def __init__(self, host=None, public_key=None, secret_key=None, flush_interval=1.0,
                 max_batch_size=100, outbox_path=None, max_pending=10000, api_key=None):
        self.host = (host or os.getenv("AGENT_TRACE_HOST", "http://localhost:8000")).rstrip("/")
        self.public_key = public_key or os.getenv("AGENT_TRACE_PUBLIC_KEY")
        self.secret_key = secret_key or os.getenv("AGENT_TRACE_SECRET_KEY")
        self.api_key = api_key or os.getenv("SENTINEL_PROJECT_KEY")
        # Never replay another project's pending events under this key.
        self._partition = self.host + ("#" + hashlib.sha256(self.api_key.encode()).hexdigest() if self.api_key else "")
        self.flush_interval = flush_interval
        self.max_batch_size = max_batch_size
        self.max_pending = max_pending
        if max_batch_size < 1 or flush_interval < 0 or max_pending < 1:
            raise ValueError("invalid outbox limits")
        self.outbox_path = Path(outbox_path or os.getenv("AGENT_TRACE_OUTBOX", "D:/CodexWork/cache/sentinellab/outbox.db"))
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.outbox_path), check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT)")
        self._db.commit()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.last_error = None
        self._closed = False
        self._worker = threading.Thread(target=self._run, daemon=True, name="sentinellab-outbox")
        self._worker.start()
        atexit.register(self.shutdown)

    def enqueue(self, event: dict[str, Any]) -> None:
        event = {**event, "id": event.get("id") or str(uuid.uuid4())}
        with self._lock:
            if self._closed:
                raise RuntimeError("client has been shut down")
            count = self._db.execute("SELECT COUNT(*) FROM outbox WHERE host=?", (self._partition,)).fetchone()[0]
            if count >= self.max_pending:
                raise BufferError("durable outbox is full; inspect delivery errors")
            self._db.execute("INSERT INTO outbox(host,payload) VALUES (?,?)", (self._partition, json.dumps(event, default=str)))
            self._db.commit()
        self._wake.set()

    def stats(self):
        with self._lock:
            rows = self._db.execute("SELECT state,COUNT(*) FROM outbox WHERE host=? GROUP BY state", (self._partition,)).fetchall()
        return {"pending": 0, "dead_letter": 0, **dict(rows), "last_error": self.last_error}

    def _run(self):
        retries = 0
        while not self._stop.is_set():
            self._wake.wait(max(self.flush_interval, .05))
            self._wake.clear()
            with self._lock:
                rows = self._db.execute("SELECT seq,payload FROM outbox WHERE host=? AND state='pending' ORDER BY seq LIMIT ?", (self._partition, self.max_batch_size)).fetchall()
            if not rows:
                continue
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            elif self.public_key and self.secret_key:
                headers["Authorization"] = f"Bearer {self.public_key}:{self.secret_key}"
            try:
                response = httpx.post(f"{self.host}/api/public/ingestion",
                                      json={"batch": [json.loads(row[1]) for row in rows]}, headers=headers, timeout=5)
                if 400 <= response.status_code < 500 and response.status_code not in (408, 409, 429):
                    self.last_error = f"HTTP {response.status_code}: {response.text[:500]}"
                    with self._lock:
                        self._db.executemany("UPDATE outbox SET state='dead_letter',error=? WHERE seq=?", [(self.last_error, row[0]) for row in rows])
                        self._db.commit()
                    continue
                response.raise_for_status()
                ack = response.json()
                if ack.get("accepted", 0) + ack.get("duplicates", 0) != len(rows):
                    raise ValueError("incomplete ingestion acknowledgement")
                with self._lock:
                    self._db.executemany("DELETE FROM outbox WHERE seq=?", [(row[0],) for row in rows])
                    self._db.commit()
                self.last_error = None
                retries = 0
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                retries += 1
                self._stop.wait(min(.25 * 2 ** min(retries, 5), 8))

    def flush(self, timeout=10):
        """Timeout leaves data on disk. Permanent rejections are surfaced, never acknowledged."""
        self._wake.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            stats = self.stats()
            if not stats["pending"]:
                if stats["dead_letter"]:
                    raise RuntimeError(f"{stats['dead_letter']} events rejected; inspect outbox.db")
                return
            time.sleep(.02)
        raise TimeoutError(f"outbox still pending: {self.stats()}")

    def shutdown(self):
        if self._closed:
            return
        try:
            self.flush(timeout=2)
        except (TimeoutError, RuntimeError):
            pass
        self._stop.set()
        self._wake.set()
        self._worker.join(timeout=6)
        if not self._worker.is_alive():
            with self._lock:
                self._closed = True
                self._db.close()
        atexit.unregister(self.shutdown)

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


_client = None
_lock = threading.Lock()


def get_client():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = TraceClient()
    return _client
