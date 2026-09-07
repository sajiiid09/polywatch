"""Phase 2: the trading loop -- sizing, the exit ladder, resting orders, latency.

The engine is exercised end to end against a fake Polymarket: a dict of books and an activity
feed the test controls. Nothing here touches the network, and paper mode signs nothing, so the
whole file runs offline in milliseconds.
"""

from __future__ import annotations

import sqlite3

import pytest

from polywatch.copytrade import book as bk
from polywatch.copytrade import exits
from polywatch.copytrade.engine import Engine
from polywatch.copytrade.execution import PaperExecutor
from polywatch.copytrade.task import Task, preset
from polywatch.db import store


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init_db(c)
    return c


def book_of(bids, asks) -> dict:
    return {"token_id": "tok", "condition_id": "cond",
            "bids": sorted(bids, key=lambda l: -l[0]),
            "asks": sorted(asks, key=lambda l: l[0])}


# --- book arithmetic ------------------------------------------------------


def test_walking_the_book_pays_up_through_the_levels():
    """$20 does not buy 40 shares at 0.50 when only 10 are offered there."""
    b = book_of([], [(0.50, 10), (0.55, 100)])
    w = bk.buy_for_usd(b, 20)
    assert w.shares == pytest.approx(10 + (20 - 5) / 0.55)
    assert w.vwap > 0.50 and w.worst_price == 0.55


def test_a_thin_book_reports_itself_exhausted_rather_than_filling():
    w = bk.buy_for_usd(book_of([], [(0.50, 10)]), 100)
    assert w.exhausted and w.shares == 10


def test_tick_rounding_moves_in_the_direction_that_preserves_the_intent():
    """A buy limit rounded down can sit under the level it meant to reach and never fill."""
    assert bk.round_to_tick(0.5034, 0.01, side="BUY") == 0.51
    assert bk.round_to_tick(0.5034, 0.01, side="SELL") == 0.50


def test_fees_are_largest_at_even_odds_and_vanish_at_the_extremes():
    assert bk.fee(100, 0.50, 0.05) > bk.fee(100, 0.90, 0.05) > bk.fee(100, 0.98, 0.05)


# --- sizing ---------------------------------------------------------------


def test_fixed_sizing_ignores_what_the_trader_staked():
    t = Task(name="a", trader="0x1", buy_method="fixed", fixed_usd=10)
    assert t.stake_usd(5000, 100_000) == 10


def test_mirror_sizing_scales_their_conviction_onto_our_bankroll():
    t = Task(name="a", trader="0x1", buy_method="mirror", bankroll=200, mirror_max_frac=0.5,
             max_market_usd=1000)
    assert t.stake_usd(1000, 10_000) == pytest.approx(20)   # 10% of theirs -> 10% of ours


def test_mirror_sizing_is_capped_because_value_understates_their_account():
    """/value reports open positions only, so the fraction it implies is an overstatement."""
    t = Task(name="a", trader="0x1", buy_method="mirror", bankroll=100, mirror_max_frac=0.10,
             max_market_usd=1000)
    assert t.stake_usd(900, 1000) == pytest.approx(10)


# --- the exit ladder ------------------------------------------------------


def position(**kw):
    base = {"id": 1, "shares": 100.0, "avg_price": 0.50, "cost_usd": 50.0, "fees_paid": 0.0,
            "opened_ts": 0}
    return {**base, **kw}


def test_the_stop_is_measured_after_fees_not_on_the_mid():
    """A stop checked against the mid does not fire until the real loss is already worse."""
    t = Task(name="a", trader="0x1", sl_value=0.10)     # stop at 0.45
    b = book_of([(0.47, 1000)], [(0.49, 1000)])          # mid 0.48, net after fee ~0.446
    assert exits.check(t, position(), b, fee_rate=0.05, now=1).reason == exits.STOP_LOSS


def test_take_profit_is_not_checked_when_it_is_resting_on_the_book():
    """It is a live GTC order there; checking it here too would sell the position twice."""
    t = Task(name="a", trader="0x1", tp_value=0.05, resting_tp=True, sl_kind=None)
    b = book_of([(0.60, 1000)], [(0.61, 1000)])
    assert exits.check(t, position(), b, fee_rate=0.0, now=1).exit is False
    t.resting_tp = False
    assert exits.check(t, position(), b, fee_rate=0.0, now=1).reason == exits.TAKE_PROFIT


