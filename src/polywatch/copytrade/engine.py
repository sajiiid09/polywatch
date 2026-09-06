"""The copy-trading core: signal in, position out.

This module holds every decision that is the same whether the trades arrive from a historical
replay or from a live poller. What differs between those two -- where the fill price comes from
-- is behind the `Fills` protocol, and nothing else here knows which it is talking to.

That split is the point. A backtest that ran different logic from the live bot would validate
nothing, so the backtest drives the real engine and only lies about the order book. It also
means the live poller in Phase 2b is a new event source and a new Fills implementation, not a
second copy of the rules.

The engine never calls `time.time()`. Every timestamp arrives from the caller, because in a
replay "now" is four months ago and a clock read anywhere in here would quietly stamp historical
events with today's date.

Everything is persisted as it happens rather than accumulated in memory: a run is meant to be
reconstructable from the database alone, including the trades we declined and why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import MIN_POSITION_USD
from ..db import store
from . import fees, gates, sizing


@dataclass
class Fill:
    """What actually happened when we tried to trade. `shares` of 0 means nothing filled."""
    shares: float
    avg_price: float
    fee: float
    limit_price: float
    book_vwap: float | None = None
    status: str = "filled"
    reason: str | None = None


class Fills(Protocol):
    """Where fill prices come from. Implemented by ReplayFills now, LiveFills in Phase 2b."""

    def buy(self, sig: dict, usd: float, market) -> Fill: ...

    def sell(self, token_id: str, shares: float, price_hint: float, ts: int, market) -> Fill: ...


class Engine:
    def __init__(self, con, run_id: int, task, fills: Fills,
                 trader_account: float | None = None, extra_gates=()):
        self.con = con
        self.run_id = run_id
        self.task = task
        self.fills = fills
        self.trader_account = trader_account
        # Gates only one driver can ask -- see gates.check. The replay adds settleability, the
        # live poller will add accepting_orders.
        self.extra_gates = tuple(extra_gates)

    # --- entry points ---------------------------------------------------------------------

    def handle(self, sig: dict) -> None:
        """One observed action of the target."""
        kind = (sig.get("kind") or "TRADE").upper()
        side = (sig.get("side") or "").upper()
        if kind != "TRADE":
            # REDEEM / MERGE / SPLIT change their book without being a trade we can mirror.
            self._record(sig, "skipped", f"not_a_trade:{kind.lower()}")
            return
        if side == "BUY":
            self._entry(sig)
        elif side == "SELL":
            self._exit(sig)
        else:
            self._record(sig, "skipped", "trade_without_side")

    def settle(self, token_id: str, ts: int, won: bool) -> float | None:
        """Close an open position because its market resolved.

        Settlement pays no fee: redeeming a winning share is not a taker trade, it is a
        redemption at face value. Charging a fee here would make hold-to-resolution look worse
        than it is, which is exactly the comparison the backtest exists to get right.
        """
        pos = store.open_position_for(self.con, self.run_id, token_id)
        if pos is None:
            return None
        proceeds = pos["shares"] * (1.0 if won else 0.0)
        return store.close_position(self.con, pos["id"], proceeds, 0.0,
                                    "settled_win" if won else "settled_loss", closed_ts=ts)

    def force_close(self, token_id: str, price: float, ts: int, reason: str) -> float | None:
        """Mark a still-open position out at `price`. Used at end of run, never mid-run."""
        pos = store.open_position_for(self.con, self.run_id, token_id)
        if pos is None:
            return None
        market = store.get_market(self.con, pos["condition_id"])
        fee = fees.taker_fee(pos["shares"], price, market)
        return store.close_position(self.con, pos["id"], pos["shares"] * price, fee, reason,
                                    closed_ts=ts)

    # --- the two halves of a copy ----------------------------------------------------------

    def _entry(self, sig: dict) -> None:
        market = store.get_market(self.con, sig["condition_id"])

        ok, reason = gates.check(self.con, self.run_id, self.task, sig, market,
                                 self.extra_gates)
        if not ok:
            self._record(sig, "skipped", reason)
            return

        usd, reason = sizing.size_usd(self.con, self.run_id, self.task, sig, market,
                                      self.trader_account)
        if reason:
            self._record(sig, "skipped", reason)
            return

        signal_id = self._record(sig, "copied", None)
        fill = self.fills.buy(sig, usd, market)
        self._log_order(sig, signal_id, "BUY", usd, fill)
        if fill.shares <= 0:
            return

        cost = fill.shares * fill.avg_price
        existing = store.open_position_for(self.con, self.run_id, sig["token_id"])
        if existing is None:
            store.open_position(self.con, {
                "run_id": self.run_id, "token_id": sig["token_id"],
                "condition_id": sig["condition_id"], "shares": fill.shares,
                "avg_price": fill.avg_price, "cost_usd": cost, "fees_paid": fill.fee,
                "opened_ts": sig["ts"],
            })
        else:
            store.add_to_position(self.con, existing["id"], fill.shares, cost, fill.fee)

    def _exit(self, sig: dict) -> None:
        """Mirror the target selling.

        How much of our position to sell is the awkward part: we see the number of shares they
        sold, not what fraction of their holding that was. So we reconstruct their position in
        this token from the signals we have already recorded -- every buy of theirs we saw,
        minus every sell -- and apply the same fraction to ours.

        When that reconstruction comes out empty, they are selling something they accumulated
        before this run started watching, and we have no basis for a fraction. In that case we
        exit fully. Holding on would be the worse error: our position exists only because we
        were copying them, and they are no longer in it.
        """
        if self.task.behavior == "buys":
            self._record(sig, "skipped", "sell_ignored_by_behavior")
            return

        pos = store.open_position_for(self.con, self.run_id, sig["token_id"])
        if pos is None:
            self._record(sig, "skipped", "no_position_to_exit")
            return

        theirs = self._trader_shares(sig["token_id"])
        fraction = 1.0 if theirs <= 0 else min(sig.get("size", 0.0) / theirs, 1.0)
        if fraction <= 0:
            self._record(sig, "skipped", "sell_of_zero_size")
            return

        signal_id = self._record(sig, "copied", None)
        shares = pos["shares"] * fraction
        fill = self.fills.sell(sig["token_id"], shares, sig["price"], sig["ts"],
                               store.get_market(self.con, sig["condition_id"]))
        self._log_order(sig, signal_id, "SELL", shares * sig["price"], fill)
        if fill.shares <= 0:
            return

        proceeds = fill.shares * fill.avg_price
        remaining_value = (pos["shares"] - fill.shares) * fill.avg_price
        # A sliver left behind is worth less than the fee to close it later, and it would keep
        # occupying a max_concurrent slot forever. Round it into this exit instead.
        if fraction >= 1.0 or remaining_value < MIN_POSITION_USD:
            store.close_position(self.con, pos["id"], proceeds, fill.fee, "mirror_sell",
                                 closed_ts=sig["ts"])
        else:
            store.reduce_position(self.con, pos["id"], fill.shares / pos["shares"], proceeds,
                                  fill.fee, "mirror_sell_partial", sig["ts"])

    # --- bookkeeping ----------------------------------------------------------------------

    def _trader_shares(self, token_id: str) -> float:
        """The target's own position in one token, rebuilt from the signals we recorded.

        Counts every signal, copied or skipped: what they hold does not depend on whether we
        chose to follow them into it.
        """
        row = self.con.execute(
            """SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN size
                                        WHEN side='SELL' THEN -size ELSE 0 END), 0)
               FROM signals WHERE run_id=? AND token_id=?""",
            (self.run_id, token_id),
        ).fetchone()
        return float(row[0])

    def _record(self, sig: dict, action: str, reason: str | None) -> int | None:
        return store.insert_signal(self.con, {
            "run_id": self.run_id, "trader": self.task.trader,
            "kind": (sig.get("kind") or "TRADE").upper(), "tx_hash": sig.get("tx_hash") or "",
            "token_id": sig["token_id"], "condition_id": sig["condition_id"],
            "side": sig.get("side"), "size": sig.get("size"), "price": sig.get("price"),
            "usdc_size": sig.get("usdc_size"), "trader_ts": sig["ts"],
            # In a replay we see the trade at the instant it happened, so latency is zero here
            # and the slippage penalty carries it instead. The live poller stamps a real
            # seen_ts, and the gap between the two columns is the copy latency.
            "seen_ts": sig.get("seen_ts", sig["ts"]),
            "action": action, "reason": reason,
        })

    def _log_order(self, sig: dict, signal_id: int | None, side: str, intent_usd: float,
                   fill: Fill) -> None:
        store.insert_order(self.con, {
            "run_id": self.run_id, "signal_id": signal_id, "mode": self.task.mode,
            "token_id": sig["token_id"], "condition_id": sig["condition_id"], "side": side,
            "intent_usd": intent_usd, "limit_price": fill.limit_price,
            "book_vwap": fill.book_vwap, "filled_shares": fill.shares,
            "avg_price": fill.avg_price, "fee": fill.fee, "status": fill.status,
            "reason": fill.reason, "ts": sig["ts"],
        })
