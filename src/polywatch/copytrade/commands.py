"""The `polywatch task` subcommands. Argument shuffling only -- the decisions live in
task.py, engine.py and report.py.

The one piece of real behaviour here is the live-mode confirmation. Everything else in this
program can be re-run; posting a signed order cannot.
"""

from __future__ import annotations

import json
import os
import sys

from ..config import (DEFAULT_SIGNATURE_TYPE, ENV_FUNDER, ENV_PRIVATE_KEY, ENV_SIGNATURE_TYPE,
                      ENV_UNATTENDED, MAX_RPS, POLL_TIMEOUT_S)
from ..db import store
from ..fetch.client import Client
from . import book as bk
from . import execution, learn, report, session, stream
from . import discover, strategy as strategy_mod
from .engine import Engine, ReconcileError
from .task import PRESETS, Task, preset


def dispatch(args) -> int:
    con = store.connect(args.db)
    store.init_db(con)
    return {
        "create": _create, "list": _list, "show": _show, "rm": _rm,
        "run": _run, "report": _report, "orders": _orders,
    }[args.task_cmd](con, args)


def _roster(con, args) -> list[str]:
    """The wallets this task will copy, from --trader, --from-shortlist, or both.

    A roster taken from the shortlist never includes an excluded wallet: exclusion is a
    statement that the wallet is not a candidate, and quietly copying one because it ranked
    highly among the rejects would undo the point of ranking.
    """
    out = [a.lower() for a in (args.trader or [])]
    n = getattr(args, "from_shortlist", None)
    if n:
        floor = args.min_rank_score or 0.0
        rows = store.top_trader_scores(con, limit=n * 3)
        picked = [r["address"] for r in rows if (r["rank_score"] or 0.0) >= floor][:n]
        if not picked:
            raise SystemExit(
                "no wallets in the shortlist clear that floor.\n"
                "Run `polywatch discover` first, or lower --min-rank-score.")
        out += [a for a in picked if a not in out]
    if not out:
        raise SystemExit("name at least one --trader, or use --from-shortlist N")
    return out


def _build(con, args) -> Task:
    """Flags -> Task. Unset flags fall through to the preset, then to the dataclass default."""
    over: dict = {}
    if args.mirror:
        over["buy_method"] = "mirror"
    for flag, field_ in (("max_market_usd", "max_market_usd"), ("max_concurrent", "max_concurrent"),
                         ("slippage", "slippage"), ("trail", "trail_pct"),
                         ("poll", "poll_interval_s"), ("session_hours", "session_hours"),
                         ("max_loss", "max_daily_loss_usd"),
                         ("max_drawdown", "max_drawdown_pct"), ("tp_policy", "tp_fee_policy"),
                         ("min_edge", "min_edge"), ("max_fee_frac", "max_fee_frac"),
                         ("max_spread_frac", "max_spread_frac"),
                         ("min_depth_usd", "min_depth_usd"),
                         ("per_trader_usd", "per_trader_usd"),
                         ("auto_drop_usd", "auto_drop_usd"),
                         ("min_rank_score", "min_rank_score")):
        v = getattr(args, flag, None)
        if v is not None:
            over[field_] = v
    if args.max_hold_min is not None:
        over["max_hold_s"] = int(args.max_hold_min * 60)
    # 0 is how a rung is switched off from the command line -- an explicit "no stop" rather
    # than the absence of a flag, which means "use the preset".
    if args.stop_loss is not None:
        over["sl_kind"], over["sl_value"] = ("pct", args.stop_loss) if args.stop_loss else (None, None)
    if args.take_profit is not None:
        over["tp_kind"], over["tp_value"] = ("pct", args.take_profit) if args.take_profit else (None, None)
    if args.no_resting_tp:
        over["resting_tp"] = False
    if args.no_follow_exit:
        over["follow_exit"] = False
    if args.no_flatten:
        over["flatten_on_stop"] = False
    roster = _roster(con, args)
    return Task(name=args.name, trader=roster[0], traders=roster, bankroll=args.bankroll,
                fixed_usd=args.stake, **preset(args.preset, **over))