def test_the_trailing_stop_only_arms_once_the_position_has_been_in_profit():
    """Otherwise it is a second, tighter stop-loss measured off the entry."""
    t = Task(name="a", trader="0x1", trail_pct=0.10, sl_kind=None, tp_kind=None)
    b = book_of([(0.48, 1000)], [(0.49, 1000)])
    assert exits.check(t, position(), b, fee_rate=0.0, now=1, high_water=0.49).exit is False
    assert exits.check(t, position(), b, fee_rate=0.0, now=1, high_water=0.60).reason \
        == exits.TRAILING


def test_a_quick_flip_that_has_not_worked_is_closed_on_time():
    t = Task(name="a", trader="0x1", sl_kind=None, tp_kind=None, max_hold_s=600)
    b = book_of([(0.50, 1000)], [(0.51, 1000)])
    assert exits.check(t, position(), b, fee_rate=0.0, now=601).reason == exits.MAX_HOLD


def test_exits_are_notional_when_nothing_is_bidding():
    t = Task(name="a", trader="0x1")
    assert exits.check(t, position(), book_of([], [(0.5, 10)]), fee_rate=0.05, now=1).exit \
        is False


def test_the_breaker_counts_unrealized_losses_too():
    """A bot that only counts realized PnL sits calmly through a total loss."""
    t = Task(name="a", trader="0x1", max_daily_loss_usd=20, max_drawdown_pct=0)
    assert exits.breaker(t, 100, 0.0, -25.0).reason == exits.CIRCUIT_BREAKER
    assert exits.breaker(t, 100, 0.0, -5.0).exit is False


@pytest.mark.parametrize("kw,expected", [
    (dict(age_s=300), "stale"),
    (dict(price=0.99), "price_band"),
    (dict(seconds_to_close=60), "market_closing"),
    (dict(accepting_orders=False), "not_accepting_orders"),
    ({}, None),
])
def test_entry_gates_name_the_rule_that_rejected_the_trade(kw, expected):
    t = Task(name="a", trader="0x1")
    base = dict(price=0.5, age_s=5, seconds_to_close=99999, usd=100.0, accepting_orders=True)
    assert exits.entry_gates(t, **{**base, **kw}) == expected


# --- paper execution ------------------------------------------------------


def test_a_paper_order_cannot_fill_above_its_limit():
    ex = PaperExecutor()
    b = book_of([], [(0.60, 1000)])
    fill = ex.buy("tok", 20, b, limit=0.55, tick=0.001, fee_rate=0.05, min_shares=5)
    assert fill.status == "rejected" and "0.550" in fill.reason


def test_a_paper_order_below_the_markets_minimum_size_is_refused():
    ex = PaperExecutor()
    fill = ex.buy("tok", 1.0, book_of([], [(0.50, 1000)]), limit=0.6, tick=0.001,
                  fee_rate=0.05, min_shares=5)
    assert fill.status == "rejected" and "minimum" in fill.reason


def test_a_resting_sell_fills_when_the_bid_reaches_it():
    ex = PaperExecutor()
    order = {"price": 0.60, "shares": 100}
    assert ex.resting_fill(order, book_of([(0.59, 10)], []), fee_rate=0.0) is None
    fill = ex.resting_fill(order, book_of([(0.60, 10)], []), fee_rate=0.0)
    assert fill.status == "filled" and fill.cost == pytest.approx(60)


# --- latency --------------------------------------------------------------


def test_latency_is_measured_from_the_two_columns_every_signal_carries(con):
    _task(con)
    run_id = store.start_run(con, "t", "paper", 100)
    for i, (traded, seen) in enumerate([(100, 105), (200, 210), (300, 360)]):
        store.insert_signal(con, _signal(run_id, tx=str(i), trader_ts=traded, seen_ts=seen))
    stats = store.latency_stats(con, run_id)
    assert stats["n"] == 3 and stats["min"] == 5 and stats["max"] == 60
    assert stats["p50"] == 10          # nearest-rank: a real observation, not an average


def test_latency_over_no_signals_does_not_divide_by_zero(con):
    assert store.latency_stats(con, 1) == {"n": 0}


# --- the loop, end to end -------------------------------------------------


