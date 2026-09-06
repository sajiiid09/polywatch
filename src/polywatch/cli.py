"""polywatch CLI.

Originally read-only analytics, with a note here saying an order path was not a gap to be
filled later. It is being filled: the `trader` and `task` commands below exist to find a wallet
worth copying and then copy it.

The read-only guarantee has moved down a layer rather than disappearing. `fetch/client.py` is
still GET-only with no auth and no signing, and paper mode never signs anything at all. When
live execution arrives it will live in exactly one module, behind an explicit flag and an
optional dependency, so that "does this program spend money" stays a question with a one-file
answer.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from . import ingest
from .config import (BACKTEST_SLIPPAGE_SWEEP, DB_PATH, DEFAULT_BANKROLL_USD,
                     DEFAULT_STAKE_FRACTION, FINALIST_COUNT, MAX_RPS,
                     RANK_WINDOW_DAYS)
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

    bt = sub.add_parser("backtest", help="replay a wallet's ingested history at a $100 bankroll")
    bt.add_argument("trader", help="wallet address to copy")
    bt.add_argument("--name", default=None, help="task name; defaults to backtest-<address>")
    bt.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL_USD)
    bt.add_argument("--buy-method", choices=["fixed", "mirror"], default="fixed")
    bt.add_argument("--fixed-usd", type=float, default=None,
                    help="stake per copied trade; defaults to 2%% of bankroll")
    bt.add_argument("--max-market-usd", type=float, default=None,
                    help="exposure ceiling per market; defaults to twice the stake")
    bt.add_argument("--max-concurrent", type=int, default=5)
    bt.add_argument("--behavior", choices=["buys", "buys_sells"], default="buys_sells")
    bt.add_argument("--risk", choices=["conservative", "moderate"], default="moderate")
    bt.add_argument("--style", choices=["safe_and_steady", "value_hunter", "momentum"],
                    default="momentum")
    bt.add_argument("--since", default=None, help="ignore trades before this date (UTC)")
    bt.add_argument("--slippage", type=float, default=None,
                    help="single slippage assumption; default sweeps 0%%/3%%/7%% instead")
    bt.add_argument("--json", action="store_true")

    rc = sub.add_parser("recommend",
                        help="sweep the leaderboard and recommend one wallet to copy")
    rc.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL_USD)
    rc.add_argument("--finalists", type=int, default=FINALIST_COUNT,
                    help="how many top-ranked wallets get walk-forward validated")
    rc.add_argument("--rps", type=float, default=MAX_RPS)
    fo = sub.add_parser("follow",
                        help="evaluate any Polymarket address and register it for copying")
    fo.add_argument("trader", help="wallet address, 0x + 40 hex characters")
    fo.add_argument("--name", default=None, help="task name; defaults to follow-<address>")
    fo.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL_USD)
    fo.add_argument("--fixed-usd", type=float, default=None,
                    help="stake per copied trade; defaults to 2%% of bankroll")
    fo.add_argument("--max-concurrent", type=int, default=5)
    fo.add_argument("--behavior", choices=["buys", "buys_sells"], default="buys_sells")
    fo.add_argument("--days", type=int, default=RANK_WINDOW_DAYS,
                    help="how much history to pull and backtest over")
    fo.add_argument("--rps", type=float, default=MAX_RPS)
    fo.add_argument("--no-fetch", action="store_true",
                    help="use only what is already stored; makes no network calls")
    fo.add_argument("--json", action="store_true")

    sv = sub.add_parser("serve", help="local web console")
    sv.add_argument("--host", default="127.0.0.1",
                    help="bind address; anything but localhost exposes an unauthenticated "
                         "console that can rewrite the database")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument("--rps", type=float, default=MAX_RPS)

    rc.add_argument("--reuse-scan", action="store_true",
                    help="skip the sweep and recon, screening what is already stored; only "
                         "sound when the existing recon is fresh")
    rc.add_argument("--reuse-scores", action="store_true",
                    help="also skip scoring and re-rank the stored metrics; the path to take "
                         "after changing a weight or the calibration formula")
    rc.add_argument("--json", action="store_true")

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

    if args.cmd == "backtest":
        from .copytrade import replay
        from .copytrade.task import Task
        con = store.connect(args.db)
        store.init_db(con)
        address = args.trader.lower()
        sweep = [args.slippage] if args.slippage is not None else list(BACKTEST_SLIPPAGE_SWEEP)
        # Stake defaults to a fraction of bankroll rather than a flat dollar amount, because
        # `stake x max_concurrent` is what actually decides whether a run can survive a losing
        # streak. At $10 a trade with 10 slots a $100 account is 100% deployed at all times and
        # ten consecutive losers end it -- measured across six screened wallets, that config
        # ruined four of them at every slippage, while 10% deployment left five profitable.
        fixed = args.fixed_usd if args.fixed_usd is not None \
            else args.bankroll * DEFAULT_STAKE_FRACTION
        max_market = args.max_market_usd if args.max_market_usd is not None else fixed * 2
        since = _ts(args.since) if args.since else 0

        out = []
        for slip in sweep:
            task = Task(
                # One task row per slippage point, so a sweep leaves three runs that can be
                # compared in SQL afterwards rather than one that overwrites itself.
                name=(args.name or f"backtest-{address[:10]}") + f"-s{int(slip * 1000):03d}",
                trader=address, mode="paper", bankroll=args.bankroll,
                buy_method=args.buy_method, fixed_usd=fixed,
                max_market_usd=max_market, max_concurrent=args.max_concurrent,
                slippage=slip, behavior=args.behavior, risk=args.risk, style=args.style,
            )
            out.append((task, replay.run(con, task, slip, since_ts=since)))

        if args.json:
            print(json.dumps([s for _, s in out], indent=2, default=float))
            return 0

        for task, summary in out:
            print()
            print(replay.format_report(summary, task))
        if len(out) > 1:
            print()
            print("  slippage is an assumption, not a measurement -- the spread across these "
                  "three\n  runs is how much of the answer rests on it.")
        return 0

    if args.cmd == "serve":
        from .web.server import serve
        serve(args.db, host=args.host, port=args.port, rps=args.rps)
        return 0

    if args.cmd == "follow":
        import time as _time
        from .copytrade import replay, select
        from .copytrade.task import Task
        from .fetch.client import Client
        con = store.connect(args.db)
        store.init_db(con)
        address = select.valid_address(args.trader)
        since = int(_time.time()) - args.days * 86400

        if not args.no_fetch:
            client = Client(con=con, rps=args.rps, dump_raw=False)
            print(f"preparing {address}")
            n = select.prepare_wallet(con, client, address, since, log=print)
            if n == 0:
                print(f"\nNo trades found for {address} in the last {args.days} days.")
                print("Either the address has not traded recently, or it is not a "
                      "Polymarket trading wallet.")
                return 1

        stake = args.fixed_usd if args.fixed_usd is not None \
            else args.bankroll * DEFAULT_STAKE_FRACTION
        base = args.name or f"follow-{address[:10]}"
        out = []
        for slip in BACKTEST_SLIPPAGE_SWEEP:
            task = Task(name=f"{base}-s{int(slip * 1000):03d}", trader=address,
                        bankroll=args.bankroll, fixed_usd=stake, max_market_usd=stake * 2,
                        max_concurrent=args.max_concurrent, slippage=slip,
                        behavior=args.behavior)
            out.append((task, replay.run(con, task, slip, since_ts=since)))

        # The registered task is the one at the pessimistic slippage: if a follow is going to be
        # acted on, it should be configured for the assumption that execution goes badly.
        keeper = Task(name=base, trader=address, bankroll=args.bankroll, fixed_usd=stake,
                      max_market_usd=stake * 2, max_concurrent=args.max_concurrent,
                      slippage=max(BACKTEST_SLIPPAGE_SWEEP), behavior=args.behavior)
        store.upsert_task(con, keeper.to_row())

        if args.json:
            print(json.dumps([s for _, s in out], indent=2, default=float))
            return 0
        for task, summary in out:
            print()
            print(replay.format_report(summary, task))
        print(f"\n  registered as task '{base}' (paper mode).")
        print("  Live execution is not built -- see the plan's Phase 2c.")
        return 0

    if args.cmd == "recommend":
        from .copytrade import select
        from .fetch.client import Client
        con = store.connect(args.db)
        store.init_db(con)
        # The sweep and the poller both hammer one endpoint repeatedly; dumping every response
        # to disk would write hundreds of near-identical files for no audit value.
        client = Client(con=con, rps=args.rps, dump_raw=False)
        funnel, finalists = select.run(con, client, bankroll=args.bankroll,
                                       finalists=args.finalists,
                                       reuse_scan=args.reuse_scan,
                                       reuse_scores=args.reuse_scores)
        _, cutoff = select.windows()
        if args.json:
            print(json.dumps({
                "funnel": funnel.__dict__,
                "cutoff": cutoff,
                "finalists": [{
                    "address": f.ranked.address, "username": f.username,
                    "rank_score": f.ranked.rank_score,
                    "components": f.ranked.components,
                    "metrics": f.ranked.score.as_row(),
                    "out_of_sample": {str(k): v for k, v in f.out_of_sample.items()},
                    "positions_closed": f.closed,
                    "disqualifiers": f.disqualifiers,
                } for f in finalists],
            }, indent=2, default=float))
            return 0
        print()
        print(select.format_report(funnel, finalists, args.bankroll, cutoff))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
