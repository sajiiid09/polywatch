"""A local console for the copy trader. Standard library only.

The project declares `dependencies = []` and the reason is stated in `cli.py`: "does this
program spend money" should have a one-file answer. A web framework would not change that
answer, but it would make it longer to verify, so this is `http.server` and a single HTML file.

Bound to localhost by default. The console can start jobs that spend the machine's bandwidth
and rewrite the database, and it has no authentication of any kind -- exposing it on a network
interface would hand those controls to whoever else is on the network.

Nothing here can trade with real money. Every action ends in the paper engine, because the live
path does not exist yet.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import (BACKTEST_SLIPPAGE_SWEEP, DEFAULT_BANKROLL_USD, DEFAULT_STAKE_FRACTION,
                      MAX_RPS, RANK_WINDOW_DAYS)
from ..db import store
from .jobs import JobRunner

STATIC = Path(__file__).resolve().parent


class Console:
    """Request handling that does not know it is HTTP. Keeps the handler class trivial."""

    def __init__(self, db_path: str, rps: float = MAX_RPS):
        self.db_path = db_path
        self.rps = rps
        self.jobs = JobRunner(db_path)
        # A SQLite connection belongs to the thread that opened it, and ThreadingHTTPServer
        # hands every request to a fresh thread -- so connections are thread-local rather than
        # shared. Opening one is cheap; smuggling one across threads raises on first use.
        self._local = threading.local()
        boot = store.connect(db_path)
        store.init_db(boot)
        boot.close()

    @property
    def _con(self):
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._local.con = store.connect(self.db_path)
        return con

    # --- reads ---------------------------------------------------------------------------

    def get(self, path: str, q: dict):
        if path == "/api/overview":
            return {"overview": store.overview(self._con),
                    "tasks": store.task_overview(self._con),
                    "runs": store.run_overview(self._con, limit=15),
                    "jobs": self.jobs.recent(),
                    "slippages": list(BACKTEST_SLIPPAGE_SWEEP),
                    "defaults": {"bankroll": DEFAULT_BANKROLL_USD,
                                 "stake_fraction": DEFAULT_STAKE_FRACTION,
                                 "days": RANK_WINDOW_DAYS},
                    "server_time": int(time.time())}
        if path == "/api/traders":
            return {"traders": store.ranked_traders(self._con, _int(q, "limit", 50))}
        if path == "/api/tasks":
            return {"tasks": store.task_overview(self._con)}
        if path == "/api/runs":
            return {"runs": store.run_overview(self._con, _int(q, "limit", 50),
                                               _str(q, "task"))}
        if path == "/api/run":
            return store.run_detail(self._con, _int(q, "id", 0), _int(q, "limit", 200))
        if path == "/api/wallet":
            return store.wallet_summary(self._con, _str(q, "address") or "")
        if path == "/api/jobs":
            return {"jobs": self.jobs.recent(50)}
        if path == "/api/job":
            job = self.jobs.get(_str(q, "id") or "")
            return job.as_dict() if job else {"error": "no such job"}
        return None

    # --- writes --------------------------------------------------------------------------

    def post(self, path: str, body: dict):
        if path == "/api/follow":
            return self._follow(body)
        if path == "/api/backtest":
            return self._backtest(body)
        if path == "/api/recommend":
            return self._recommend(body)
        if path == "/api/task/delete":
            n = store.delete_task(self._con, str(body.get("name") or ""))
            return {"deleted": n}
        return None

    def _client(self, con):
        from ..fetch.client import Client
        return Client(con=con, rps=self.rps, dump_raw=False)

    def _task_from(self, body: dict, address: str, slippage: float, name: str):
        from ..copytrade.task import Task
        bankroll = float(body.get("bankroll") or DEFAULT_BANKROLL_USD)
        stake = body.get("fixed_usd")
        stake = float(stake) if stake else bankroll * DEFAULT_STAKE_FRACTION
        return Task(
            name=name, trader=address, bankroll=bankroll, fixed_usd=stake,
            max_market_usd=float(body.get("max_market_usd") or stake * 2),
            max_concurrent=int(body.get("max_concurrent") or 5),
            slippage=slippage,
            behavior=str(body.get("behavior") or "buys_sells"),
            risk=str(body.get("risk") or "moderate"),
            style=str(body.get("style") or "momentum"),
            hold=str(body.get("hold") or "hours"),
            activity=str(body.get("activity") or "active"),
            buy_method=str(body.get("buy_method") or "fixed"),
        )

    def _follow(self, body: dict):
        from ..copytrade import replay, select
        address = select.valid_address(str(body.get("trader") or ""))
        days = int(body.get("days") or RANK_WINDOW_DAYS)
        fetch = bool(body.get("fetch", True))
        base = str(body.get("name") or f"follow-{address[:10]}")

        def work(con, log):
            since = int(time.time()) - days * 86400
            if fetch:
                log(f"preparing {address}")
                n = select.prepare_wallet(con, self._client(con), address, since, log=log)
                if n == 0:
                    raise ValueError(
                        f"no trades for {address} in the last {days} days -- either it has not "
                        "traded recently, or it is not a Polymarket trading wallet")
            out = {}
            for slip in BACKTEST_SLIPPAGE_SWEEP:
                task = self._task_from(body, address, slip,
                                       f"{base}-s{int(slip * 1000):03d}")
                log(f"backtesting at {slip:.0%} slippage...")
                s = replay.run(con, task, slip, since_ts=since)
                out[str(slip)] = {"final": s["final_value_at_cost"], "cash": s["cash"],
                                  "copied": s["copied"], "skipped": s["skipped"],
                                  "closed": s["positions_closed"], "wins": s["wins"],
                                  "run_id": s["run_id"],
                                  "fees": sum(s["fee_sources"].values()),
                                  "skips": s["skips"][:8]}
                log(f"  ${task.bankroll:,.0f} -> ${s['final_value_at_cost']:,.2f}")
            # The saved task is pinned to the worst slippage: a follow that gets acted on should
            # be configured for execution going badly, not well.
            keeper = self._task_from(body, address, max(BACKTEST_SLIPPAGE_SWEEP), base)
            store.upsert_task(con, keeper.to_row())
            log(f"registered task '{base}' (paper mode)")
            return {"address": address, "task": base, "results": out}

        return {"job": self.jobs.submit("follow", f"follow {address[:12]}", work).as_dict()}

    def _backtest(self, body: dict):
        from ..copytrade import replay, select
        address = select.valid_address(str(body.get("trader") or ""))
        days = int(body.get("days") or RANK_WINDOW_DAYS)

        def work(con, log):
            since = int(time.time()) - days * 86400
            out = {}
            for slip in BACKTEST_SLIPPAGE_SWEEP:
                task = self._task_from(body, address, slip,
                                       f"bt-{address[:10]}-s{int(slip * 1000):03d}")
                s = replay.run(con, task, slip, since_ts=since)
                out[str(slip)] = {"final": s["final_value_at_cost"], "run_id": s["run_id"],
                                  "closed": s["positions_closed"], "wins": s["wins"],
                                  "copied": s["copied"], "skipped": s["skipped"],
                                  "fees": sum(s["fee_sources"].values()),
                                  "skips": s["skips"][:8]}
                log(f"{slip:.0%} slippage -> ${s['final_value_at_cost']:,.2f}")
            return {"address": address, "results": out}

        return {"job": self.jobs.submit("backtest", f"backtest {address[:12]}",
                                        work).as_dict()}

    def _recommend(self, body: dict):
        from ..copytrade import select
        reuse = bool(body.get("reuse_scan", False))
        reuse_scores = bool(body.get("reuse_scores", False))
        finalists = int(body.get("finalists") or 10)
        bankroll = float(body.get("bankroll") or DEFAULT_BANKROLL_USD)

        def work(con, log):
            funnel, out = select.run(con, self._client(con), bankroll=bankroll,
                                     finalists=finalists, reuse_scan=reuse,
                                     reuse_scores=reuse_scores, log=log)
            return {"funnel": funnel.__dict__,
                    "finalists": [{"address": f.ranked.address, "username": f.username,
                                   "rank_score": f.ranked.rank_score,
                                   "brier": f.ranked.score.brier,
                                   "roi": f.ranked.score.roi,
                                   "consistency": f.ranked.score.consistency,
                                   "closed": f.closed,
                                   "out_of_sample": {str(k): v
                                                     for k, v in f.out_of_sample.items()},
                                   "disqualifiers": f.disqualifiers} for f in out]}

        return {"job": self.jobs.submit("recommend", "leaderboard sweep + walk-forward",
                                        work).as_dict()}


def _int(q, key, default):
    try:
        return int(q.get(key, [default])[0])
    except (TypeError, ValueError):
        return default


def _str(q, key):
    v = q.get(key, [None])[0]
    return v or None


def make_handler(console: Console):
    class Handler(BaseHTTPRequestHandler):
        server_version = "polywatch"

        def log_message(self, *_):
            pass  # the console is interactive; a request log per poll is noise

        def _send(self, code: int, payload, content_type="application/json"):
            body = (json.dumps(payload, default=float).encode() if content_type ==
                    "application/json" else payload)
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Local-only tool; no caching so a rebuilt page is never served stale.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                try:
                    out = console.get(url.path, parse_qs(url.query))
                except Exception as e:  # noqa: BLE001
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})
                return self._send(200, out) if out is not None else \
                    self._send(404, {"error": "unknown endpoint"})

            name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            target = (STATIC / name).resolve()
            if STATIC not in target.parents or not target.is_file():
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)

        def do_POST(self):
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "body is not JSON"})
            try:
                out = console.post(url.path, body)
            except ValueError as e:
                # Bad input from the form -- the user's problem to fix, not a server fault.
                return self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return self._send(200, out) if out is not None else \
                self._send(404, {"error": "unknown endpoint"})

    return Handler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8787,
          rps: float = MAX_RPS) -> None:
    console = Console(db_path, rps=rps)
    httpd = ThreadingHTTPServer((host, port), make_handler(console))
    print(f"polywatch console on http://{host}:{port}")
    print("paper mode only -- nothing here can place a real order")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: bound to {host}, which is reachable from the network. This console "
              "has no authentication and can start jobs and rewrite the database.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
