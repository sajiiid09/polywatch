"""Background jobs for the console.

Anything the console can start -- following a new wallet, running a backtest, sweeping the
leaderboard -- takes minutes, not milliseconds. Running one inside the HTTP handler would hold
the request open long past any browser's patience, so each is handed to a thread and the page
polls for its progress.

Two rules hold this together and both come from SQLite rather than from taste:

  * a connection belongs to the thread that opened it, so every job opens its own and never
    borrows the server's;
  * one writer at a time, so jobs are serialised through a lock rather than run concurrently.
    Concurrency here would buy nothing anyway -- the work is rate-limited network calls, and
    the rate limiter is global.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field


@dataclass
class Job:
    id: str
    kind: str
    label: str
    status: str = "queued"          # queued | running | done | failed
    started_at: float = 0.0
    finished_at: float = 0.0
    lines: list = field(default_factory=list)
    result: dict | None = None
    error: str | None = None

    def as_dict(self, tail: int = 200) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "lines": self.lines[-tail:], "result": self.result, "error": self.error,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1)
            if self.started_at else 0.0,
        }


class JobRunner:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()      # guards the registry
        self._write_lock = threading.Lock()  # serialises SQLite writers

    def submit(self, kind: str, label: str, fn) -> Job:
        """`fn(con, log)` runs on a worker thread with its own database connection."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label)
        with self._lock:
            self.jobs[job.id] = job
            self._order.append(job.id)
            # The console is a local tool, not a service; a few dozen jobs of history is all
            # anyone reads, and unbounded growth is the only way this leaks memory.
            while len(self._order) > 50:
                self.jobs.pop(self._order.pop(0), None)
        threading.Thread(target=self._run, args=(job, fn), daemon=True).start()
        return job

    def _run(self, job: Job, fn) -> None:
        from ..db import store
        job.status = "running"
        job.started_at = time.time()

        def log(*parts):
            line = " ".join(str(p) for p in parts)
            job.lines.append(line)
            if len(job.lines) > 2000:
                del job.lines[:1000]

        with self._write_lock:
            con = store.connect(self.db_path)
            try:
                store.init_db(con)
                job.result = fn(con, log)
                job.status = "done"
            except Exception as e:  # noqa: BLE001 - surfaced to the browser, never swallowed
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
                log("FAILED:", job.error)
                for frame in traceback.format_exc().splitlines()[-12:]:
                    log(frame)
            finally:
                con.close()
                job.finished_at = time.time()

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            ids = list(reversed(self._order[-limit:]))
        return [self.jobs[i].as_dict(tail=3) for i in ids if i in self.jobs]

    @property
    def busy(self) -> bool:
        return any(j.status == "running" for j in self.jobs.values())