def _create(con, args) -> int:
    t = _build(con, args)
    store.upsert_task(con, t.as_row())
    store.set_task_traders(con, t.name, [{"address": a} for a in t.traders])
    print(f"task {t.name} saved ({t.hold} preset, {t.mode} mode)")
    print(_describe(t))
    return 0


def _describe(t: Task) -> str:
    sl = "none" if t.sl_kind is None else (f"-{t.sl_value:.0%} off entry" if t.sl_kind == "pct"
                                           else f"at {t.sl_value:.3f}")
    tp = ("none -- exits follow the trader, the trail and the stop"
          if t.tp_kind is None else
          (f"+{t.tp_value:.0%} off entry" if t.tp_kind == "pct" else f"at {t.tp_value:.3f}"))
    # The fee floor is price-dependent, so it is shown at a price rather than as a constant.
    # 0.50 is the worst case and the one that surprises people.
    floor = bk.round_trip_fee_frac(0.50, 0.05)
    who = ([f"  trader        {t.trader}"] if len(t.traders) == 1 else
           [f"  traders       {len(t.traders)}, up to ${t.per_trader_cap:,.2f} of risk each"]
           + [f"                {a}" for a in t.traders]
           + ([f"                dropped once down ${t.auto_drop_usd:,.2f}"]
              if t.auto_drop_usd else []))
    return "\n".join(x for x in [
        *who,
        f"  stake         {t.buy_method} "
        + (f"${t.fixed_usd:,.2f}" if t.buy_method == "fixed"
           else f"up to {t.mirror_max_frac:.0%} of ${t.bankroll:,.2f}"),
        f"  stop-loss     {sl}",
        f"  take-profit   {tp}"
        + ("  posted as a resting GTC sell -- fills without the bot running"
           if t.resting_tp and t.tp_kind else ""),
        f"  trailing      {t.trail_pct:.0%}" if t.trail_pct else "  trailing      off",
        f"  fee floor     a round trip costs {floor:.0%} of stake at 0.50, "
        f"{bk.round_trip_fee_frac(0.85, 0.05):.0%} at 0.85 (5% category)",
        (f"                targets under that are {t.tp_fee_policy}"
         + (f"ed to the floor +{t.min_edge:.0%}" if t.tp_fee_policy == "widen"
            else ("ped as fee_floor" if t.tp_fee_policy == "skip" else " left as set"))
         ) if t.tp_kind == "pct" else "",
        f"                entries refused above {t.max_fee_frac:.0%} fee"
        if t.max_fee_frac else "",
        f"  liquidity     spread over {t.max_spread_frac:.0%} of mid is refused"
        + (f"; ask side must hold ${t.min_depth_usd:,.2f}" if t.min_depth_usd else "")
        if t.max_spread_frac else "",
        f"  time stop     {t.max_hold_s / 60:.0f} min",
        f"  follow exits  {'yes' if t.follow_exit else 'no'}",
        f"  poll          {t.poll_interval_s:.0f}s, skip signals older than "
        f"{t.max_signal_age_s}s",
        f"  session       {t.session_hours:.1f}h; breakers -${t.max_daily_loss_usd:,.2f} "
        f"or -{t.max_drawdown_pct:.0%}",
    ] if x)


def _list(con, args) -> int:
    rows = store.list_tasks(con)
    if not rows:
        print("no tasks")
        return 0
    # 24, not 16: task names run to 22 characters and the suffix is the part that distinguishes
    # them, so a narrower column silently prints three different tasks as three identical lines.
    print(f"{'name':24} {'mode':6} {'trader':14} {'bankroll':>9} {'stake':>8} {'hold':12} runs")
    for r in rows:
        n = store.run_count(con, r["name"])
        print(f"{r['name'][:24]:24} {r['mode']:6} {r['trader'][:14]:14} "
              f"{r['bankroll']:9,.0f} {(r['fixed_usd'] or 0):8,.0f} {r['hold']:12} {n}")
    return 0


