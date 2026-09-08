"""The `polywatch task` subcommands. Argument shuffling only -- the decisions live in
task.py, engine.py and report.py.

The one piece of real behaviour here is the live-mode confirmation. Everything else in this
program can be re-run; posting a signed order cannot.
"""

from __future__ import annotations

import json
import os

from ..config import ENV_FUNDER, ENV_PRIVATE_KEY, MAX_RPS, POLL_TIMEOUT_S
from ..db import store
from ..fetch.client import Client
from . import book as bk
from . import execution, report
from .engine import Engine
from .task import PRESETS, Task, preset


def dispatch(args) -> int:
    con = store.connect(args.db)
    store.init_db(con)
    return {
        "create": _create, "list": _list, "show": _show, "rm": _rm,
        "run": _run, "report": _report, "orders": _orders,
    }[args.task_cmd](con, args)


def _build(args) -> Task:
    """Flags -> Task. Unset flags fall through to the preset, then to the dataclass default."""
    over: dict = {}
    if args.mirror:
        over["buy_method"] = "mirror"
    for flag, field_ in (("max_market_usd", "max_market_usd"), ("max_concurrent", "max_concurrent"),
                         ("slippage", "slippage"), ("trail", "trail_pct"),
                         ("poll", "poll_interval_s"), ("session_hours", "session_hours"),
                         ("max_loss", "max_daily_loss_usd"),
                         ("max_drawdown", "max_drawdown_pct"), ("tp_policy", "tp_fee_policy"),
                         ("min_edge", "min_edge"), ("max_fee_frac", "max_fee_frac")):
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
    return Task(name=args.name, trader=args.trader, bankroll=args.bankroll,
                fixed_usd=args.stake, **preset(args.preset, **over))


def _create(con, args) -> int:
    t = _build(args)
    store.upsert_task(con, t.as_row())
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
    return "\n".join(x for x in [
        f"  trader        {t.trader}",
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
    print(f"{'name':16} {'mode':6} {'trader':14} {'bankroll':>9} {'stake':>8} {'hold':12} runs")
    for r in rows:
        n = con.execute("SELECT COUNT(*) FROM task_runs WHERE task=?", (r["name"],)).fetchone()[0]
        print(f"{r['name'][:16]:16} {r['mode']:6} {r['trader'][:14]:14} "
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
LIVE MODE. This will sign orders with the key in ${key} and spend real USDC from
{funder}.

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
    print(LIVE_WARNING.format(key=ENV_PRIVATE_KEY, funder=os.environ.get(ENV_FUNDER, "(unset)"),
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

    if t.mode == "live" and not args.yes and not _confirm_live(t):
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
    eng = Engine(con, t, ex, client)
    summary = eng.run()
    print()
    print(report.run_report(con, summary["run_id"], t))
    return 0


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
        out["skips"] = dict(store.skip_reasons(con, run_id))
        out["exits"] = {r[0]: {"n": r[1], "pnl": r[2]} for r in report.exit_mix(con, run_id)}
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
    rows = store.orphan_resting(con, mode="live") + [
        r for r in con.execute(
            "SELECT * FROM resting_orders WHERE status='open' AND mode='paper'")]
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