class FakeAPI:
    """Stands in for polymarket.py. The engine's whole view of the world."""

    def __init__(self, feed, books, end_ts=10**10):
        self.feed = feed
        self.books = books
        self.end_ts = end_ts
        self.calls = []

    def activity(self, client, user, **kw):
        self.calls.append(("activity", user))
        return self.feed

    def book(self, client, token_id, **kw):
        b = self.books[token_id]
        return {"asset_id": token_id, "market": "cond",
                "bids": [{"price": str(p), "size": str(s)} for p, s in b["bids"]],
                "asks": [{"price": str(p), "size": str(s)} for p, s in b["asks"]]}

    def markets_by_condition(self, client, ids, closed=None):
        return [{"conditionId": ids[0], "id": "1", "question": "q", "outcomes": '["Yes","No"]',
                 "outcomePrices": '["0.5","0.5"]', "clobTokenIds": '["tok"]',
                 "acceptingOrders": True, "enableOrderBook": True, "orderMinSize": 5,
                 "orderPriceMinTickSize": 0.001, "feeType": "politics_fees",
                 "endDate": "2030-01-01T00:00:00Z", "closed": False, "active": True}]


def activity_event(**kw):
    base = {"proxyWallet": "0xtrader", "type": "TRADE", "asset": "tok", "conditionId": "cond",
            "side": "BUY", "size": 100, "price": 0.50, "usdcSize": 50.0, "timestamp": 1000,
            "transactionHash": "0xtx", "outcome": "Yes", "outcomeIndex": 0, "title": "t"}
    return {**base, **kw}


def engine_for(con, feed, books, task=None, now=1010):
    import polywatch.copytrade.engine as engine_mod
    fake = FakeAPI(feed, books)
    engine_mod.api = fake
    t = task or Task(name="t", trader="0xtrader", **preset("quick_flips"))
    _task(con, t)
    eng = Engine(con, t, PaperExecutor(), client=None, log=lambda *a: None, now=lambda: now)
    eng._trader_account = lambda: 10_000.0
    eng.start()
    return eng, fake


def _task(con, t=None):
    t = t or Task(name="t", trader="0xtrader")
    store.upsert_task(con, t.as_row())
    return t


def _signal(run_id, tx="0x1", **kw):
    row = {"run_id": run_id, "trader": "0xtrader", "kind": "TRADE", "tx_hash": tx,
           "token_id": "tok", "condition_id": "cond", "side": "BUY", "size": 1.0,
           "price": 0.5, "usdc_size": 1.0, "trader_ts": 1, "seen_ts": 2, "action": "copied",
           "reason": None}
    return {**row, **kw}


