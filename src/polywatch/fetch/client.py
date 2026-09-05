"""HTTP layer. Knows about sockets, retries and disk. Knows nothing about polywatch's schema.

Every response is written to data/raw/ verbatim and logged to ingest_log BEFORE any caller
looks at it, so when a field gets renamed upstream the evidence is already on disk.

Read-only by construction: GET only, no auth header, no request body, no signing.
"""

from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import (BACKOFF_BASE, BACKOFF_CAP, MAX_ATTEMPTS, MAX_RPS, RAW, USER_AGENT)


class FetchError(RuntimeError):
    pass


class RateLimiter:
    """Single-process token bucket. Polymarket publishes no limit for these public endpoints,
    so we impose one rather than discover theirs the hard way."""

    def __init__(self, rps: float = MAX_RPS):
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self._last = 0.0
        # Lock, because worker threads share one limiter: the cap is on total requests to
        # Polymarket, not per-thread.
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            gap = self.min_interval - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


class Client:
    def __init__(self, con: sqlite3.Connection | None = None, rps: float = MAX_RPS,
                 raw_dir: Path = RAW, dump_raw: bool = True,
                 limiter: RateLimiter | None = None, buffer_logs: bool = False,
                 log_ok: bool = True, timeout: float = 30.0):
        self.con = con
        # A shared limiter lets several Clients (or threads) obey one global rate cap.
        self.limiter = limiter or RateLimiter(rps)
        self.raw_dir = Path(raw_dir)
        self.dump_raw = dump_raw
        # Worker threads must not touch the SQLite connection, so they buffer log rows and the
        # main thread writes them. Appends to a list are atomic under the GIL.
        self.buffer_logs = buffer_logs
        self.log_buffer: list[dict] = []
        # The copy-trading poller calls /activity once a second, forever. Logging every success
        # would add ~250k ingest_log rows a day to say nothing happened. Failures are still
        # logged -- that is the whole reason this flag is `log_ok` and not `log`.
        self.log_ok = log_ok
        # And a 30s socket timeout inside a 1Hz loop is worse than a failed request: it freezes
        # the loop, which means it freezes the stop-loss check. Trading clients pass ~5.
        self.timeout = timeout

    def get_json(self, url: str, params: dict | None = None, *, kind: str,
                 raw_name: str | None = None, dump_raw: bool | None = None):
        """GET and parse JSON, retrying on 429/5xx/transport errors.

        Backoff is exponential with jitter, and honours Retry-After when the server sends it.
        A 4xx that isn't 429 is not retried -- that's a bad request, and repeating it is rude
        and pointless.

        `dump_raw` overrides the client-wide setting for this one call. The copy-trading poller
        hits /activity every second forever; keeping the audit dump on would fill the disk with
        near-identical files, and its own signals table is the better record anyway.
        """
        full = url + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        req = urllib.request.Request(
            full, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )

        last_err = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.limiter.wait()
            status = None
            raw = b""
            retry_after = None
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    status, raw = r.status, r.read()
            except urllib.error.HTTPError as e:
                status, raw = e.code, e.read()
                retry_after = e.headers.get("Retry-After")
            except Exception as e:  # noqa: BLE001 - transport failures are retryable
                last_err = f"{type(e).__name__}: {e}"

            if status == 200:
                try:
                    # strict=False is defensive, not diagnostic. A gamma /markets page did
                    # fail to parse strictly once on 2026-09-06 ("Invalid control character at
                    # char 607"), but it did not reproduce on later fetches and the cause was
                    # never pinned down -- it may have been an artifact of how that one response
                    # was piped rather than anything gamma did. Relaxing the parser costs
                    # nothing here (we re-serialise everything we store anyway) and the failure
                    # mode it guards against is losing a whole page to one byte.
                    parsed = json.loads(raw, strict=False)
                except json.JSONDecodeError as e:
                    last_err = f"200 but not JSON: {e}"
                else:
                    path = self._dump(kind, raw_name, raw, dump_raw)
                    self._log(kind=kind, url=full, status=status, bytes=len(raw),
                              records=_count(parsed), attempts=attempt, error=None,
                              raw_path=str(path) if path else None)
                    return parsed
            elif status is not None and status != 429 and 400 <= status < 500:
                self._log(kind=kind, url=full, status=status, bytes=len(raw), records=None,
                          attempts=attempt, error=raw[:400].decode("utf-8", "replace"),
                          raw_path=None)
                raise FetchError(f"{status} on {full}: {raw[:300]!r}")
            else:
                last_err = last_err or f"status {status}: {raw[:200]!r}"

            if attempt == MAX_ATTEMPTS:
                break
            delay = min(BACKOFF_CAP, BACKOFF_BASE ** attempt) + random.uniform(0, 0.5)
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
            time.sleep(delay)

        self._log(kind=kind, url=full, status=status, bytes=len(raw), records=None,
                  attempts=MAX_ATTEMPTS, error=last_err, raw_path=None)
        raise FetchError(f"gave up on {full} after {MAX_ATTEMPTS} attempts: {last_err}")

    def _dump(self, kind: str, raw_name: str | None, raw: bytes,
              override: bool | None = None) -> Path | None:
        if not (self.dump_raw if override is None else override):
            return None
        d = self.raw_dir / kind
        d.mkdir(parents=True, exist_ok=True)
        name = raw_name or f"{int(time.time() * 1000)}"
        path = d / f"{name}.json"
        path.write_bytes(raw)
        return path

    def _log(self, **kw) -> None:
        if not self.log_ok and kw.get("error") is None:
            return
        if self.buffer_logs:
            self.log_buffer.append(kw)
            return
        if self.con is None:
            return
        from ..db import store
        store.log_fetch(self.con, **kw)

    def drain_logs(self, con: sqlite3.Connection) -> int:
        """Flush buffered log rows. Call from the thread that owns the connection."""
        from ..db import store
        rows, self.log_buffer = self.log_buffer, []
        for row in rows:
            store.log_fetch(con, **row)
        return len(rows)


def _count(parsed) -> int | None:
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict):
        for k in ("history", "data", "results"):
            if isinstance(parsed.get(k), list):
                return len(parsed[k])
        return 1
    return None