def _show(con, args) -> int:
    row = store.get_task(con, args.name)
    if row is None:
        print(f"no task {args.name}")
        return 1
    t = Task.from_row(row)
    print(f"task {t.name}  ({t.mode})")
    print(_describe(t))
    return 0


def _rm(con, args) -> int:
    print(f"deleted {store.delete_task(con, args.name)} task(s)")
    return 0


LIVE_WARNING = """
LIVE MODE. This will sign orders with the key in ${key} ({wallet}) and spend real
USDC from {funder}.

Before confirming, understand what is and is not protected while it runs:

  * The take-profit is posted to the exchange as a resting GTC sell order. It will fill on its
    own even if this program is closed or the machine sleeps.
  * The stop-loss is not an exchange order. Polymarket's CLOB has no stop order type, so the
    stop is a price this process watches and a market sell it sends. If this process is not
    running, the stop is not running, and the position can fall past it unchecked.
  * The session lasts {hours:.1f} hours and then sells everything it is holding, which is the
    only reason it is safe to close the terminal afterwards.
  * The run stops itself after losing ${loss:,.2f} or {dd:.0%} of the bankroll, whichever comes
    first, and flattens.

Most it can spend at once: ${exposure:,.2f} across {n} concurrent positions.
"""


def _confirm_live(t: Task) -> bool:
    sig = os.environ.get(ENV_SIGNATURE_TYPE) or str(DEFAULT_SIGNATURE_TYPE)
    wallet = {"0": "bare EOA", "1": "email/magic-login proxy wallet",
              "2": "browser-wallet proxy"}.get(sig, f"signature type {sig}")
    print(LIVE_WARNING.format(key=ENV_PRIVATE_KEY, funder=os.environ.get(ENV_FUNDER, "(unset)"),
                              wallet=wallet,
                              hours=t.session_hours, loss=t.max_daily_loss_usd,
                              dd=t.max_drawdown_pct, exposure=t.max_market_usd * t.max_concurrent,
                              n=t.max_concurrent))
    return input("Type LIVE to confirm: ").strip() == "LIVE"


def _run(con, args) -> int:
    row = store.get_task(con, args.name)
    if row is None:
        print(f"no task {args.name}")
        return 1
    t = Task.from_row(row)
    if args.mode:
        t.mode = args.mode
    if args.session_hours is not None:
        t.session_hours = args.session_hours

    if t.mode == "live":
        # --yes exists so a person who has already read the warning does not have to type LIVE
        # again. In a cron entry the same flag spends money with nobody watching, and the
        # difference between the two is not visible from inside this function -- so ask the
        # environment. A terminal is a person; anything else has to say out loud that it meant
        # to trade unattended.
        if args.yes and not sys.stdin.isatty() and os.environ.get(ENV_UNATTENDED) != "1":
            print(f"refusing --yes live outside a terminal: set {ENV_UNATTENDED}=1 if this "
                  f"really is meant to trade with nobody watching (the stop-loss only runs "
                  f"while this process does)")
            return 1
        if not args.yes and not _confirm_live(t):
            print("cancelled")
            return 1

    try:
        ex = execution.make(t.mode)
    except RuntimeError as e:
        print(f"live mode unavailable: {e}")
        return 1

    # A short socket timeout, and no raw dumps: the poller runs for hours, and a 30-second
    # stall inside the loop is a 30-second window with no stop-loss.
    client = Client(con=con, rps=MAX_RPS, dump_raw=False, log_ok=False,
                    timeout=POLL_TIMEOUT_S)

    # A live book feed if the optional extra is installed. It starts with no subscriptions --
    # a run holds nothing yet -- and picks up each token as a position opens in it.
    feed = None
    if not args.no_stream:
        feed = stream.connect([], log=print)
        if feed is None and stream_requested(args):
            print("  book streaming needs the optional extra: "
                  "uv pip install 'polywatch[stream]'")

    eng = Engine(con, t, ex, client, stream=feed)
    try:
        summary = eng.run()
    except ReconcileError as e:
        # The banner never printed, because the run refused to start. Say so plainly and leave
        # the run row closed with its reason, so `task report` explains it too.
        print(f"\n{e}")
        last = store.last_run(con, t.name)
        if last is not None:
            print()
            print(report.run_report(con, last["id"], t))
            # A run that refused to start is exactly the run whose handoff matters: the next
            # operator has to be told the exchange disagreed with us before they try again.
            _close_session(con, t.name, last["id"], args)
        return 1
    finally:
        if feed is not None:
            feed.stop()
    print()
    print(report.run_report(con, summary["run_id"], t))
    _close_session(con, t.name, summary["run_id"], args)
    return 0


