"""The exit ladder: when to get out of a position, and why.

Pure functions over (task, position, book, clock). No network, no database, no side effects --
which is what makes the ladder testable without a market, and what keeps the engine's loop
readable as "fetch, decide, execute".

The rungs, in the order they are checked:

  1. stop_loss    -- the price fell through the floor. Checked first and unconditionally,
                     because every other rung is an opinion and this one is the budget.
  2. take_profit  -- the target was reached. Skipped here when `resting_tp` is on, since in that
                     case the target is a live GTC order on the book rather than a check.
  3. trailing     -- gave back too much of the best price this position ever saw.
  4. max_hold     -- a quick flip that has not worked in 45 minutes is not going to.
  5. no_bid       -- nothing to sell into. Not an exit; a warning that the exits are notional.

Entry is gated in two passes, `entry_gates` then `book_gates`, split by what they cost: the first
reads numbers we already have, the second needs the order book. Both run before a signal is
recorded as copied, so every refusal lands in the skip histogram.

Every threshold is measured against the *net* exit price -- what the bid side would actually
pay after the taker fee -- not the mid, and not the last trade. A stop measured on the mid is a
stop that does not fire until the loss is already worse than it says.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import book as bk

# Rung names, also used as `positions.close_reason` and in the run report. Kept as constants so
# a typo in one branch cannot silently create a new category in the report.
STOP_LOSS = "stop_loss"
TAKE_PROFIT = "take_profit"
TRAILING = "trailing"
MAX_HOLD = "max_hold"
FOLLOW_EXIT = "follow_exit"
SESSION_END = "session_end"
CIRCUIT_BREAKER = "circuit_breaker"
MARKET_CLOSING = "market_closing"


@dataclass
class Mark:
    """A position marked to the book it would actually be sold into."""
    net_price: float | None      # per-share proceeds after fee; None when there is no bid
    net_value: float | None      # the whole position's exit value
    pnl: float | None            # against cost plus fees already paid
    pnl_pct: float | None


def mark(position, book: dict, fee_rate: float) -> Mark:
    shares = position["shares"]
    proceeds, exit_fee, net = bk.net_exit_value(book, shares, fee_rate)
    if proceeds <= 0:
        return Mark(None, None, None, None)
    cost = position["cost_usd"] + (position["fees_paid"] or 0.0)
    return Mark(net / shares, net, net - cost, (net - cost) / cost if cost > 0 else None)


@dataclass
class Decision:
    exit: bool
    reason: str | None = None
    detail: str = ""


def check(task, position, book: dict, *, fee_rate: float, now: int,
          high_water: float | None = None) -> Decision:
    """Should this position be closed right now?

    `high_water` is the best net exit price the position has seen, which the caller tracks --
    a trailing stop is the one rung that depends on history rather than on the current book.
    """
    m = mark(position, book, fee_rate)
    if m.net_price is None:
        return Decision(False, None, "no bid: exits are notional until liquidity returns")

    avg = position["avg_price"]
    stop = task.stop_price(avg)
    if stop is not None and m.net_price <= stop:
        return Decision(True, STOP_LOSS, f"net {m.net_price:.3f} <= stop {stop:.3f}")

    target = task.target_price(avg, fee_rate)
    if target is not None and not task.resting_tp and m.net_price >= target:
        return Decision(True, TAKE_PROFIT, f"net {m.net_price:.3f} >= target {target:.3f}")

    if task.trail_pct > 0 and high_water is not None:
        trail = high_water * (1 - task.trail_pct)
        # Only trails once the position has been in profit; otherwise the trailing stop is just
        # a second, tighter stop-loss measured off the entry, which is not what it is for.
        if high_water > avg and m.net_price <= trail:
            return Decision(True, TRAILING,
                            f"net {m.net_price:.3f} <= {task.trail_pct:.0%} off high "
                            f"{high_water:.3f}")

    if task.max_hold_s > 0 and now - position["opened_ts"] >= task.max_hold_s:
        held = (now - position["opened_ts"]) / 60
        return Decision(True, MAX_HOLD, f"held {held:.0f}m, limit {task.max_hold_s / 60:.0f}m")

    return Decision(False, None, f"net {m.net_price:.3f}, pnl {m.pnl:+.2f}")


def breaker(task, start_bankroll: float, realized_pnl: float, unrealized_pnl: float
            ) -> Decision:
    """Run-level circuit breakers: stop the whole session, not one position.

    Both are measured on realized plus unrealized PnL. Measuring the drawdown on realized PnL
    alone produces a bot that sits calmly through a total loss because it has not sold yet.
    """
    total = realized_pnl + unrealized_pnl
    if task.max_daily_loss_usd > 0 and total <= -task.max_daily_loss_usd:
        return Decision(True, CIRCUIT_BREAKER,
                        f"down ${-total:,.2f}, limit ${task.max_daily_loss_usd:,.2f}")
    if task.max_drawdown_pct > 0 and start_bankroll > 0:
        dd = -total / start_bankroll
        if dd >= task.max_drawdown_pct:
            return Decision(True, CIRCUIT_BREAKER,
                            f"drawdown {dd:.1%}, limit {task.max_drawdown_pct:.0%}")
    return Decision(False)


def book_gates(task, book: dict | None, *, usd: float) -> str | None:
    """Why not to copy this trade, given the book we would actually have to buy from.

    Separate from `entry_gates` because it costs a request, so it runs last -- but it runs
    *before* the signal is recorded as copied, which is the point. Discovering illiquidity inside
    the executor produced a rejected order and no skip reason, so the histogram that a paper run
    exists to produce could not see the most common reason a copy is not worth making.

    Three refusals, in the order a trade dies of them:

      no_book     -- nothing quoted, or one side missing entirely.
      wide_spread -- crossing costs more than `max_spread_frac` of the mid. This is the same kind
                     of cost as the fee floor and is measured in the same unit, so the two can be
                     read against each other.
      thin_book   -- the ask side cannot supply our stake, or cannot meet `min_depth_usd`. Buying
                     what a book cannot sell means paying through it on the way in and finding
                     nobody there on the way out.
    """
    if not book or not book.get("asks") or not book.get("bids"):
        return "no_book"
    sf = bk.spread_frac(book)
    if sf is None:
        return "no_book"
    if task.max_spread_frac > 0 and sf > task.max_spread_frac:
        return "wide_spread"
    depth = bk.buy_for_usd(book, max(usd, task.min_depth_usd))
    if depth.exhausted or depth.cost < max(usd, task.min_depth_usd) - 1e-6:
        return "thin_book"
    return None


def entry_gates(task, *, price: float, age_s: int, seconds_to_close: int | None,
                usd: float, accepting_orders: bool | None,
                fee_rate: float | None = None) -> str | None:
    """Why not to copy this trade. Returns a skip reason, or None to proceed.

    The reasons are deliberately coarse and few: they become the histogram that `task report`
    prints, and that histogram is the main finding of a paper run. 'stale' dominating means the
    trader is too fast to copy; 'price_band' dominating means their entries sit where the book
    is thin; 'fee_floor' dominating means the round trip costs more than the exit rule can win
    back, which is a verdict on the market rather than on the trader.
    """
    if age_s > task.max_signal_age_s:
        return "stale"
    if usd < task.min_trade_usd:
        return "trade_too_small"
    if not (task.min_price <= price <= task.max_price):
        return "price_band"
    if fee_rate is not None:
        # The fee alone, before anything is won or lost. Near 0.50 in an expensive category it
        # can exceed a tenth of the stake, and no exit rule recovers that.
        if task.max_fee_frac > 0 and \
                bk.round_trip_fee_frac(price, fee_rate) > task.max_fee_frac:
            return "fee_floor"
        # And the configured target has to be able to clear it. Under 'widen' the target moves
        # instead, so there is nothing to refuse.
        if task.tp_fee_policy == "skip" and not task.target_is_viable(price, fee_rate):
            return "fee_floor"
    if accepting_orders is False:
        return "not_accepting_orders"
    if seconds_to_close is not None and seconds_to_close < task.min_seconds_to_close:
        return "market_closing"
    return None
