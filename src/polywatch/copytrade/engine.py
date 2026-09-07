"""The copy-trading loop.

One trader, one run, one process. The loop is deliberately boring:

    every poll_interval seconds
        read the trader's activity since the last poll (with overlap)
        for each fill we have not seen: record it, gate it, maybe copy it
        for each position we hold: check resting orders, then the exit ladder
        check the run-level circuit breakers and the session clock

Everything the run did lands in the database as it happens -- signals whether copied or
skipped, orders whether filled or rejected, and the seen_ts/trader_ts pair on every signal that
makes copy latency a measurement rather than an assumption. A crashed run is therefore still a
readable one, which matters because the interesting runs are the ones that end badly.

What this loop can and cannot enforce, stated plainly because it decides how the bot should be
used: take-profits can be posted to the exchange as resting GTC sell orders and will fill on
their own whether or not this process is alive. Stop-losses cannot -- the CLOB has no stop order
type, so a stop is a price this loop watches and a market sell it sends. When the bot is not
running, the stop is not running. That is why a run has a session clock and flattens at the end
of it rather than leaving positions unattended.
"""

from __future__ import annotations

import signal as _signal
import time
from dataclasses import dataclass, field

from ..config import (MIN_ORDER_SHARES_FALLBACK, POLL_OVERLAP_S, TICK_SIZE_FALLBACK)
from ..db import store
from ..fetch import polymarket as api
from ..fetch.client import Client, FetchError
from ..parse import records
from . import book as bk
from . import exits
from .execution import Executor, Fill
from .task import Task

# How long a cached market_meta row is trusted. `accepting_orders` flips well before a market
# resolves, and copying into a market that has stopped accepting orders is a guaranteed reject.
META_TTL_S = 600


@dataclass
class RunState:
    """Everything the loop needs that is not worth a database round trip every tick."""
    run_id: int
    started_at: int
    start_bankroll: float
    last_seen_ts: int = 0
    # Best net exit price each open position has reached, keyed by position id. Held in memory
    # because a trailing stop only has meaning within a session -- a run that restarts has no
    # business trailing off a high water mark set before it was watching.
    high_water: dict[int, float] = field(default_factory=dict)
    trader_account_usd: float = 0.0
    polls: int = 0
    errors: int = 0
    stop_reason: str | None = None


