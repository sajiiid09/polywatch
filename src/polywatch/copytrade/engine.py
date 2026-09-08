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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..config import (ACTIVITY_PAGE, MAX_ACTIVITY_PAGES, MIN_ORDER_SHARES_FALLBACK,
                      POLL_OVERLAP_S, POLL_TIMEOUT_S, TICK_SIZE_FALLBACK)
from ..db import store
from ..fetch import polymarket as api
from ..fetch.client import Client, FetchError
from ..parse import records
from . import book as bk
from . import exits
from . import reconcile
from .execution import Executor, Fill
from .task import Task

# How long a cached market_meta row is trusted. `accepting_orders` flips well before a market
# resolves, and copying into a market that has stopped accepting orders is a guaranteed reject.
META_TTL_S = 600

# A resting sell above this never fills often enough to be worth tying the shares up in. The
# fee floor pushes targets upward at prices near 0.50, and widening one past here means the
# round trip cannot be won at that entry -- which is a reason to stop asking, not to post the
# order anyway.
MAX_RESTING_TARGET = 0.97


class ReconcileError(RuntimeError):
    """The exchange and the database disagree, so this run will not trade.

    Raised rather than returned because there is no partial version of this failure: a run that
    cannot verify what it holds has nothing safe to do next.
    """


@dataclass
class RunState:
    """Everything the loop needs that is not worth a database round trip every tick."""
    run_id: int
    started_at: int
    start_bankroll: float
    # One watermark per trader. They are polled independently, and a trader who has been quiet
    # for an hour must not have their watermark dragged forward by a busy one.
    last_seen: dict[str, int] = field(default_factory=dict)
    # Best net exit price each open position has reached, keyed by position id. Held in memory
    # because a trailing stop only has meaning within a session -- a run that restarts has no
    # business trailing off a high water mark set before it was watching.
    high_water: dict[int, float] = field(default_factory=dict)
    trader_account_usd: dict[str, float] = field(default_factory=dict)
    dropped: dict[str, str] = field(default_factory=dict)
    # Marks computed by the last sweep of manage_positions, and the tick they belong to. The
    # circuit breakers need exactly the numbers that sweep just produced; refetching every
    # position's book to recompute them doubled the request count of every tick.
    marks: dict[int, float] = field(default_factory=dict)
    marks_tick: int = -1
    polls: int = 0
    errors: int = 0
    adopted: int = 0
    truncated_polls: int = 0
    stop_reason: str | None = None


