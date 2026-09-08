"""polywatch CLI.

Originally read-only analytics, with a note here saying an order path was not a gap to be
filled later. It is being filled: the `trader` and `task` commands below exist to find a wallet
worth copying and then copy it.

The read-only guarantee has moved down a layer rather than disappearing. `fetch/client.py` is
still GET-only with no auth and no signing, and paper mode never signs anything at all. Live
execution lives in exactly one module -- copytrade/execution.py, class LiveExecutor -- behind an
explicit --mode live, an optional dependency and a typed confirmation, so that "does this
program spend money" stays a question with a one-file answer.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from . import ingest
from .config import DB_PATH, DEFAULT_BANKROLL_USD, MAX_RPS
from .copytrade import task as task_mod
from .db import store
from .screen import Thresholds, screen as run_screen


_THRESHOLD_FLAGS = [
    ("max_trades_per_day", 60.0, "above this a wallet is a machine, not a forecaster"),
    ("min_median_gap_s", 60.0, "sub-minute median spacing between trades is automation"),
    ("max_burst_frac", 0.50, "max share of trades landing within 60s of the previous one"),
    ("max_recency_days", 7.0, "must have traded this recently to be copyable"),
    ("min_active_days", 5, "distinct days traded"),
    ("min_active_day_ratio", 0.25, "active days / days spanned; catches one-burst records"),
    ("min_distinct_markets", 10, "diversification floor"),
    ("max_vol_percentile", 80.0, "drop the biggest-volume wallets in the candidate set"),
]


def _thresholds(args) -> Thresholds:
    return Thresholds(**{name: getattr(args, name) for name, _, _ in _THRESHOLD_FLAGS})


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="polywatch", description="Read-only Polymarket wallet analytics")
    p.add_argument("--db", default=str(DB_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create the schema")

    ing = sub.add_parser("ingest", help="pull wallets, trades, markets and price windows")
    ing.add_argument("--wallets", type=int, default=50, help="top N leaderboard wallets")
    ing.add_argument("--since", default="2026-01-01",
                     help="ignore trades older than this date (UTC)")
    ing.add_argument("--max-pages", type=int, default=25,
                     help="trade pages per wallet; 25 x 1000 = 25k trades")
    ing.add_argument("--rps", type=float, default=MAX_RPS,
                     help="global request cap across all workers")
    ing.add_argument("--workers", type=int, default=4,
                     help="parallel fetchers for the market and price stages")
    for name, default, helptext in _THRESHOLD_FLAGS:
        ing.add_argument(f"--{name.replace('_', '-')}", type=type(default), default=default,
                         help=helptext)
    ing.add_argument("--screen-only", action="store_true",
                     help="stop after screening; skips full history, markets and prices")
    ing.add_argument("--prices-all", action="store_true",
                     help="fetch price windows for unresolved markets too (default: resolved only)")

    sc = sub.add_parser("screen", help="re-screen already-ingested wallets and print profiles")
    for name, default, helptext in _THRESHOLD_FLAGS:
        sc.add_argument(f"--{name.replace('_', '-')}", type=type(default), default=default,
                        help=helptext)
    sc.add_argument("--csv", help="write the profile table here")

    st = sub.add_parser("status", help="row counts and ingestion health")
    st.add_argument("--json", action="store_true")

    pa = sub.add_parser("price-at", help="debug the hot query")
    pa.add_argument("token_id")
    pa.add_argument("ts", type=int)

    tr = sub.add_parser("trader", help="skill score card for one wallet")
    tr.add_argument("address")
    tr.add_argument("--pages", type=int, default=20,
                    help="settled-position pages to pull; 50 rows each")
    tr.add_argument("--rps", type=float, default=MAX_RPS)
    tr.add_argument("--json", action="store_true")


    tk = sub.add_parser("task", help="create and run a copy-trading task")
    tsub = tk.add_subparsers(dest="task_cmd", required=True)

    tc = tsub.add_parser("create", help="create or update a task")
    tc.add_argument("name")
    tc.add_argument("--trader", required=True, help="wallet to copy")
    tc.add_argument("--preset", default="quick_flips", choices=sorted(task_mod.PRESETS),
                    help="quick_flips is what the poller is tuned for; hours loosens the "
                         "exits for an idea you intend to babysit yourself")
    tc.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL_USD)
    tc.add_argument("--stake", type=float, default=10.0, help="USD per copied trade (fixed)")
    tc.add_argument("--mirror", action="store_true",
                    help="size as a fraction of their account instead of a fixed stake")
    tc.add_argument("--max-market-usd", type=float, default=None)
    tc.add_argument("--max-concurrent", type=int, default=None)
    tc.add_argument("--slippage", type=float, default=None)
    tc.add_argument("--stop-loss", type=float, default=None,
                    help="fraction off entry, e.g. 0.15; 0 disables")
    tc.add_argument("--take-profit", type=float, default=None,
                    help="fraction above entry, e.g. 0.10; 0 disables")
    tc.add_argument("--trail", type=float, default=None, help="trailing stop fraction; 0 off")
    tc.add_argument("--tp-policy", choices=("widen", "skip", "off"), default=None,
                    help="what to do when the take-profit is under the round-trip fee: widen "
                         "it to clear the fees (default), skip the trade, or leave it alone")
    tc.add_argument("--min-edge", type=float, default=None,
                    help="margin demanded on top of the fee floor, e.g. 0.02")
    tc.add_argument("--max-fee-frac", type=float, default=None,
                    help="refuse entries whose round-trip fee exceeds this share of the stake")
    tc.add_argument("--max-spread-frac", type=float, default=None,
                    help="refuse entries whose spread exceeds this share of the mid; read it "
                         "against the fee floor, both are costs paid before being right")
    tc.add_argument("--min-depth-usd", type=float, default=None,
                    help="demand this much on the ask side beyond our own stake")
    tc.add_argument("--max-hold-min", type=float, default=None)
    tc.add_argument("--poll", type=float, default=None, help="seconds between polls")
    tc.add_argument("--session-hours", type=float, default=None)
    tc.add_argument("--max-loss", type=float, default=None, help="stop the run down this much")
    tc.add_argument("--max-drawdown", type=float, default=None)
    tc.add_argument("--no-resting-tp", action="store_true",
                    help="watch for the target and sell at market instead of leaving a GTC "
                         "sell order on the book")
    tc.add_argument("--no-follow-exit", action="store_true",
                    help="do not sell when the trader sells")
    tc.add_argument("--no-flatten", action="store_true",
                    help="leave positions open when the session ends (nothing then watches "
                         "the stop-loss)")

    tsub.add_parser("list", help="every saved task")
    tsh = tsub.add_parser("show", help="one task's full config")
    tsh.add_argument("name")
    trm = tsub.add_parser("rm", help="delete a task")
    trm.add_argument("name")

    trn = tsub.add_parser("run", help="start a copy-trading session")
    trn.add_argument("name")
    trn.add_argument("--mode", choices=("paper", "live"), default=None,
                     help="overrides the task's mode for this run only")
    trn.add_argument("--session-hours", type=float, default=None)
    trn.add_argument("--yes", action="store_true", help="skip the live-mode confirmation")

    trp = tsub.add_parser("report", help="what a run did")
    trp.add_argument("name")
    trp.add_argument("--run-id", type=int, default=None, help="defaults to the latest run")
    trp.add_argument("--json", action="store_true")

    tor = tsub.add_parser("orders", help="resting GTC orders left on the book")
    tor.add_argument("--cancel", action="store_true", help="cancel every live one")

    args = p.parse_args(argv)

    if args.cmd == "init-db":
        con = store.connect(args.db)
        store.init_db(con)
        print(f"schema ready at {args.db}")
        return 0

    if args.cmd == "ingest":
        stats = ingest.run(
            args.db, wallet_limit=args.wallets, since_ts=_ts(args.since),
            max_pages=args.max_pages, rps=args.rps, resolved_only=not args.prices_all,
            workers=args.workers, thresholds=_thresholds(args),
            screen_only=args.screen_only,
        )
        print()
        print(stats.report())
        return 0

    if args.cmd == "screen":
        con = store.connect(args.db)
        store.init_db(con)
        results = run_screen(con, _thresholds(args))
        hdr = f"{'address':14} {'user':16} {'trd':>5} {'t/day':>7} {'gap_s':>8} {'burst':>6} " \
              f"{'days':>5} {'ratio':>6} {'mkts':>5} {'vol':>12} {'ok':>3}"
        print(hdr)
        print("-" * len(hdr))
        for p_, ok, fails in results:
            print(f"{p_.address[:14]:14} {(p_.username or '')[:16]:16} {p_.n_trades:5} "
                  f"{p_.trades_per_day:7.1f} {p_.median_gap_s:8.0f} {p_.burst_frac:6.2f} "
                  f"{p_.active_days:5} {p_.active_day_ratio:6.2f} {p_.distinct_markets:5} "
                  f"{(p_.vol or 0):12,.0f} {'Y' if ok else 'n':>3}"
                  + ("" if ok else f"   <- {fails[0]}"))
        passed = sum(1 for _, ok, _ in results if ok)
        print(f"\n{passed}/{len(results)} pass")
        if args.csv:
            import csv as _csv
            with open(args.csv, "w", newline="") as fh:
                w = _csv.DictWriter(fh, fieldnames=list(results[0][0].as_row()) + ["selected", "why"])
                w.writeheader()
                for p_, ok, fails in results:
                    w.writerow({**p_.as_row(), "selected": int(ok), "why": "; ".join(fails)})
            print(f"wrote {args.csv}")
        return 0

    if args.cmd == "status":
        con = store.connect(args.db)
        store.init_db(con)
        c = store.counts(con)
        if args.json:
            print(json.dumps(c, indent=2))
        else:
            for k, v in c.items():
                print(f"{k:18} {v}")
        return 0

    if args.cmd == "price-at":
        con = store.connect(args.db)
        print(store.price_at(con, args.token_id, args.ts))
        return 0

    if args.cmd == "trader":
        from .copytrade import discover
        from .fetch.client import Client
        con = store.connect(args.db)
        store.init_db(con)
        client = Client(con=con, rps=args.rps)
        sc, est = discover.score_trader(con, client, args.address, max_pages=args.pages)
        if args.json:
            print(json.dumps({**sc.as_row(), "est_account_usd": est}, indent=2))
            return 0
        row = con.execute("SELECT username FROM wallets WHERE address=?",
                          (args.address.lower(),)).fetchone()
        print()
        print(discover.format_score(sc, est, row["username"] if row else None))
        if sc.n_closed == 0:
            print("\n  no settled positions -- nothing to judge this wallet on")
        return 0


    if args.cmd == "task":
        from .copytrade import commands
        return commands.dispatch(args)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
