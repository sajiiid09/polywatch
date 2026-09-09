"""What a run actually did, read back out of the database.

Two audiences. The skip histogram and the latency table are for tuning: they say whether the
poll interval is right, whether the gates are too tight, and whether this trader can be copied
at all. The PnL and fee lines are for the only question that matters -- whether the edge
survives the round trip.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..db import store
from . import exits


def _dt(ts: int | None) -> str:
    return "-" if not ts else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def latency_block(stats: dict, poll_interval_s: float, split: dict | None = None) -> list[str]:
    """Measured copy latency: seen_ts - trader_ts, per signal.

    Read against the poll interval. If p90 exceeds the staleness gate, most of what this trader
    does is uncopyable at this cadence whatever the median says.

    The split is the actionable half. `feed` is how stale data-api's answer already was when it
    reached us, which polling faster cannot fix and which is a fact about the trader's
    copyability rather than about this program. `loop` is ours, and is the only part worth
    tuning `POLL_INTERVAL_S` against.
    """
    if not stats.get("n"):
        return ["  latency        no signals observed"]
    out = [f"  latency        n={stats['n']}  p50 {stats['p50']}s  p90 {stats['p90']}s  "
           f"p99 {stats['p99']}s  max {stats['max']}s  (poll {poll_interval_s:.0f}s)"]
    if split and split.get("n"):
        out.append(f"                 of which  feed {split['feed']:.1f}s  "
                   f"loop {split['loop']:.1f}s  on average")
        if split["feed"] > split["loop"] * 2 and split["feed"] > 2:
            out.append("                 the feed is the bottleneck, not the loop -- a shorter "
                       "poll interval will not help")
    return out


def run_report(con, run_id: int, task=None) -> str:
    run = store.get_run(con, run_id)
    if run is None:
        raise KeyError(f"no run {run_id}")
    s = store.run_summary(con, run_id)
    poll = task.poll_interval_s if task is not None else 0.0

    lines = [
        f"run {run_id}  task {run['task']}  {run['mode'].upper()}",
        f"  started        {_dt(run['started_at'])}   stopped {_dt(run['stopped_at'])}"
        f"   ({run['stop_reason'] or 'running'})",
        # `end_bankroll` is equity, written when the run closed out. Falling back to cash for a
        # run still in flight is right: nothing has marked its open positions yet.
        f"  bankroll       ${run['start_bankroll']:,.2f} -> "
        f"${(run['end_bankroll'] if run['end_bankroll'] is not None else s['cash']):,.2f}"
        + ("" if not s["positions_open"] else
           f"   (${s['cash']:,.2f} cash, {s['positions_open']} position(s) still open)"),
        f"  realized pnl   ${s['realized_pnl']:+,.2f}   fees ${s['fees_paid']:,.2f}",
        f"  signals        {s['signals_seen']} seen, {s['copied']} copied, "
        f"{s['skipped']} skipped",
        f"  positions      {s['positions_closed']} closed, {s['positions_open']} still open",
    ]
    lines += latency_block(store.latency_stats(con, run_id), poll,
                           store.latency_split(con, run_id))

    if s["realized_pnl"] and s["fees_paid"]:
        gross = s["realized_pnl"] + s["fees_paid"]
        lines.append(f"  fee drag       ${s['fees_paid']:,.2f} on ${gross:+,.2f} gross "
                     f"({s['fees_paid'] / abs(gross):.0%} of it)" if gross else "")

    skips = store.skip_reasons(con, run_id)
    if skips:
        lines += ["", "  why trades were skipped"]
        width = max(len(r or "?") for r, _ in skips)
        for reason, n in skips:
            lines.append(f"    {(reason or '?'):<{width}}  {n}")

    closed = store.closed_positions_for_run(con, run_id)
    if closed:
        lines += ["", "  closed positions",
                  f"    {'token':<12} {'shares':>8} {'entry':>7} {'pnl':>9} {'held':>7}  why"]
        for p in closed:
            held = (p["closed_ts"] - p["opened_ts"]) / 60 if p["closed_ts"] else 0
            lines.append(f"    {p['token_id'][:12]:<12} {p['shares']:8.1f} "
                         f"{p['avg_price']:7.3f} {p['realized_pnl']:+9.2f} {held:6.0f}m  "
                         f"{p['close_reason'] or ''}")

    lines += trader_block(con, run_id, task)

    rest = store.open_resting(con, run_id)
    if rest:
        lines += ["", f"  {len(rest)} resting sell order(s) still on the book:"]
        for r in rest:
            lines.append(f"    {r['shares']:.1f} @ {r['price']:.3f}  {r['token_id'][:12]}"
                         f"  {r['exchange_id'] or '(paper)'}")
    return "\n".join(x for x in lines if x != "")


def trader_block(con, run_id: int, task=None) -> list[str]:
    """Per-trader attribution. The reason a portfolio run is worth running.

    Copying several wallets on one bankroll is only diversification if a bad one can be
    identified afterwards. Without this the run reports a single number and the wallet that lost
    the money hides inside it.
    """
    by = store.trader_pnl(con, run_id)
    if len(by) <= 1:
        return []
    dropped = {}
    if task is not None:
        dropped = {r["address"]: r["dropped_reason"]
                   for r in store.task_traders(con, task.name) if not r["active"]}
    lines = ["", "  by trader",
             f"    {'wallet':<14} {'signals':>7} {'copied':>6} {'pos':>4} {'open':>5} "
             f"{'pnl':>9} {'fees':>7}"]
    for addr, d in sorted(by.items(), key=lambda kv: -kv[1]["realized_pnl"]):
        lines.append(f"    {addr[:14]:<14} {d['signals']:7} {d['copied']:6} "
                     f"{d['positions']:4} {d['open']:5} {d['realized_pnl']:+9.2f} "
                     f"{d['fees']:7.2f}"
                     + (f"   DROPPED: {dropped[addr]}" if addr in dropped else ""))
    return lines


def exit_mix(con, run_id: int) -> list[tuple[str, int, float]]:
    """Which rung of the ladder closed positions, and what each one earned.

    The shape to look for: take_profit and follow_exit carrying the PnL, stop_loss bounded and
    infrequent, max_hold near zero. A run where max_hold dominates is one where the trader's
    edge is slower than the task assumes it is.
    """
    rows = store.close_reason_mix(con, run_id)
    return [(r[0] or exits.SESSION_END, int(r[1]), float(r[2])) for r in rows]
