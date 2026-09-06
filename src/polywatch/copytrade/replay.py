"""Historical replay: what $100 would have done copying one wallet.

This is the backtest. It reads the target's trades straight out of the `trades` table -- 71,886
of them are already ingested, across 29 screened wallets -- converts each into the same signal
shape the live `/activity` poller will produce, and drives them through the real `Engine`. The
only thing it invents is the fill price.

Why a replay rather than a live paper run: measured against the ingested data, the screened
human wallets trade between 0.08 and 0.84 times per twenty minutes. A twenty-minute live run
therefore observes zero or one copyable trade, and no market resolves in that window, so
whatever it reported would be unrealized noise. Four months of settled history is the shortest
horizon on which copy trading has a measurable answer.

The fill model, stated plainly because it is the largest assumption in the result:

  We cannot retrieve historical order books. /book returns the book now, never the book at 3pm
  four months ago. So our entry price is the target's own fill price, made worse by a flat
  penalty, and that single number stands in for both the book walk we cannot perform and the
  latency between their fill and ours. It is an assumption, not a measurement, which is why the
  CLI sweeps it across 0%, 3% and 7% instead of quoting one figure.

Settlement is not modelled -- it is read. A resolved market prices its winning token at exactly
1 and the rest at 0, and `markets.winning_index` tells us which. 55,174 of the 56,305 trades by
screened wallets sit in markets we can settle this way, so no extra network calls are needed.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from ..config import TICK_SIZE_FALLBACK
from ..db import store
from . import fees, gates
from .engine import Engine, Fill


@dataclass
class ReplayFills:
    """Fills priced off the target's own fill, worsened by `slippage`.

    Fees come out of the same budget as the shares rather than being charged on top. Sizing
    already reserved exactly `usd` against the bankroll, so a fee added afterwards would spend
    money the cash calculation never set aside -- and the overspend would compound across a run
    until the account quietly went negative.
    """
    slippage: float = 0.0
    tick: float = TICK_SIZE_FALLBACK

    def buy(self, sig: dict, usd: float, market) -> Fill:
        raw = sig["price"]
        price = min(raw * (1.0 + self.slippage), 1.0 - self.tick)
        if price <= 0:
            return Fill(0.0, 0.0, 0.0, price, raw, "rejected", "non_positive_price")

        # fee is linear in shares, so the budget splits exactly rather than by iteration:
        #   shares * price + rate * shares * min(p,1-p)**e == usd
        rate, exponent, _ = fees.fee_params(market)
        if _fees_off(market):
            rate = 0.0
        per_share_fee = rate * (min(price, 1.0 - price) ** exponent)
        shares = usd / (price + per_share_fee)
        if shares <= 0:
            return Fill(0.0, 0.0, 0.0, price, raw, "rejected", "zero_shares")
        return Fill(shares, price, shares * per_share_fee, price, raw, "filled", None)

    def sell(self, token_id: str, shares: float, price_hint: float, ts: int, market) -> Fill:
        price = max(price_hint * (1.0 - self.slippage), self.tick)
        fee = fees.taker_fee(shares, price, market)
        return Fill(shares, price, fee, price, price_hint, "filled", None)


def _fees_off(market) -> bool:
    try:
        return market is not None and market["fees_enabled"] in (0, False)
    except (KeyError, IndexError, TypeError):
        return False


def signal_from_trade(row) -> dict:
    """A `trades` row in the shape `records.parse_activity` returns.

    Deliberately the same dict the live poller will hand the engine, so the engine has no idea
    which one it is serving and the backtest cannot drift away from the thing it validates.
    `usdc_size` is derived here because /trades does not carry it while /activity does.
    """
    return {
        "wallet": row["wallet"],
        "kind": "TRADE",
        "token_id": row["token_id"],
        "condition_id": row["condition_id"],
        "side": row["side"],
        "size": row["size"],
        "price": row["price"],
        "usdc_size": (row["size"] or 0.0) * (row["price"] or 0.0),
        "ts": row["ts"],
        "outcome": row["outcome"],
        "outcome_index": row["outcome_index"],
        "tx_hash": row["tx_hash"] or "",
        "title": None,
        "slug": None,
    }


def run(con, task, slippage: float, since_ts: int = 0, until_ts: int | None = None) -> dict:
    """Replay one wallet's history through the engine. Returns the run summary.

    Settlements are interleaved with trades rather than applied at the end, which is not a
    detail at a $100 bankroll: a resolved position returns its money, and the next trade can
    only be afforded because the previous one paid out. Applying settlements last would model
    an account that never recycles capital, and would understate a small bankroll badly.
    """
    trades = store.trader_trades(con, task.trader, since_ts, until_ts)
    if not trades:
        raise ValueError(f"no ingested trades for {task.trader} -- run `polywatch ingest` first")

    started = trades[0]["ts"]
    # Persisted here rather than by the caller: task_runs.task is a foreign key onto tasks.name,
    # so a caller that forgot would get an opaque FOREIGN KEY failure from three frames down.
    store.upsert_task(con, task.to_row())
    run_id = store.start_run(con, task.name, task.mode, task.bankroll, started_at=started)

    # Loaded once, before the engine: the entry gate closes over it.
    last_trade = store.last_trade_ts_by_condition(con)
    score = store.get_trader_score(con, task.trader)
    trader_account = score["est_account_usd"] if score else None
    engine = Engine(con, run_id, task, ReplayFills(slippage), trader_account,
                    extra_gates=(gates.replay_can_settle(last_trade),))

    # (settle_ts, token_id) for every market we hold, popped as the replay clock passes it.
    pending: list[tuple[int, str]] = []
    scheduled: set[str] = set()
    settled_unknown = 0

    for row in trades:
        now = row["ts"]
        _drain(engine, pending, scheduled, con, until_ts=now)
        sig = signal_from_trade(row)
        engine.handle(sig)
        # Schedule this token's settlement the first time we take a position in it.
        if row["token_id"] not in scheduled:
            pos = store.open_position_for(con, run_id, row["token_id"])
            if pos is not None:
                info = store.settlement(con, row["token_id"])
                if info and info["won"] is not None:
                    # The entry gate has already guaranteed this estimate lands after the fill.
                    est = max(int(info["end_ts"]), last_trade.get(row["condition_id"], 0))
                    heapq.heappush(pending, (est, row["token_id"]))
                    scheduled.add(row["token_id"])
                elif info:
                    settled_unknown += 1

    last_ts = trades[-1]["ts"]
    _drain(engine, pending, scheduled, con, until_ts=None, fallback_ts=last_ts)

    summary = store.run_summary(con, run_id)
    store.finish_run(con, run_id, summary["cash"], "replay_complete", stopped_at=last_ts)

    # Positions still open are ones whose markets never resolved in our data. They are reported
    # at cost rather than marked: only 10% of trades have a price quote within five minutes, so
    # any mark for the other 90% would be invented, and inventing marks is how a backtest ends
    # up flattering itself.
    open_cost = sum(p["cost_usd"] + p["fees_paid"] for p in store.open_positions(con, run_id))
    closed = store.closed_positions_for(con, run_id)
    wins = sum(1 for p in closed if (p["realized_pnl"] or 0.0) > 0)
    summary.update({
        "run_id": run_id,
        "trader": task.trader,
        "slippage": slippage,
        "start_ts": started,
        "end_ts": last_ts,
        "start_bankroll": task.bankroll,
        "open_cost": open_cost,
        "final_value_at_cost": summary["cash"] + open_cost,
        "settled_unknown_outcome": settled_unknown,
        "wins": wins,
        "fee_sources": store.fee_source_split(con, run_id),
        "skips": store.skip_reasons(con, run_id),
    })
    return summary


def _drain(engine: Engine, pending: list, scheduled: set, con, until_ts: int | None,
           fallback_ts: int | None = None) -> None:
    """Settle every scheduled market whose estimated resolution time has passed.

    `fallback_ts` dates the undated sentinel on the final pass, so those positions close at the
    end of the run instead of carrying a timestamp from the far future.
    """
    while pending and (until_ts is None or pending[0][0] <= until_ts):
        settle_ts, token_id = heapq.heappop(pending)
        scheduled.discard(token_id)
        info = store.settlement(con, token_id)
        if info is None or info["won"] is None:
            continue
        if fallback_ts is not None and settle_ts > fallback_ts:
            settle_ts = fallback_ts
        engine.settle(token_id, settle_ts, bool(info["won"]))


def format_report(s: dict, task) -> str:
    """The backtest score card."""
    start = s["start_bankroll"]
    final = s["final_value_at_cost"]
    pnl = final - start
    days = max((s["end_ts"] - s["start_ts"]) / 86400.0, 1e-9)
    guessed = s["fee_sources"].get("fallback", 0.0)
    total_fee = sum(s["fee_sources"].values()) or 0.0
    hit = _hit_rate(s)

    lines = [
        f"  trader            {s['trader']}",
        f"  window            {_day(s['start_ts'])} -> {_day(s['end_ts'])}  ({days:.0f} days)",
        f"  sizing            {task.buy_method}"
        + (f" ${task.fixed_usd:,.2f}/trade" if task.buy_method == "fixed" else "")
        + f", up to {task.max_concurrent} at once",
        f"  capital deployed  {_deployed(task):.0%} of bankroll when fully committed",
        f"  slippage assumed  {s['slippage']:.0%}",
        "",
        f"  start             ${start:,.2f}",
        f"  end               ${final:,.2f}   (${s['cash']:,.2f} cash"
        + (f" + ${s['open_cost']:,.2f} still open at cost)" if s["open_cost"] else ")"),
        f"  profit            ${pnl:+,.2f}   ({pnl / start:+.1%})",
        f"  fees paid         ${total_fee:,.2f}"
        + (f"   ({guessed / total_fee:.0%} of it from guessed rates)" if total_fee else ""),
        "",
        f"  signals seen      {s['signals_seen']:,}",
        f"  copied            {s['copied']:,}",
        f"  skipped           {s['skipped']:,}",
        f"  positions closed  {s['positions_closed']:,}"
        + (f"   ({hit:.1%} profitable)" if hit is not None else ""),
        f"  positions open    {s['positions_open']:,}",
    ]
    if s["settled_unknown_outcome"]:
        lines.append(f"  unsettleable      {s['settled_unknown_outcome']} markets resolved but "
                     "with no known winning outcome")
    if s["skips"]:
        lines += ["", "  why trades were skipped"]
        for reason, n in s["skips"][:12]:
            lines.append(f"    {n:>6,}  {reason}")
    note = _affordability_note(s, task)
    if note:
        lines += ["", note]
    return "\n".join(lines)


def _affordability_note(s: dict, task) -> str | None:
    """Warn when the stake is too small to buy anything but longshots.

    Polymarket markets have a minimum order of about 5 shares, so a stake of $S can only enter
    outcomes priced at or below S/5. At $2 a trade that is $0.40 -- which quietly turns any
    strategy into an underdog-only one, whatever the trader being copied actually does. It
    shows up as a healthy-looking profit on a poor-looking hit rate, and it is a property of
    the bankroll rather than of the trader, so the report has to say it out loud.
    """
    top = s["skips"][0][0] if s["skips"] else None
    if top != "below_min_order_size" or task.buy_method != "fixed":
        return None
    ceiling = (task.fixed_usd or 0.0) / 5.0
    if ceiling >= 0.95:
        return None
    return (f"  note: at ${task.fixed_usd:,.2f} a trade the 5-share market minimum puts every\n"
            f"  outcome priced above ${ceiling:.2f} out of reach, so this run could only copy\n"
            f"  their cheaper bets. That is a limit of the bankroll, not of the trader.")


def _deployed(task) -> float:
    """The share of the account at risk with every slot filled. The single most decisive number
    in a backtest of a small bankroll -- at 100% a losing streak is terminal, because there is
    no reserve left to trade the recovery with."""
    stake = task.fixed_usd if task.buy_method == "fixed" else task.bankroll * 0.10
    return min((stake or 0.0) * task.max_concurrent / task.bankroll, 1.0)


def _hit_rate(s: dict) -> float | None:
    """Share of closed positions that made money. Counted on PnL, not on whether the outcome
    won -- the same distinction `skill.win_rate` draws, and for the same reason: buying a
    winner at 0.97 and paying fees twice is a loss."""
    return None if not s["positions_closed"] else s["wins"] / s["positions_closed"]


def _day(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