def _close_session(con, task: str, run_id: int, args) -> None:
    """Record the handoff. Failing to write it must not fail the run -- the trading is already
    done and its record is already in the database; this is the narrative on top of it."""
    if getattr(args, "no_session_log", False):
        return
    try:
        h = session.close(con, task, run_id, note=getattr(args, "note", "") or "",
                          operator=getattr(args, "operator", None) or "agent")
    except Exception as e:                                   # noqa: BLE001
        print(f"\n  could not write the session handoff: {e}")
        return
    print()
    print("  session recorded. next steps:")
    for i, step in enumerate(h.steps, 1):
        print(f"    {i}. {step}")


# --- `polywatch strategy` -------------------------------------------------
# What kind of traders are being copied, how those kinds have performed, and what the numbers
# suggest changing. Nothing here applies anything: see RULES.md I8.


def strategy_dispatch(args) -> int:
    con = store.connect(args.db)
    store.init_db(con)
    return {"profile": _strategy_profile, "show": _strategy_show, "learn": _strategy_learn,
            "backfill": _strategy_backfill}[args.strategy_cmd](con, args)


def _strategy_profile(con, args) -> int:
    address = args.address.lower()
    row = store.get_trader_strategy(con, address)
    if row is None:
        print(f"  no strategy profile for {address}.\n"
              f"  `polywatch trader {address}` scores and classifies a wallet in one pass.")
        return 1
    if args.json:
        print(json.dumps(dict(row), indent=2))
        return 0
    print()
    print(f"  wallet     {row['address']}")
    print(f"  scanned    {report._dt(row['scanned_at'])} UTC")
    prof = strategy_mod.StrategyProfile(
        address=row["address"], archetype=row["archetype"], confidence=row["confidence"] or 0.0,
        evidence_n=row["evidence_n"] or 0,
        scores=json.loads(row["scores_json"] or "{}"),
        features=json.loads(row["features_json"] or "{}"))
    print(strategy_mod.format_profile(prof))
    return 0


def _strategy_backfill(con, args) -> int:
    n = discover.backfill_strategies(con, limit=args.limit, min_trades=args.min_trades)
    print(f"  classified {n} wallet(s) from stored trades. No requests were made.")
    print("  `strategy learn` will now have something to group by.")
    return 0


def _strategy_show(con, args) -> int:
    f, props, obs = learn.learn(con, task=args.task, write=False)
    if args.json:
        print(json.dumps({"archetypes": f.archetypes, "skips": f.skips,
                          "proposals": [p.__dict__ for p in props],
                          "observations": obs}, indent=2, default=str))
        return 0
    print()
    print(learn.format_findings(f, props, obs))
    print("\n  nothing above has been applied. `strategy learn` writes it to "
          "docs/STRATEGY_LEARNED.md.")
    return 0


def _strategy_learn(con, args) -> int:
    from ..config import LEARNED_DOC
    f, props, obs = learn.learn(con, task=args.task, write=True)
    if args.json:
        print(json.dumps({"archetypes": f.archetypes,
                          "proposals": [p.__dict__ for p in props],
                          "observations": obs}, indent=2, default=str))
        return 0
    print()
    print(learn.format_findings(f, props, obs))
    print(f"\n  written to {LEARNED_DOC}")
    print("  every proposal in it is UNAPPLIED; applying one is a human decision.")
    return 0