class Engine:
    def __init__(self, con, task: Task, executor: Executor, client: Client,
                 log=print, now=lambda: int(time.time()), stream=None, workers: int = 4):
        self.con = con
        self.task = task
        self.ex = executor
        self.client = client
        self.log = log
        self.now = now
        # A live book feed, or None to fetch every book over HTTP. See copytrade/stream.py.
        self.stream = stream
        self.workers = workers
        self.state: RunState | None = None
        self._stopping = False
        self._workers_client: Client | None = None

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
            "neg_risk_id": m.get("neg_risk_id"),
            "category_derived": records.derive_category(m["fee_type"], m["category"]),
            "fee_rate": m.get("fee_rate"),
            "end_ts": m["end_ts"],
        }
        store.upsert_market_meta(self.con, dict(meta))
        return meta

    def fee_rate(self, meta: dict) -> float:
        return bk.fee_rate_for(meta.get("category_derived"), meta.get("fee_rate"))

    def book(self, token_id: str) -> dict | None:
        """The current book for one token: from the stream when it is live, else fetched."""
        if self.stream is not None:
            cached = self.stream.book(token_id)
            if cached is not None:
                return cached
        try:
            return records.parse_book(api.book(self.client, token_id, dump_raw=False))
        except FetchError as e:
            self.log(f"  ! book {token_id[:12]}: {e}")
            return None

    def books(self, token_ids: list[str]) -> dict[str, dict]:
        """Several books at once. Streamed ones are free; the rest are fetched in parallel.

        The rate limiter is shared and global, so this does not issue requests any faster than
        the serial version was allowed to -- it just stops each one waiting on the last one's
        round trip before it starts.
        """
        out: dict[str, dict] = {}
        todo: list[str] = []
        for tok in dict.fromkeys(token_ids):
            cached = self.stream.book(tok) if self.stream is not None else None
            if cached is not None:
                out[tok] = cached
            else:
                todo.append(tok)
        if not todo:
            return out
        if len(todo) == 1 or self.workers <= 1:
            for tok in todo:
                b = self.book(tok)
                if b is not None:
                    out[tok] = b
            return out

        client = self._worker_client()

        def fetch(token_id: str):
            try:
                return token_id, records.parse_book(
                    api.book(client, token_id, dump_raw=False)), None
            except FetchError as e:
                return token_id, None, str(e)

        with ThreadPoolExecutor(max_workers=min(self.workers, len(todo))) as pool:
            for token_id, book, err in pool.map(fetch, todo):
                if err:
                    self.log(f"  ! book {token_id[:12]}: {err}")
                elif book is not None:
                    out[token_id] = book
        return out

    def _worker_client(self) -> Client:
        """A Client for worker threads: shares the rate limiter, never touches SQLite.

        The connection belongs to the main thread. Workers that logged to it would be writing to
        sqlite from a thread that does not own it, which is the kind of bug that shows up once a
        week in production and never in a test.
        """
        if self._workers_client is None:
            src = self.client
            self._workers_client = Client(
                con=None, dump_raw=False, log_ok=False, buffer_logs=True,
                limiter=getattr(src, "limiter", None),
                timeout=getattr(src, "timeout", POLL_TIMEOUT_S))
        return self._workers_client

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
        now = self.now()
        self.state = RunState(run_id=run_id, started_at=now, start_bankroll=t.bankroll,
                              last_seen={a: now for a in t.traders})
        store.set_task_traders(self.con, t.name, [{"address": a} for a in t.traders])
        self.state.trader_account_usd = {a: self._trader_account(a) for a in t.traders}
        self.reconcile("start")
        self.log(self.banner())
        return self.state

    # --- reconciliation -----------------------------------------------------------------

    def reconcile(self, when: str) -> None:
        """Make the database agree with the exchange, or refuse to trade.

        Paper mode has no exchange to disagree with, so this is a no-op there. Live, it is the
        gate between believing we hold something and knowing it -- see `RULES.md` L5.
        """
        s = self.state
        resolutions, problems = reconcile.check(self.con, s.run_id, self.ex)
        for r in resolutions:
            self._adopt(r)
        if not problems:
            return
        self.log(reconcile.describe(problems))
        store.finish_run(self.con, s.run_id, s.start_bankroll, "reconcile_failed")
        s.stop_reason = "reconcile_failed"
        raise ReconcileError(f"reconciliation failed at {when}: "
                             f"{len(problems)} discrepancy(ies)")

    def _adopt(self, r: reconcile.Resolution) -> None:
        """Book what an order turned out to have done, once the exchange has said so."""
        s = self.state
        if r.shares <= 0:
            store.resolve_order(self.con, r.order_id, "rejected", 0.0, None, 0.0,
                                "reconciled: nothing matched")
            self.log(f"  reconciled order {r.order_id}: nothing matched")
            return

        meta = self.market_meta(r.condition_id)
        fee = bk.fee(r.shares, r.avg_price, self.fee_rate(meta))
        store.resolve_order(self.con, r.order_id, "filled", r.shares, r.avg_price, fee,
                            f"reconciled via {r.source}")
        if r.side != "BUY":
            # A sell we could not confirm is settled by the shortfall check rather than here:
            # the shares are simply gone from both views, and `close` already wrote the exit.
            self.log(f"  reconciled sell {r.order_id}: {r.shares:.1f} @ {r.avg_price:.3f}")
            return

        held = store.open_position_for(self.con, s.run_id, r.token_id)
        cost = r.shares * r.avg_price
        if held is None:
            store.open_position(self.con, {
                "run_id": s.run_id, "token_id": r.token_id, "condition_id": r.condition_id,
                "shares": r.shares, "avg_price": r.avg_price, "cost_usd": cost,
                "fees_paid": fee, "opened_ts": self.now()})
        else:
            store.add_to_position(self.con, held["id"], r.shares, cost, fee)
        s.adopted += 1
        self.log(f"  adopted {r.shares:.1f} shares @ {r.avg_price:.3f} from order "
                 f"{r.order_id} ({r.source})")

    def banner(self) -> str:
        t, s = self.task, self.state
        sl = ("none" if t.sl_kind is None else
              (f"-{t.sl_value:.0%} off entry" if t.sl_kind == "pct" else f"at {t.sl_value:.3f}"))
        tp = ("none -- exits follow the trader, the trail and the stop" if t.tp_kind is None else
              (f"+{t.tp_value:.0%} off entry" if t.tp_kind == "pct" else f"at {t.tp_value:.3f}"))
        return "\n".join([
            f"run {s.run_id}  task {t.name}  mode {t.mode.upper()}",
            (f"  trader        {t.trader}" if len(t.traders) == 1 else
             f"  traders       {len(t.traders)}, up to ${t.per_trader_cap:,.2f} each\n"
             + "\n".join(f"                {a}" for a in t.traders)),
            f"  bankroll      ${t.bankroll:,.2f}   stake {t.buy_method} "
            f"${t.fixed_usd:,.2f}   max/market ${t.max_market_usd:,.2f}",
            f"  poll          {t.poll_interval_s:.0f}s   signals older than "
            f"{t.max_signal_age_s}s are skipped as stale",
            f"  session       {t.session_hours:.1f}h, then "
            f"{'flatten' if t.flatten_on_stop else 'leave positions open'}",
            f"  stop-loss     {sl}   take-profit {tp}"
            f"{'  (resting GTC order on the book)' if t.resting_tp and t.tp_kind else ''}",
            f"  trailing      {t.trail_pct:.0%}   max hold {t.max_hold_s / 60:.0f}m   "
            f"follow trader's exits: {'yes' if t.follow_exit else 'no'}",
            f"  breakers      -${t.max_daily_loss_usd:,.2f} or -{t.max_drawdown_pct:.0%}",
            "",
            f"  exits         checked {'on every book update (streamed)' if self.stream is not None and self.stream.live else f'once every {t.poll_interval_s:.0f}s (polled)'}",
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

                # An order the exchange accepted without saying what it matched is a question,
                # and the loop does not get to ask another one until it is answered.
                if store.unknown_orders(self.con, s.run_id):
                    self.reconcile("tick")

                stop = self.check_breakers()
                if stop:
                    s.stop_reason = stop
                    break
                if self.now() >= deadline:
                    s.stop_reason = "session_end"
                    break
                nap = t.poll_interval_s - (time.monotonic() - tick)
                if nap > 0:
                    self._wait(nap)
        except ReconcileError:
            # Already logged, and `reconcile` closed the run row. Deliberately not routed through
            # `stop()`: flattening means selling, and selling is the thing we have just decided
            # we cannot safely do.
            return self._summary("reconcile_failed")
        except KeyboardInterrupt:
            s.stop_reason = "interrupted"
        return self.stop(s.stop_reason or "stopped")

    def _wait(self, seconds: float) -> None:
        """Wait for the next poll -- re-checking the exits every time the book moves.

        Without a stream this is a sleep, and the exit ladder runs once per poll interval. Since
        the stop-loss, the trailing stop and the time stop are enforced by this process and by
        nothing else, that interval is the resolution of every protection the run has: a
        fifteen-second gap in a market that moves in seconds is a fifteen-second option written
        against us for free.

        With a stream, the wait ends early whenever a held book changes, the ladder runs, and
        the wait resumes for whatever is left of the interval. Signals are still gathered on the
        poll tick, because a third party's fills cannot be streamed at all.
        """
        if self.stream is None or not self.stream.live:
            time.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while not self._stopping:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            if not self.stream.changed.wait(timeout=left):
                return
            self.stream.changed.clear()
            try:
                self.manage_positions()
            except FetchError as e:
                self.state.errors += 1
                self.log(f"  ! exit check failed: {e}")

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

    def read_activity(self, trader: str, start: int | None) -> tuple[list[dict], int, bool]:
        """Every event since `start`, paged out. Returns (events, fetch_ts, truncated).

        The feed is newest-first and one page is 100 events, so a single page is not a read of
        "everything since the watermark" -- it is a read of the most recent hundred things. A
        trader who did more than that between polls, or a poll after any stall, silently lost the
        remainder while the watermark advanced past it. Nothing recorded that it had happened.

        So: page until a page comes back short, or until it reaches back past the watermark, or
        until the page cap. Hitting the cap is `truncated`, which the caller counts as a run
        error rather than swallowing.
        """
        out: list[dict] = []
        for page in range(MAX_ACTIVITY_PAGES):
            payload = api.activity(self.client, trader, offset=page * ACTIVITY_PAGE,
                                   start_ts=start, dump_raw=False)
            rows = records.parse_activity(payload)
            out.extend(rows)
            if len(rows) < ACTIVITY_PAGE:
                return out, self.now(), False
            if start is not None and min(r["ts"] for r in rows) <= start:
                return out, self.now(), False
        return out, self.now(), True

    def poll_signals(self) -> int:
        """Read the trader's recent activity and act on anything new.

        Each poll re-reads `POLL_OVERLAP_S` before the last event we saw. The overlap is not
        redundancy for its own sake: the activity feed is served from a cache, and a fill can
        appear in it slightly after a later one. The UNIQUE constraint on `signals` makes
        re-reading free.

        The watermark moves behind the loop, not ahead of it. Advancing it before handling an
        event meant that a failure partway through a page left the watermark past events that
        were never processed, and the next poll would never look at them again. Events are
        handled oldest-first and the watermark follows the last one that actually succeeded, so
        a failure costs a re-read rather than a fill.
        """
        return sum(self.poll_trader(a) for a in self.task.traders)

    def poll_trader(self, trader: str) -> int:
        """One trader's feed, one watermark, one pass."""
        s = self.state
        prev = s.last_seen.get(trader, 0)
        start = max(0, prev - POLL_OVERLAP_S) if prev else None
        events, fetch_ts, truncated = self.read_activity(trader, start)
        if truncated:
            s.errors += 1
            s.truncated_polls += 1
            self.log(f"  ! {trader[:10]}: activity read hit the {MAX_ACTIVITY_PAGES}-page cap; "
                     f"some events were not read this poll")

        acted = 0
        watermark = prev
        for ev in sorted(events, key=lambda e: e["ts"]):
            try:
                if self.handle_event(ev, self.now(), fetch_ts, trader=trader):
                    acted += 1
            except FetchError:
                raise
            except Exception as e:  # noqa: BLE001 - one bad event must not skip the rest forever
                s.errors += 1
                self.log(f"  ! event {ev.get('tx_hash', '')[:12]} failed: "
                         f"{type(e).__name__}: {e}")
                break
            watermark = max(watermark, ev["ts"])
        s.last_seen[trader] = watermark
        return acted

    def handle_event(self, ev: dict, seen_ts: int, fetch_ts: int | None = None,
                     trader: str | None = None) -> bool:
        """Record one observed action of the trader, and copy it if every gate passes.

        Returns True if this was the first time we saw it. The dedupe is the database's, not a
        set in memory: consecutive polls always overlap, and an in-memory set is lost on restart
        -- exactly when re-copying a stale trade would cost the most.
        """
        s, t = self.state, self.task
        who = (trader or ev.get("wallet") or t.trader).lower()
        if ev["kind"] != "TRADE" or not ev["side"]:
            # SPLIT / MERGE / REDEEM still get recorded: they are how a position can leave the
            # trader's book without a sell, and a report that omits them looks like the trader
            # simply stopped trading.
            return self._record(ev, seen_ts, "skipped", f"kind_{ev['kind'].lower()}",
                                fetch_ts, who) is not None

        if ev["side"] == "SELL":
            return self._handle_trader_sell(ev, seen_ts, fetch_ts, who)

        if who in s.dropped:
            return self._record(ev, seen_ts, "skipped", "trader_dropped", fetch_ts,
                                who) is not None

        held = store.open_position_for(self.con, s.run_id, ev["token_id"])
        meta = self.market_meta(ev["condition_id"])
        age = seen_ts - ev["ts"]
        secs_to_close = (meta.get("end_ts") - seen_ts) if meta.get("end_ts") else None
        reason = exits.entry_gates(
            t, price=ev["price"], age_s=age, seconds_to_close=secs_to_close,
            usd=ev["usdc_size"], fee_rate=self.fee_rate(meta), accepting_orders=(
                None if meta.get("accepting_orders") is None else bool(meta["accepting_orders"])))
        if reason is None:
            reason = self._portfolio_gates(ev, held, meta, who)

        # The book is the last gate and the only one that costs a request, so it runs last --
        # but it runs before the signal is recorded, so illiquidity appears in the skip histogram
        # instead of as a rejected order that nothing reports on.
        book = None
        if reason is None:
            book = self.book(ev["token_id"])
            reason = exits.book_gates(t, book, usd=self._stake_for(ev, meta, who))

        if reason is not None:
            return self._record(ev, seen_ts, "skipped", reason, fetch_ts, who) is not None

        sig_id = self._record(ev, seen_ts, "copied", None, fetch_ts, who)
        if sig_id is None:
            return False  # already handled on an earlier poll
        self.copy_buy(ev, sig_id, meta, held, book, who)
        return True

    def _stake_for(self, ev: dict, meta: dict, trader: str) -> float:
        """What we would actually spend on this trade, after every cap that applies.

        One expression, called from the gate and from the order, because the two disagreeing is
        how a trade passes a check on a number it is not going to use.
        """
        s, t = self.state, self.task
        return min(t.stake_usd(ev["usdc_size"], s.trader_account_usd.get(trader, 0.0)),
                   t.max_market_usd - self.exposure(ev["condition_id"], meta),
                   t.per_trader_cap - store.trader_exposure(self.con, s.run_id, trader),
                   store.run_cash(self.con, s.run_id))

    def exposure(self, condition_id: str, meta: dict) -> float:
        """Open cost across one market -- or across a whole neg-risk event, when it is one.

        In a neg-risk market the outcomes are mutually exclusive slices of one question, so three
        `condition_id`s can be three ways of holding the same view. Capping each separately caps
        nothing.
        """
        s = self.state
        group = meta.get("neg_risk_id")
        if group:
            return store.event_exposure(self.con, s.run_id, group)
        return store.market_exposure(self.con, s.run_id, condition_id)

    def _portfolio_gates(self, ev: dict, held, meta: dict, trader: str) -> str | None:
        """Gates that depend on our book rather than on the trade."""
        s, t = self.state, self.task
        # One trader may not take the whole account. This is the cap that makes copying several
        # of them a diversification rather than a race to spend the bankroll first.
        if store.trader_exposure(self.con, s.run_id, trader) >= t.per_trader_cap:
            return "max_trader_usd"
        # Buying the other outcome of a market we are already in pays two entry fees and two
        # exit fees to arrive at roughly no exposure. It is not a hedge, it is a round trip with
        # the profit removed, so it is refused rather than sized down.
        if held is None and store.open_position_other_outcome(
                self.con, s.run_id, ev["condition_id"], ev["token_id"]) is not None:
            return "holds_other_outcome"
        if held is None and len(store.open_positions(self.con, s.run_id)) >= t.max_concurrent:
            return "max_concurrent"
        if self.exposure(ev["condition_id"], meta) >= t.max_market_usd:
            return "max_market_usd"
        if t.stake_usd(ev["usdc_size"], s.trader_account_usd.get(trader, 0.0)) <= 0:
            return "stake_zero"
        if store.run_cash(self.con, s.run_id) <= 0:
            return "insufficient_cash"
        # The stake after every cap, not before: a trade can clear the cash and exposure checks
        # and still be squeezed under the market's minimum order size by them. That used to
        # produce a rejected order and no skip reason, so the histogram never saw it.
        stake = self._stake_for(ev, meta, trader)
        if stake <= 0:
            return "insufficient_cash"
        min_shares = meta.get("min_order_size") or MIN_ORDER_SHARES_FALLBACK
        if ev["price"] > 0 and stake / ev["price"] < min_shares:
            return "below_min_size"
        return None

    def _handle_trader_sell(self, ev: dict, seen_ts: int, fetch_ts: int | None = None,
                            trader: str | None = None) -> bool:
        """The trader sold. If we are in that token and following their exits, so do we.

        Copying the exit matters more than copying the entry. The reason to copy a quick-flip
        trader is that they know when the move is over; our own ladder is a floor under that
        judgement, not a substitute for it.

        We sell the *fraction* of our position that they sold of theirs, not all of it. Scaling
        out of a winner in thirds is an ordinary thing to do, and answering it by dumping the
        whole position copies neither their entry nor their exit -- it just leaves the trade
        early and pays a full exit fee for the privilege.

        Their position size is only known as far as this run has watched them, which is a floor
        (see `store.trader_observed_shares`). When that floor says they held no more than they
        just sold, the fraction is 1 and we close outright -- the same behaviour as before, now
        reached because it is right rather than by default.
        """
        s, t = self.state, self.task
        who = (trader or t.trader).lower()
        held = store.open_position_for(self.con, s.run_id, ev["token_id"])
        if held is None:
            return self._record(ev, seen_ts, "skipped", "not_held", fetch_ts, who) is not None
        if not t.follow_exit:
            return self._record(ev, seen_ts, "skipped", "follow_exit_off", fetch_ts,
                                who) is not None
        # A position belongs to the trader whose signal opened it, and only they get to close it.
        # Following B out of a position A put us into would attribute A's loss to B's judgement,
        # and would exit a trade on the opinion of someone who never took it.
        owner = (held["trader"] or t.trader).lower()
        if owner != who:
            return self._record(ev, seen_ts, "skipped", "position_owned_by_other", fetch_ts,
                                who) is not None

        # Computed before the sell is recorded, so their own sale is not counted against them.
        theirs = store.trader_observed_shares(self.con, s.run_id, who, ev["token_id"],
                                              before_ts=ev["ts"])
        sig_id = self._record(ev, seen_ts, "copied", None, fetch_ts, who)
        if sig_id is None:
            return False
        frac = 1.0 if theirs <= 0 else min(1.0, (ev["size"] or 0.0) / theirs)
        shares = held["shares"] if frac >= 1.0 else held["shares"] * frac
        if frac < 1.0:
            self.log(f"  trader sold {frac:.0%} of their position; matching it")
        self.close(held, exits.FOLLOW_EXIT, signal_id=sig_id, shares=shares)
        return True

    def _record(self, ev: dict, seen_ts: int, action: str, reason: str | None,
                fetch_ts: int | None = None, trader: str | None = None) -> int | None:
        return store.insert_signal(self.con, {
            "run_id": self.state.run_id,
            "trader": (trader or self.task.trader).lower(), "kind": ev["kind"],
            "tx_hash": ev["tx_hash"], "token_id": ev["token_id"],
            "condition_id": ev["condition_id"], "side": ev["side"] or None,
            "size": ev["size"], "price": ev["price"], "usdc_size": ev["usdc_size"],
            "trader_ts": ev["ts"], "fetch_ts": fetch_ts or seen_ts, "seen_ts": seen_ts,
            "action": action, "reason": reason,
        })

    # --- execution ----------------------------------------------------------------------

    def copy_buy(self, ev: dict, signal_id: int, meta: dict, held,
                 book: dict | None = None, trader: str | None = None) -> Fill | None:
        s, t = self.state, self.task
        trader = (trader or t.trader).lower()
        # The gate already paid for this book. Re-fetching it would cost a request and, worse,
        # would decide on a book different from the one the trade was approved against.
        book = book if book is not None else self.book(ev["token_id"])
        if book is None:
            return None
        stake = self._stake_for(ev, meta, trader)
        w = bk.buy_for_usd(book, stake)
        if not w.filled:
            self._log_order(signal_id, ev, "BUY", stake, 0.0,
                            Fill("rejected", reason="empty book"), trader=trader)
            return None
        tick = meta.get("tick_size") or TICK_SIZE_FALLBACK
        limit = bk.limit_price(bk.best_ask(book) or w.vwap, t.slippage, tick, "BUY")
        rate = self.fee_rate(meta)
        fill = self.ex.buy(ev["token_id"], stake, book, limit=limit, tick=tick,
                           fee_rate=rate, min_shares=meta.get("min_order_size")
                           or MIN_ORDER_SHARES_FALLBACK)
        self._log_order(signal_id, ev, "BUY", stake, w.vwap, fill, trader=trader)
        if not fill.ok:
            self.log(f"  x buy {ev['token_id'][:10]} ${stake:.2f}: {fill.reason}")
            return fill

        if held is None:
            pos_id = store.open_position(self.con, {
                "run_id": s.run_id, "trader": trader, "token_id": ev["token_id"],
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
        if self.stream is not None:
            self.stream.watch([ev["token_id"]])
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
        rate = self.fee_rate(meta)
        target = t.target_price(pos["avg_price"], rate)
        if target is None or target >= 1:
            return
        # A target that had to be widened past what the market can reach is not a take-profit,
        # it is an order that will never fill while pinning the shares. Say so and let the
        # trailing stop and the trader's own exit carry the position instead.
        if target > MAX_RESTING_TARGET:
            self.log(f"    take-profit at {target:.3f} is out of reach after fees; "
                     f"relying on the trailing stop and the trader's exit")
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

    def close(self, pos, reason: str, signal_id: int | None = None,
              shares: float | None = None) -> Fill | None:
        """Sell out of a position at market, within the slippage bound.

        `shares` sells only part of it, which is what following a trader who scales out looks
        like. The resting take-profit is cancelled either way: it was priced against a position
        of a different size, and leaving it would sell shares the exit ladder no longer knows
        about.
        """
        meta = self.market_meta(pos["condition_id"])
        book = self.book(pos["token_id"])
        if book is None:
            return None
        self.cancel_resting(pos["id"], reason)
        pos = self.con.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
        want = pos["shares"] if shares is None else min(shares, pos["shares"])
        if want <= 0:
            return None
        w = bk.sell_shares(book, want)
        if not w.filled:
            self.log(f"  ! cannot exit {pos['token_id'][:10]}: no bids ({reason})")
            return None
        tick = meta.get("tick_size") or TICK_SIZE_FALLBACK
        limit = bk.limit_price(bk.best_bid(book) or w.vwap, self.task.slippage, tick, "SELL")
        rate = self.fee_rate(meta)
        fill = self.ex.sell(pos["token_id"], want, book, limit=limit, tick=tick,
                            fee_rate=rate)
        self._log_order(signal_id, {"token_id": pos["token_id"],
                                    "condition_id": pos["condition_id"]},
                        "SELL", w.cost, w.vwap, fill, reason=reason, trader=pos["trader"])
        if not fill.ok:
            self.log(f"  x sell {pos['token_id'][:10]}: {fill.reason}")
            return fill
        self.book_exit(pos, fill, reason)
        return fill

    def book_exit(self, pos, fill: Fill, reason: str) -> None:
        """Write a completed exit to the position, whole or partial.

        A partial fill is left open with the remainder rather than closed at an average that
        never traded: the shares are still there, and the next tick will try again. The
        arithmetic of splitting cost and fees across the two halves lives in
        `store.settle_position`, which is also what keeps the remainder's stop-loss honest.
        """
        s = self.state
        pnl, closed = store.settle_position(self.con, pos["id"], fill.shares, fill.cost,
                                            fill.fee, reason)
        if not closed:
            self.log(f"  ~ partial exit {fill.shares:.1f}/{pos['shares']:.1f} "
                     f"@ {fill.avg_price:.3f} ({reason})  pnl ${pnl:+.2f}")
            return
        s.high_water.pop(pos["id"], None)
        self.log(f"  - sell {fill.shares:.1f} @ {fill.avg_price:.3f}  {reason}  "
                 f"pnl ${pnl:+.2f}")

    def _log_order(self, signal_id: int | None, ev: dict, side: str, intent_usd: float,
                   vwap: float, fill: Fill, reason: str | None = None,
                   trader: str | None = None) -> int:
        return store.insert_order(self.con, {
            "run_id": self.state.run_id, "signal_id": signal_id, "trader": trader,
            "mode": self.ex.mode,
            "token_id": ev["token_id"], "condition_id": ev["condition_id"], "side": side,
            "intent_usd": intent_usd, "limit_price": fill.limit_price,
            "book_vwap": vwap or fill.book_vwap, "filled_shares": fill.shares,
            "avg_price": fill.avg_price or None, "fee": fill.fee, "status": fill.status,
            "exchange_id": fill.exchange_id,
            "reason": reason or fill.reason, "ts": self.now()})

    # --- position management ------------------------------------------------------------

    def manage_positions(self) -> None:
        """Walk the open book once: check resting orders, then the exit ladder.

        The books are fetched together rather than one after another. Every request is a round
        trip under a global rate limit, so a serial sweep makes the tick -- and therefore the
        interval between stop-loss checks -- grow with the number of positions held. That is
        exactly backwards: the more exposure a run has, the faster it should be looking at it.
        """
        s, t = self.state, self.task
        positions = store.open_positions(self.con, s.run_id)
        books = self.books([p["token_id"] for p in positions])
        marks: dict[int, float] = {}
        for pos in positions:
            book = books.get(pos["token_id"])
            if book is None:
                continue
            meta = self.market_meta(pos["condition_id"])
            rate = self.fee_rate(meta)

            if self.check_resting(pos, book, rate):
                continue

            m = exits.mark(pos, book, rate)
            if m.net_price is not None:
                s.high_water[pos["id"]] = max(s.high_water.get(pos["id"], 0.0), m.net_price)
            if m.pnl is not None:
                marks[pos["id"]] = m.pnl
            d = exits.check(t, pos, book, fee_rate=rate, now=self.now(),
                            high_water=s.high_water.get(pos["id"]))
            if d.exit:
                self.log(f"  exit {pos['token_id'][:10]}: {d.reason} -- {d.detail}")
                self.close(pos, d.reason)
                marks.pop(pos["id"], None)
        s.marks, s.marks_tick = marks, s.polls

    def check_resting(self, pos, book: dict, rate: float) -> bool:
        """Did a resting take-profit fill? Returns True when the position is now closed.

        A resting order can fill in pieces, and a piece is not a settlement: the order stays on
        the book for the remainder and only its `filled_shares` moves.
        """
        for row in store.resting_for_position(self.con, pos["id"]):
            fill = self.ex.resting_fill(dict(row), book, fee_rate=rate)
            if fill is None:
                continue
            whole = fill.shares >= row["shares"] - 1e-6
            store.settle_resting(self.con, row["id"], "filled" if whole else "open",
                                 fill.shares, fill.avg_price,
                                 reason=None if whole else "partially filled")
            self._log_order(None, {"token_id": pos["token_id"],
                                   "condition_id": pos["condition_id"]},
                            "SELL", fill.cost, fill.avg_price, fill, reason=exits.TAKE_PROFIT,
                            trader=pos["trader"])
            self.book_exit(pos, fill, exits.TAKE_PROFIT)
            return fill.shares >= pos["shares"] - 1e-6
        return False

    def unrealized(self) -> float:
        """Mark every open position to the bid side, after fees. Used by the breakers.

        Reuses the marks `manage_positions` computed on this same tick. They are the same books
        and the same arithmetic, so fetching them again cost a second round trip per position to
        arrive at a number we already had -- and delayed the breaker check by exactly that long.
        """
        s = self.state
        positions = store.open_positions(self.con, s.run_id)
        if s.marks_tick == s.polls:
            return sum(s.marks.get(p["id"], 0.0) for p in positions)

        total = 0.0
        books = self.books([p["token_id"] for p in positions])
        for pos in positions:
            book = books.get(pos["token_id"])
            if book is None:
                continue
            m = exits.mark(pos, book, self.fee_rate(self.market_meta(pos["condition_id"])))
            if m.pnl is not None:
                total += m.pnl
        return total

    def check_breakers(self) -> str | None:
        s = self.state
        self.check_trader_drops()
        summary = store.run_summary(self.con, s.run_id)
        d = exits.breaker(self.task, s.start_bankroll, summary["realized_pnl"],
                          self.unrealized())
        if d.exit:
            self.log(f"  !! circuit breaker: {d.detail}")
            return exits.CIRCUIT_BREAKER
        return None

    def check_trader_drops(self) -> list[str]:
        """Stop copying a trader whose signals have lost too much within this run.

        The whole reason to copy several wallets is that one of them will stop working, and the
        run should be able to notice that without ending. Their open positions stay under the
        exit ladder: this drops their advice, not their trades.

        Measured on realized PnL only, deliberately. Unrealized loss is what the stop-loss and
        the trailing stop are for, and dropping a trader over a position that has not resolved
        would fire on noise every time a market moved against an open copy.
        """
        s, t = self.state, self.task
        if t.auto_drop_usd <= 0:
            return []
        dropped = []
        for addr in t.traders:
            if addr in s.dropped:
                continue
            pnl = store.trader_realized(self.con, s.run_id, addr)
            if pnl <= -t.auto_drop_usd:
                reason = f"down ${-pnl:,.2f} on realized trades, limit ${t.auto_drop_usd:,.2f}"
                s.dropped[addr] = reason
                store.drop_task_trader(self.con, t.name, addr, reason)
                self.log(f"  !! no longer copying {addr[:12]}: {reason}")
                dropped.append(addr)
        return dropped

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
        summary = self._summary(reason)
        # The run ends on equity, not cash. `cash` subtracts what open positions tie up and adds
        # nothing back for what they are worth, so a run that deliberately left positions open
        # would otherwise report their entire cost basis as a loss that never happened.
        store.finish_run(self.con, s.run_id, summary["equity"], reason)
        return summary

    def _summary(self, reason: str) -> dict:
        s = self.state
        summary = store.run_summary(self.con, s.run_id)
        summary["unrealized_pnl"] = self.unrealized() if summary["positions_open"] else 0.0
        # Cash is what is not tied up; equity is cash plus what the open positions would fetch.
        # Both are reported because they answer different questions -- what can still be staked,
        # and what the account is worth.
        summary["equity"] = summary["cash"] + summary["unrealized_pnl"] + sum(
            (p["cost_usd"] or 0.0) + (p["fees_paid"] or 0.0)
            for p in store.open_positions(self.con, s.run_id))
        summary["stop_reason"] = reason
        summary["latency"] = store.latency_stats(self.con, s.run_id)
        summary["polls"] = s.polls
        summary["adopted"] = s.adopted
        summary["run_id"] = s.run_id
        summary["by_trader"] = store.trader_pnl(self.con, s.run_id)
        summary["dropped"] = dict(s.dropped)
        return summary

    # --- helpers ------------------------------------------------------------------------

    def _trader_account(self, address: str) -> float:
        from . import discover
        try:
            return discover.account_value(self.client, address)
        except FetchError:
            return 0.0
