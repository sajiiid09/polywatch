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
import sys
from datetime import datetime, timezone

from . import ingest
from .config import (DB_PATH, DEFAULT_BANKROLL_USD, DEFAULT_STAKE_USD, ENV_FUNDER,
                     MAX_RPS)
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
    # A run's log is the only thing watching a live session from outside the process, and Python
    # block-buffers stdout the moment it is not a terminal -- so `task run ... | tee run.log`
    # showed nothing for the first 8 KB, which for a quiet session is the entire run. An operator
    # tailing a log to decide whether to intervene needs the line when it happens, not later.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):          # a stdout that cannot be reconfigured
        pass
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

    dc = sub.add_parser("discover", help="sweep, screen, score and rank wallets worth copying")
    dc.add_argument("--limit", type=int, default=20, help="shortlist length")
    dc.add_argument("--pages", type=int, default=1,
                    help="leaderboard pages per board; 1 board-page is 50 wallets")
    dc.add_argument("--candidates", type=int, default=60,
                    help="cap on wallets deep-scored; this stage is where the cost is")
    dc.add_argument("--category", action="append", default=None,
                    help="restrict the sweep to these leaderboard categories (repeatable)")
    dc.add_argument("--no-replay", action="store_true",
                    help="skip the copy-lag replay. Much faster, and copyability is then "
                         "inferred from holding period rather than measured")
    dc.add_argument("--include-excluded", action="store_true",
                    help="also show wallets that failed a hard gate, and why")
    dc.add_argument("--archetype", action="append", default=None,
                    help="only wallets classified as this; repeat for several. "
                         "scalper, momentum-chaser, event-specialist and fade-the-move "
                         "are the quick-swing kinds; resolution-holder is not")
    dc.add_argument("--max-hold-hours", type=float, default=None,
                    help="only wallets whose median hold is under this, whatever they "
                         "are labelled -- a slow wallet produces no flips to copy")
    dc.add_argument("--rps", type=float, default=MAX_RPS)
    dc.add_argument("--json", action="store_true")
    for name, default, helptext in _THRESHOLD_FLAGS:
        dc.add_argument(f"--{name.replace('_', '-')}", type=type(default), default=default,
                        help=helptext)

    tr = sub.add_parser("trader", help="skill score card for one wallet")
    tr.add_argument("address")
    tr.add_argument("--pages", type=int, default=20,
                    help="settled-position pages to pull; 50 rows each")
    tr.add_argument("--rps", type=float, default=MAX_RPS)
    tr.add_argument("--no-replay", action="store_true",
                    help="skip the copy-lag replay and the price backfill it needs")
    tr.add_argument("--json", action="store_true")


    ac = sub.add_parser("account", help="what the exchange thinks this account holds")
    ac.add_argument("address", nargs="?", default=None,
                    help=f"defaults to ${ENV_FUNDER}; unauthenticated either way")
    ac.add_argument("--activity", type=int, default=10, help="recent activity rows to show")
    ac.add_argument("--limit", type=int, default=20,
                    help="open positions to print, largest first; 0 for all")
    ac.add_argument("--live-check", action="store_true",
                    help="check the live-trading setup -- keys, wallet type, CLOB auth -- "
                         "without placing an order")
    ac.add_argument("--rps", type=float, default=MAX_RPS)
    ac.add_argument("--json", action="store_true")

    tk = sub.add_parser("task", help="create and run a copy-trading task")
    tsub = tk.add_subparsers(dest="task_cmd", required=True)

    tc = tsub.add_parser("create", help="create or update a task")
    tc.add_argument("name")
    tc.add_argument("--trader", action="append", default=None,
                    help="wallet to copy; repeat to copy several on one bankroll")
    tc.add_argument("--from-shortlist", type=int, default=None, metavar="N",
                    help="copy the top N wallets from `polywatch discover` instead of naming "
                         "them; excluded wallets are never chosen")
    tc.add_argument("--min-rank-score", type=float, default=None,
                    help="floor on rank_score when building a roster from the shortlist")
    tc.add_argument("--archetype", action="append", default=None,
                    help="only wallets classified as this; repeat for several. "
                         "scalper, momentum-chaser, event-specialist and fade-the-move "
                         "are the quick-swing kinds; resolution-holder is not")
    tc.add_argument("--max-hold-hours", type=float, default=None,
                    help="only wallets whose median hold is under this, whatever they "
                         "are labelled -- a slow wallet produces no flips to copy")

    tc.add_argument("--per-trader-usd", type=float, default=None,
                    help="cap on what one trader's signals may have at risk; defaults to an "
                         "equal share of the bankroll")
    tc.add_argument("--auto-drop-usd", type=float, default=None,
                    help="stop copying a trader after their signals lose this much in a run")
    tc.add_argument("--preset", default="quick_flips", choices=sorted(task_mod.PRESETS),
                    help="quick_flips is what the poller is tuned for; hours loosens the "
                         "exits for an idea you intend to babysit yourself")
    tc.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL_USD)
    tc.add_argument("--stake", type=float, default=DEFAULT_STAKE_USD,
                    help="USD per copied trade (fixed)")
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
    trn.add_argument("--stream", action="store_true",
                     help="use streamed order books, so the stop-loss and trailing stop are "
                          "checked on every book update rather than once per poll; needs the "
                          "optional `stream` extra")
    trn.add_argument("--no-stream", action="store_true",
                     help="poll for books even when streaming is available")
    trn.add_argument("--chain", action="store_true",
                     help="detect the copied wallets' fills from Polygon rather than waiting "
                          "for data-api to index them; measured at 13-21s of the copy latency, "
                          "against 0.0-0.4s for the loop itself. On by default; needs the "
                          "optional `stream` extra")
    trn.add_argument("--no-chain", action="store_true",
                     help="detect fills only from the /activity poll, at its ~15s cache floor")
    # A run ends by recording what it did and what is left for whoever is next. On by default:
    # the sessions worth handing over are the ones nobody remembered to log.
    trn.add_argument("--no-session-log", action="store_true",
                     help="do not write a session handoff when the run ends")
    trn.add_argument("--note", default=None,
                     help="free text carried into this run's session handoff")
    trn.add_argument("--operator", default=None,
                     help="who is running it: a name, 'agent', 'human'")

    trp = tsub.add_parser("report", help="what a run did")
    trp.add_argument("name")
    trp.add_argument("--run-id", type=int, default=None, help="defaults to the latest run")
    trp.add_argument("--json", action="store_true")

    tor = tsub.add_parser("orders", help="resting GTC orders left on the book")
    tor.add_argument("--cancel", action="store_true", help="cancel every live one")

    # `polywatch strategy` -- what kind of traders are being copied, and what the record of
    # those kinds suggests. Nothing here applies anything (RULES.md I8).
    sg = sub.add_parser("strategy", help="trader archetypes, learned findings, proposals")
    sgs = sg.add_subparsers(dest="strategy_cmd", required=True)
    sp = sgs.add_parser("profile", help="one wallet's archetype and the features behind it")
    sp.add_argument("address")
    sp.add_argument("--json", action="store_true")
    sb = sgs.add_parser("backfill", help="classify every wallet already in the trades table "
                                        "(offline; no requests)")
    sb.add_argument("--limit", type=int, default=None, help="only the N busiest wallets")
    sb.add_argument("--min-trades", type=int, default=20,
                    help="skip wallets with fewer stored trades than this")
    for name, helptext in (("show", "findings and proposals; writes nothing"),
                           ("learn", "recompute, snapshot, and regenerate the learned doc")):
        sl = sgs.add_parser(name, help=helptext)
        sl.add_argument("--task", default=None,
                        help="scope to one task; default pools every task")
        sl.add_argument("--json", action="store_true")

    # `polywatch session` -- the handoff between one operator of the account and the next.
    sn = sub.add_parser("session", help="account handoff: brief the next operator, record this one")
    sns = sn.add_subparsers(dest="session_cmd", required=True)
    so = sns.add_parser("open", help="print the last session's briefing (does NOT resume state)")
    so.add_argument("name")
    sc = sns.add_parser("close", help="record account state and write the handoff")
    sc.add_argument("name")
    sc.add_argument("--run-id", type=int, default=None,
                    help="which run to close out; default is the task's last")
    sc.add_argument("--note", default=None, help="free text carried into the handoff")
    sc.add_argument("--operator", default=None, help="who ran it: a name, 'agent', 'human'")
    sc.add_argument("--json", action="store_true")
    sll = sns.add_parser("log", help="recent sessions, newest first")
    sll.add_argument("name", nargs="?", default=None)
    sll.add_argument("--limit", type=int, default=10)
    sll.add_argument("--json", action="store_true")

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
        results = run_screen(store.wallets_by_rank(con),
                             lambda a: store.screening_trades(con, a),
                             _thresholds(args))
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

    if args.cmd == "discover":
        from .copytrade import discover
        from .fetch.client import Client
        con = store.connect(args.db)
        store.init_db(con)
        client = Client(con=con, rps=args.rps, dump_raw=False)
        discover.run(con, client, limit=args.limit, pages=args.pages,
                     with_replay=not args.no_replay,
                     categories=[c.upper() for c in args.category] if args.category else None,
                     thresholds=_thresholds(args), max_candidates=args.candidates)
        rows = store.top_trader_scores(
            con, limit=args.limit, include_excluded=args.include_excluded,
            archetypes=args.archetype,
            max_hold_s=int(args.max_hold_hours * 3600) if args.max_hold_hours else None)
        if args.json:
            print(json.dumps([dict(r) for r in rows], indent=2))
            return 0
        print()
        print(discover.format_shortlist(rows))
        print("\n  Nothing here picks a trader. `polywatch trader <address>` for the full card.")
        return 0

    if args.cmd == "trader":
        from .copytrade import discover, replay as replay_mod
        from .fetch.client import Client
        con = store.connect(args.db)
        store.init_db(con)
        client = Client(con=con, rps=args.rps)
        sc, est, res = discover.score_trader(con, client, args.address, max_pages=args.pages,
                                             with_replay=not args.no_replay)
        strat = store.get_trader_strategy(con, args.address)
        if args.json:
            out = {**sc.as_row(), "est_account_usd": est}
            if strat is not None:
                out["strategy"] = dict(strat)
            if res is not None:
                out["replay"] = {"lags": [r.as_row() for r in res.lags],
                                 "capture_ratio": res.capture_ratio,
                                 "edge_half_life_s": res.edge_half_life_s}
            print(json.dumps(out, indent=2))
            return 0
        print()
        print(discover.format_score(sc, est, store.wallet_username(con, args.address),
                                    strategy_row=strat))
        if res is not None:
            print()
            print(replay_mod.format_curve(res))
        if sc.n_closed == 0:
            print("\n  no settled positions -- nothing to judge this wallet on")
        return 0


    if args.cmd == "account":
        from . import account as account_mod
        from .fetch.client import Client
        if args.live_check:
            rows = account_mod.live_preflight()
            if args.json:
                print(json.dumps([{"check": n, "ok": ok, "detail": d} for n, ok, d in rows],
                                 indent=2))
            else:
                print()
                print(account_mod.format_preflight(rows))
            return 0 if all(ok for _, ok, _ in rows) else 1
        try:
            addr = account_mod.resolve_address(args.address)
        except RuntimeError as e:
            print(e)
            return 1
        snap = account_mod.snapshot(Client(con=None, rps=args.rps, dump_raw=False),
                                    addr, activity_limit=args.activity)
        if args.json:
            print(json.dumps(snap, indent=2))
            return 0
        print()
        print(account_mod.format_snapshot(snap, limit=args.limit))
        return 0

    if args.cmd == "task":
        from .copytrade import commands
        return commands.dispatch(args)

    if args.cmd == "strategy":
        from .copytrade import commands
        return commands.strategy_dispatch(args)

    if args.cmd == "session":
        from .copytrade import commands
        return commands.session_dispatch(args)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