class Engine:
    def __init__(self, con, task: Task, executor: Executor, client: Client,
                 log=print, now=lambda: int(time.time())):
        self.con = con
        self.task = task
        self.ex = executor
        self.client = client
        self.log = log
        self.now = now
        self.state: RunState | None = None
        self._stopping = False

    # --- market metadata ----------------------------------------------------------------

    def market_meta(self, condition_id: str) -> dict:
        """Tick size, minimum order size, fee rate and close time for one market.

        Cached in `market_meta` with a short TTL. Without the cache this would be one gamma
        request per signal per poll; with it, a busy trader in three markets costs three
        requests every ten minutes.
        """
        row = store.get_market_meta(self.con, condition_id)
        if row is not None and self.now() - row["fetched_at"] < META_TTL_S:
            return dict(row)
        try:
            payload = api.markets_by_condition(self.client, [condition_id])
        except FetchError as e:
            self.log(f"  ! market meta {condition_id[:10]}: {e}")
            return dict(row) if row is not None else {}
        if not payload:
            # Gamma returns only open markets unless closed=true is passed, so an empty result
            # here means the market has closed rather than that it does not exist.
            payload = api.markets_by_condition(self.client, [condition_id], closed=True)
        if not payload:
            return dict(row) if row is not None else {}
        m, _assets = records.parse_market(payload[0])
        meta = {
            "condition_id": condition_id,
            "tick_size": m["tick_size"] or TICK_SIZE_FALLBACK,
            "min_order_size": m["order_min_size"] or MIN_ORDER_SHARES_FALLBACK,
            "accepting_orders": m["accepting_orders"],
            "enable_order_book": m["enable_order_book"],
            "neg_risk": m["neg_risk"],
            "category_derived": records.derive_category(m["fee_type"], m["category"]),
            "fee_rate": m.get("fee_rate"),
            "end_ts": m["end_ts"],
        }
        store.upsert_market_meta(self.con, dict(meta))
        return meta

    def fee_rate(self, meta: dict) -> float:
        return bk.fee_rate_for(meta.get("category_derived"), meta.get("fee_rate"))

    def book(self, token_id: str) -> dict | None:
        try:
            return records.parse_book(api.book(self.client, token_id, dump_raw=False))
        except FetchError as e:
            self.log(f"  ! book {token_id[:12]}: {e}")
            return None

    # --- the loop -----------------------------------------------------------------------

    def start(self) -> RunState:
        t = self.task
        stale = store.running_runs(self.con, t.name)
        for row in stale:
            # A run left open is a run that was killed. Its positions are stale and its
            # bankroll accounting is unfinished; inheriting either silently would make the next
            # report a fiction.
            store.finish_run(self.con, row["id"], row["start_bankroll"], "abandoned")
            self.log(f"  closed abandoned run {row['id']}")
        orphans = store.orphan_resting(self.con, mode="live")
        if orphans and t.mode == "live":
            self.log(f"  ! {len(orphans)} live GTC order(s) still resting from earlier runs; "
                     f"`polywatch task orders --cancel` to clear them")
        run_id = store.start_run(self.con, t.name, t.mode, t.bankroll)
        # The watermark starts at "now", not at zero. Without it the first poll reads the
        # trader's last hundred events -- days of history -- and records every one of them as a
        # signal skipped for staleness, which buries the run's real skip histogram and drags
        # the latency percentiles into the tens of thousands of seconds. A run watches from the
        # moment it starts watching.
        self.state = RunState(run_id=run_id, started_at=self.now(), start_bankroll=t.bankroll,
                              last_seen_ts=self.now())
        self.state.trader_account_usd = self._trader_account()
        self.log(self.banner())
        return self.state

    def banner(self) -> str:
        t, s = self.task, self.state
        sl = "none" if t.sl_kind is None else f"{t.sl_kind} {t.sl_value}"
        tp = "none" if t.tp_kind is None else f"{t.tp_kind} {t.tp_value}"
        return "\n".join([
            f"run {s.run_id}  task {t.name}  mode {t.mode.upper()}",
            f"  trader        {t.trader}",
            f"  bankroll      ${t.bankroll:,.2f}   stake {t.buy_method} "
            f"${t.fixed_usd:,.2f}   max/market ${t.max_market_usd:,.2f}",
            f"  poll          {t.poll_interval_s:.0f}s   signals older than "
            f"{t.max_signal_age_s}s are skipped as stale",
            f"  session       {t.session_hours:.1f}h, then "
            f"{'flatten' if t.flatten_on_stop else 'leave positions open'}",
            f"  stop-loss     {sl}   take-profit {tp}"
            f"{'  (resting GTC order on the book)' if t.resting_tp else ''}",
            f"  trailing      {t.trail_pct:.0%}   max hold {t.max_hold_s / 60:.0f}m   "
            f"follow trader's exits: {'yes' if t.follow_exit else 'no'}",
            f"  breakers      -${t.max_daily_loss_usd:,.2f} or -{t.max_drawdown_pct:.0%}",
            "",
            "  The stop-loss is enforced by this process. The exchange has no stop order type,",
            "  so while the bot is not running, the stop is not running. The take-profit, if it",
            "  is a resting order, does not depend on the bot at all.",
        ])

    def run(self) -> dict:
        """Poll until the session ends, a breaker trips, or the operator interrupts."""
        if self.state is None:
            self.start()
        self._install_signal_handlers()
        t, s = self.task, self.state
        deadline = s.started_at + int(t.session_hours * 3600)
        try:
            while not self._stopping:
                tick = time.monotonic()
                try:
                    self.poll_signals()
                    self.manage_positions()
                except FetchError as e:
                    s.errors += 1
                    self.log(f"  ! poll failed: {e}")
                s.polls += 1

                stop = self.check_breakers()
                if stop:
                    s.stop_reason = stop
                    break
                if self.now() >= deadline:
                    s.stop_reason = "session_end"
                    break
                nap = t.poll_interval_s - (time.monotonic() - tick)
                if nap > 0:
                    time.sleep(nap)
        except KeyboardInterrupt:
            s.stop_reason = "interrupted"
        return self.stop(s.stop_reason or "stopped")

    def _install_signal_handlers(self) -> None:
        """SIGINT/SIGTERM set a flag rather than raising through the middle of a fill.

        A run killed between posting an order and writing the row is the one failure this
        program cannot reconcile afterwards, so the loop is allowed to finish its tick.
        """
        def handler(signum, frame):  # noqa: ARG001
            if self._stopping:
                raise KeyboardInterrupt
            self._stopping = True
            self.log("\n  stopping after this tick (again to force)")

        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                _signal.signal(sig, handler)
            except ValueError:
                pass  # not the main thread; the caller owns interrupts

    # --- signals ------------------------------------------------------------------------

    def poll_signals(self) -> int:
        """Read the trader's recent activity and act on anything new.

        Each poll re-reads `POLL_OVERLAP_S` before the last event we saw. The overlap is not
        redundancy for its own sake: the activity feed is served from a cache, and a fill can
        appear in it slightly after a later one. The UNIQUE constraint on `signals` makes
        re-reading free.
        """
        s = self.state
        start = max(0, s.last_seen_ts - POLL_OVERLAP_S) if s.last_seen_ts else None
        payload = api.activity(self.client, self.task.trader, start_ts=start, dump_raw=False)
        events = records.parse_activity(payload)
        seen_ts = self.now()
        acted = 0
        for ev in sorted(events, key=lambda e: e["ts"]):
            s.last_seen_ts = max(s.last_seen_ts, ev["ts"])
            if self.handle_event(ev, seen_ts):
                acted += 1
        return acted

    def handle_event(self, ev: dict, seen_ts: int) -> bool:
        """Record one observed action of the trader, and copy it if every gate passes.

        Returns True if this was the first time we saw it. The dedupe is the database's, not a
        set in memory: consecutive polls always overlap, and an in-memory set is lost on restart
        -- exactly when re-copying a stale trade would cost the most.
        """
        s, t = self.state, self.task
        if ev["kind"] != "TRADE" or not ev["side"]:
            # SPLIT / MERGE / REDEEM still get recorded: they are how a position can leave the
            # trader's book without a sell, and a report that omits them looks like the trader
            # simply stopped trading.
            return self._record(ev, seen_ts, "skipped", f"kind_{ev['kind'].lower()}") is not None

        if ev["side"] == "SELL":
            return self._handle_trader_sell(ev, seen_ts)

        held = store.open_position_for(self.con, s.run_id, ev["token_id"])
        meta = self.market_meta(ev["condition_id"])
        age = seen_ts - ev["ts"]
        secs_to_close = (meta.get("end_ts") - seen_ts) if meta.get("end_ts") else None
        reason = exits.entry_gates(
            t, price=ev["price"], age_s=age, seconds_to_close=secs_to_close,
            usd=ev["usdc_size"], accepting_orders=(
                None if meta.get("accepting_orders") is None else bool(meta["accepting_orders"])))
        if reason is None:
            reason = self._portfolio_gates(ev, held)
        if reason is not None:
            return self._record(ev, seen_ts, "skipped", reason) is not None

        sig_id = self._record(ev, seen_ts, "copied", None)
        if sig_id is None:
            return False  # already handled on an earlier poll
        self.copy_buy(ev, sig_id, meta, held)
        return True

    def _portfolio_gates(self, ev: dict, held) -> str | None:
        """Gates that depend on our book rather than on the trade."""
        s, t = self.state, self.task
        if held is None and len(store.open_positions(self.con, s.run_id)) >= t.max_concurrent:
            return "max_concurrent"
        exposure = store.market_exposure(self.con, s.run_id, ev["condition_id"])
        if exposure >= t.max_market_usd:
            return "max_market_usd"
        stake = t.stake_usd(ev["usdc_size"], s.trader_account_usd)
        if stake <= 0:
            return "stake_zero"
        if stake > store.run_cash(self.con, s.run_id):
            return "insufficient_cash"
        return None

    def _handle_trader_sell(self, ev: dict, seen_ts: int) -> bool:
        """The trader sold. If we are in that token and following their exits, so do we.

        Copying the exit matters more than copying the entry. The reason to copy a quick-flip
        trader is that they know when the move is over; our own ladder is a floor under that
        judgement, not a substitute for it.
        """
        s, t = self.state, self.task
        held = store.open_position_for(self.con, s.run_id, ev["token_id"])
        if held is None:
            return self._record(ev, seen_ts, "skipped", "not_held") is not None
        if not t.follow_exit:
            return self._record(ev, seen_ts, "skipped", "follow_exit_off") is not None
        sig_id = self._record(ev, seen_ts, "copied", None)
        if sig_id is None:
            return False
        self.close(held, exits.FOLLOW_EXIT, signal_id=sig_id)
        return True

    def _record(self, ev: dict, seen_ts: int, action: str, reason: str | None) -> int | None:
        return store.insert_signal(self.con, {
            "run_id": self.state.run_id, "trader": self.task.trader, "kind": ev["kind"],
            "tx_hash": ev["tx_hash"], "token_id": ev["token_id"],
            "condition_id": ev["condition_id"], "side": ev["side"] or None,
            "size": ev["size"], "price": ev["price"], "usdc_size": ev["usdc_size"],
            "trader_ts": ev["ts"], "seen_ts": seen_ts, "action": action, "reason": reason,
        })

    # --- execution ----------------------------------------------------------------------

    def copy_buy(self, ev: dict, signal_id: int, meta: dict, held) -> Fill | None:
        s, t = self.state, self.task
        book = self.book(ev["token_id"])
        if book is None:
            return None
        stake = min(t.stake_usd(ev["usdc_size"], s.trader_account_usd),
                    t.max_market_usd - store.market_exposure(self.con, s.run_id,
                                                             ev["condition_id"]),
                    store.run_cash(self.con, s.run_id))
        w = bk.buy_for_usd(book, stake)
        if not w.filled:
            self._log_order(signal_id, ev, "BUY", stake, 0.0,
                            Fill("rejected", reason="empty book"))
            return None
        tick = meta.get("tick_size") or TICK_SIZE_FALLBACK
        limit = bk.limit_price(w.vwap, t.slippage, tick, "BUY")
        rate = self.fee_rate(meta)
        fill = self.ex.buy(ev["token_id"], stake, book, limit=limit, tick=tick,
                           fee_rate=rate, min_shares=meta.get("min_order_size")
                           or MIN_ORDER_SHARES_FALLBACK)
        self._log_order(signal_id, ev, "BUY", stake, w.vwap, fill)
        if not fill.ok:
            self.log(f"  x buy {ev['token_id'][:10]} ${stake:.2f}: {fill.reason}")
            return fill

        if held is None:
            pos_id = store.open_position(self.con, {
                "run_id": s.run_id, "token_id": ev["token_id"],
                "condition_id": ev["condition_id"], "shares": fill.shares,
                "avg_price": fill.avg_price, "cost_usd": fill.cost, "fees_paid": fill.fee,
                "opened_ts": self.now()})
        else:
            pos_id = held["id"]
            store.add_to_position(self.con, pos_id, fill.shares, fill.cost, fill.fee)
            # The average price moved, so any resting take-profit was priced off a level that
            # no longer exists. Replaced rather than left, or the stop and the target end up
            # measured from different entries.
            self.cancel_resting(pos_id, "reprice")
        pos = self.con.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
        self.log(f"  + buy {fill.shares:.1f} @ {fill.avg_price:.3f} "
                 f"(${fill.cost:.2f} + ${fill.fee:.2f} fee)  {ev.get('title') or ''}"[:110])
        if t.resting_tp:
            self.place_take_profit(pos, meta)
        return fill

    def place_take_profit(self, pos, meta: dict) -> None:
        """Leave the take-profit on the book as a GTC sell.

        This is the Polymarket UI's "sell limit above the market" and it is the one exit that
        does not need the bot: once it is resting, it fills when the price comes to it whether
        or not this process is alive.
        """
        t = self.task
        target = t.target_price(pos["avg_price"])
        if target is None or target >= 1:
            return
        tick = meta.get("tick_size") or TICK_SIZE_FALLBACK
        r = self.ex.place_resting_sell(pos["token_id"], pos["shares"], target, tick=tick)
        store.insert_resting(self.con, {
            "run_id": self.state.run_id, "position_id": pos["id"], "mode": self.ex.mode,
            "token_id": pos["token_id"], "condition_id": pos["condition_id"], "side": "SELL",
            "shares": r.shares, "price": r.price, "kind": "take_profit",
            "exchange_id": r.exchange_id, "status": "open" if r.status == "open" else "rejected",
            "reason": r.reason})
        if r.status == "open":
            self.log(f"    resting sell {r.shares:.1f} @ {r.price:.3f} (take profit)")
        else:
            self.log(f"    ! take-profit not posted: {r.reason}")

    def cancel_resting(self, position_id: int, why: str) -> None:
        for row in store.resting_for_position(self.con, position_id):
            ok = self.ex.cancel(row["exchange_id"]) if row["exchange_id"] else True
            store.settle_resting(self.con, row["id"], "cancelled" if ok else "open",
                                 reason=why if ok else "cancel failed")

    def close(self, pos, reason: str, signal_id: int | None = None) -> Fill | None:
        """Sell out of a position at market, within the slippage bound."""
        meta = self.market_meta(pos["condition_id"])
        book = self.book(pos["token_id"])
        if book is None:
            return None
        self.cancel_resting(pos["id"], reason)
        pos = self.con.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
        w = bk.sell_shares(book, pos["shares"])
        if not w.filled:
            self.log(f"  ! cannot exit {pos['token_id'][:10]}: no bids ({reason})")
            return None
        tick = meta.get("tick_size") or TICK_SIZE_FALLBACK
        limit = bk.limit_price(w.vwap, self.task.slippage, tick, "SELL")
        rate = self.fee_rate(meta)
        fill = self.ex.sell(pos["token_id"], pos["shares"], book, limit=limit, tick=tick,
                            fee_rate=rate)
        self._log_order(signal_id, {"token_id": pos["token_id"],
                                    "condition_id": pos["condition_id"]},
                        "SELL", w.cost, w.vwap, fill, reason=reason)
        if not fill.ok:
            self.log(f"  x sell {pos['token_id'][:10]}: {fill.reason}")
            return fill
        self.book_exit(pos, fill, reason)
        return fill

    def book_exit(self, pos, fill: Fill, reason: str) -> None:
        """Write a completed exit to the position, whole or partial.

        A partial fill is left open with the remainder rather than closed at an average that
        never traded: the shares are still there, and the next tick will try again.
        """
        s = self.state
        if fill.shares < pos["shares"] - 1e-6:
            remaining = pos["shares"] - fill.shares
            frac = fill.shares / pos["shares"]
            self.con.execute(
                """UPDATE positions SET shares=?, cost_usd=cost_usd*?, fees_paid=fees_paid+?
                   WHERE id=?""",
                (remaining, 1 - frac, fill.fee, pos["id"]))
            self.con.commit()
            self.log(f"  ~ partial exit {fill.shares:.1f}/{pos['shares']:.1f} "
                     f"@ {fill.avg_price:.3f} ({reason})")
            return
        pnl = store.close_position(self.con, pos["id"], fill.cost, fill.fee, reason)
        s.high_water.pop(pos["id"], None)
        self.log(f"  - sell {fill.shares:.1f} @ {fill.avg_price:.3f}  {reason}  "
                 f"pnl ${pnl:+.2f}")

    def _log_order(self, signal_id: int | None, ev: dict, side: str, intent_usd: float,
                   vwap: float, fill: Fill, reason: str | None = None) -> int:
        return store.insert_order(self.con, {
            "run_id": self.state.run_id, "signal_id": signal_id, "mode": self.ex.mode,
            "token_id": ev["token_id"], "condition_id": ev["condition_id"], "side": side,
            "intent_usd": intent_usd, "limit_price": fill.limit_price,
            "book_vwap": vwap or fill.book_vwap, "filled_shares": fill.shares,
            "avg_price": fill.avg_price or None, "fee": fill.fee, "status": fill.status,
            "reason": reason or fill.reason, "ts": self.now()})

    # --- position management ------------------------------------------------------------

    def manage_positions(self) -> None:
        s, t = self.state, self.task
        for pos in store.open_positions(self.con, s.run_id):
            book = self.book(pos["token_id"])
            if book is None:
                continue
            meta = self.market_meta(pos["condition_id"])
            rate = self.fee_rate(meta)

            if self.check_resting(pos, book, rate):
                continue

            m = exits.mark(pos, book, rate)
            if m.net_price is not None:
                s.high_water[pos["id"]] = max(s.high_water.get(pos["id"], 0.0), m.net_price)
            d = exits.check(t, pos, book, fee_rate=rate, now=self.now(),
                            high_water=s.high_water.get(pos["id"]))
            if d.exit:
                self.log(f"  exit {pos['token_id'][:10]}: {d.reason} -- {d.detail}")
                self.close(pos, d.reason)

    def check_resting(self, pos, book: dict, rate: float) -> bool:
        """Did a resting take-profit fill? Returns True when the position is now closed."""
        for row in store.resting_for_position(self.con, pos["id"]):
            fill = self.ex.resting_fill(dict(row), book, fee_rate=rate)
            if fill is None:
                continue
            store.settle_resting(self.con, row["id"], "filled", fill.shares, fill.avg_price)
            self._log_order(None, {"token_id": pos["token_id"],
                                   "condition_id": pos["condition_id"]},
                            "SELL", fill.cost, fill.avg_price, fill, reason=exits.TAKE_PROFIT)
            self.book_exit(pos, fill, exits.TAKE_PROFIT)
            return fill.shares >= pos["shares"] - 1e-6
        return False

    def unrealized(self) -> float:
        """Mark every open position to the bid side, after fees. Used by the breakers."""
        total = 0.0
        for pos in store.open_positions(self.con, self.state.run_id):
            book = self.book(pos["token_id"])
            if book is None:
                continue
            m = exits.mark(pos, book, self.fee_rate(self.market_meta(pos["condition_id"])))
            if m.pnl is not None:
                total += m.pnl
        return total

    def check_breakers(self) -> str | None:
        s = self.state
        summary = store.run_summary(self.con, s.run_id)
        d = exits.breaker(self.task, s.start_bankroll, summary["realized_pnl"],
                          self.unrealized())
        if d.exit:
            self.log(f"  !! circuit breaker: {d.detail}")
            return exits.CIRCUIT_BREAKER
        return None

    # --- shutdown -----------------------------------------------------------------------

    def stop(self, reason: str) -> dict:
        """Close the run out. Flattens open positions unless told not to.

        Leaving positions open is allowed but is a decision, not a default: nothing watches the
        stop-loss once this returns.
        """
        s, t = self.state, self.task
        if t.flatten_on_stop:
            for pos in store.open_positions(self.con, s.run_id):
                self.close(pos, exits.SESSION_END)
        left = store.open_positions(self.con, s.run_id)
        if left:
            self.log(f"  ! {len(left)} position(s) left open -- no stop-loss is running on "
                     f"them until the next session")
            for row in store.open_resting(self.con, s.run_id):
                if row["mode"] == "paper":
                    store.settle_resting(self.con, row["id"], "cancelled", reason="run ended")
        else:
            for row in store.open_resting(self.con, s.run_id):
                ok = self.ex.cancel(row["exchange_id"]) if row["exchange_id"] else True
                store.settle_resting(self.con, row["id"], "cancelled" if ok else "open",
                                     reason="run ended")
        summary = store.run_summary(self.con, s.run_id)
        store.finish_run(self.con, s.run_id, summary["cash"], reason)
        summary["stop_reason"] = reason
        summary["latency"] = store.latency_stats(self.con, s.run_id)
        summary["polls"] = s.polls
        summary["run_id"] = s.run_id
        return summary

    # --- helpers ------------------------------------------------------------------------

    def _trader_account(self) -> float:
        from . import discover
        try:
            return discover.account_value(self.client, self.task.trader)
        except FetchError:
            return 0.0