def test_a_fresh_buy_is_copied_and_opens_a_position(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, _ = engine_for(con, [activity_event()], books)
    eng.poll_signals()
    pos = store.open_positions(con, eng.state.run_id)
    assert len(pos) == 1
    assert pos[0]["avg_price"] == pytest.approx(0.51)
    assert store.signals(con, eng.state.run_id, "copied")


def test_the_same_fill_seen_on_two_polls_is_copied_once(con):
    """The dedupe is the database's, not a set in memory -- a restart must not re-copy."""
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, _ = engine_for(con, [activity_event()], books)
    eng.poll_signals()
    eng.poll_signals()
    assert len(store.orders(con, eng.state.run_id)) == 1


def test_a_trade_we_noticed_too_late_is_skipped_and_the_reason_is_recorded(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, _ = engine_for(con, [activity_event(timestamp=1)], books, now=9999)
    eng.poll_signals()
    assert store.skip_reasons(con, eng.state.run_id) == [("stale", 1)]
    assert not store.open_positions(con, eng.state.run_id)


def test_a_copied_buy_leaves_a_resting_take_profit_on_the_book(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, _ = engine_for(con, [activity_event()], books)
    eng.poll_signals()
    resting = store.open_resting(con, eng.state.run_id)
    assert len(resting) == 1
    assert resting[0]["kind"] == "take_profit"
    assert resting[0]["price"] == pytest.approx(0.51 * 1.10, abs=0.002)


def test_the_resting_take_profit_closes_the_position_when_the_bid_reaches_it(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, fake = engine_for(con, [activity_event()], books)
    eng.poll_signals()
    fake.books["tok"] = {"bids": [(0.70, 1000)], "asks": [(0.71, 1000)]}
    eng.manage_positions()
    assert not store.open_positions(con, eng.state.run_id)
    closed = con.execute("SELECT * FROM positions WHERE open=0").fetchone()
    assert closed["close_reason"] == exits.TAKE_PROFIT and closed["realized_pnl"] > 0


def test_a_collapsing_price_trips_the_stop_and_the_loss_is_bounded(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, fake = engine_for(con, [activity_event()], books)
    eng.poll_signals()
    fake.books["tok"] = {"bids": [(0.30, 1000)], "asks": [(0.31, 1000)]}
    eng.manage_positions()
    closed = con.execute("SELECT * FROM positions WHERE open=0").fetchone()
    assert closed["close_reason"] == exits.STOP_LOSS and closed["realized_pnl"] < 0


def test_the_trader_selling_takes_us_out_with_them(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, fake = engine_for(con, [activity_event()], books)
    eng.poll_signals()
    fake.feed = [activity_event(side="SELL", transactionHash="0xtx2", timestamp=1005)]
    eng.poll_signals()
    closed = con.execute("SELECT * FROM positions WHERE open=0").fetchone()
    assert closed["close_reason"] == exits.FOLLOW_EXIT


def test_a_sell_in_a_token_we_do_not_hold_is_recorded_not_acted_on(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, _ = engine_for(con, [activity_event(side="SELL")], books)
    eng.poll_signals()
    assert store.skip_reasons(con, eng.state.run_id) == [("not_held", 1)]


def test_non_trade_events_are_recorded_because_they_move_positions_too(con):
    """A REDEEM is how a position leaves their book without a sell."""
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, _ = engine_for(con, [activity_event(type="REDEEM", side="")], books)
    eng.poll_signals()
    assert store.skip_reasons(con, eng.state.run_id) == [("kind_redeem", 1)]


def test_concurrency_and_per_market_caps_are_enforced_before_any_order(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    t = Task(name="t", trader="0xtrader", max_concurrent=1, max_market_usd=10, fixed_usd=10)
    eng, fake = engine_for(con, [activity_event()], books, task=t)
    eng.poll_signals()
    fake.feed = [activity_event(transactionHash="0xtx2", timestamp=1005)]
    eng.poll_signals()
    assert store.skip_reasons(con, eng.state.run_id) == [("max_market_usd", 1)]
    assert len(store.orders(con, eng.state.run_id)) == 1


def test_stopping_flattens_the_book_and_closes_the_run(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, _ = engine_for(con, [activity_event()], books)
    eng.poll_signals()
    summary = eng.stop("session_end")
    assert summary["positions_open"] == 0
    assert not store.open_resting(con, eng.state.run_id)
    run = con.execute("SELECT * FROM task_runs WHERE id=?", (eng.state.run_id,)).fetchone()
    assert run["stopped_at"] and run["stop_reason"] == "session_end"


def test_not_flattening_leaves_the_position_and_says_so(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    t = Task(name="t", trader="0xtrader", flatten_on_stop=False)
    eng, _ = engine_for(con, [activity_event()], books, task=t)
    eng.poll_signals()
    assert eng.stop("session_end")["positions_open"] == 1


def test_an_abandoned_run_is_closed_out_rather_than_inherited(con):
    """A run left open was killed; its positions are stale and its accounting unfinished."""
    _task(con)
    dead = store.start_run(con, "t", "paper", 100)
    eng, _ = engine_for(con, [], {})
    assert eng.state.run_id != dead
    assert con.execute("SELECT stop_reason FROM task_runs WHERE id=?",
                       (dead,)).fetchone()[0] == "abandoned"


def test_a_task_saved_by_an_older_version_still_loads(con):
    """config_json is the source of truth, and a knob added later must not invalidate it."""
    t = _task(con)
    row = dict(con.execute("SELECT * FROM tasks WHERE name='t'").fetchone())
    row["config_json"] = '{"name":"t","trader":"0xTRADER","fixed_usd":7,"gone_field":1}'
    con.execute("UPDATE tasks SET config_json=? WHERE name='t'", (row["config_json"],))
    loaded = Task.from_row(con.execute("SELECT * FROM tasks WHERE name='t'").fetchone())
    assert loaded.fixed_usd == 7 and loaded.trader == "0xtrader"
    assert loaded.max_hold_s == Task(name="x", trader="0x1").max_hold_s