# --- `polywatch session` --------------------------------------------------
# The handoff between one operator and the next. `open` prints and changes nothing.


def session_dispatch(args) -> int:
    con = store.connect(args.db)
    store.init_db(con)
    return {"open": _session_open, "close": _session_close,
            "log": _session_log}[args.session_cmd](con, args)


def _session_open(con, args) -> int:
    if store.get_task(con, args.name) is None:
        print(f"no task {args.name}")
        return 1
    print()
    print(session.format_open(con, args.name))
    return 0


def _session_close(con, args) -> int:
    if store.get_task(con, args.name) is None:
        print(f"no task {args.name}")
        return 1
    from ..config import PROGRESS_DOC
    h = session.close(con, args.name, args.run_id, note=args.note or "",
                      operator=args.operator or "human")
    if args.json:
        print(json.dumps({"task": h.task, "run_id": h.run_id, "warnings": h.warnings,
                          "next_steps": h.steps}, indent=2))
        return 0
    print()
    print(session.briefing(h))
    print(f"  appended to {PROGRESS_DOC}")
    return 0


def _session_log(con, args) -> int:
    rows = store.sessions(con, args.name, limit=args.limit)
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    print()
    print(session.format_log(rows))
    return 0


def stream_requested(args) -> bool:
    """Did the operator ask for streaming explicitly? Only then is its absence worth a line."""
    return bool(getattr(args, "stream", False))


def _report(con, args) -> int:
    row = store.get_task(con, args.name)
    if row is None:
        print(f"no task {args.name}")
        return 1
    t = Task.from_row(row)
    run_id = args.run_id
    if run_id is None:
        last = store.last_run(con, args.name)
        if last is None:
            print(f"task {args.name} has never run")
            return 1
        run_id = last["id"]
    if args.json:
        out = store.run_summary(con, run_id)
        out["latency"] = store.latency_stats(con, run_id)
        out["latency_split"] = store.latency_split(con, run_id)
        out["skips"] = dict(store.skip_reasons(con, run_id))
        out["exits"] = {r[0]: {"n": r[1], "pnl": r[2]} for r in report.exit_mix(con, run_id)}
        out["by_trader"] = store.trader_pnl(con, run_id)
        print(json.dumps(out, indent=2))
        return 0
    print(report.run_report(con, run_id, t))
    mix = report.exit_mix(con, run_id)
    if mix:
        print("\n  how positions were closed")
        for reason, n, pnl in mix:
            print(f"    {reason:<14} {n:3}  ${pnl:+,.2f}")
    return 0


def _orders(con, args) -> int:
    """Resting GTC orders left behind by runs that have already stopped.

    These are real orders on a real book in live mode. Listing them is safe; --cancel is not
    reversible, so it says what it did rather than reporting a count.
    """
    rows = store.orphan_resting(con, mode="live") + list(
        store.resting_by_mode(con, "paper"))
    if not rows:
        print("no resting orders")
        return 0
    for r in rows:
        print(f"  run {r['run_id']:>4}  {r['mode']:5}  {r['shares']:8.1f} @ {r['price']:.3f}  "
              f"{r['token_id'][:16]}  {r['exchange_id'] or '(paper)'}")
    if not args.cancel:
        print("\n--cancel to cancel them")
        return 0
    live = [r for r in rows if r["mode"] == "live" and r["exchange_id"]]
    ex = execution.make("live") if live else None
    for r in rows:
        ok = ex.cancel(r["exchange_id"]) if (ex and r["exchange_id"]) else True
        store.settle_resting(con, r["id"], "cancelled" if ok else "open",
                             reason="cancelled by operator" if ok else "cancel failed")
        print(f"  {'cancelled' if ok else 'FAILED   '} {r['id']}")
    return 0
