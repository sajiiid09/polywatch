"""Phase 2a: the paper engine and the historical replay that drives it.

The engine is the same code the live poller will run, so these tests are not only about the
backtest -- they are the only place the copy logic is exercised before real money is behind it.

Two of them exist because the backtest got the answer badly wrong first and the failure was
invisible in the headline number: `test_a_position_never_settles_at_the_instant_it_opens` and
`test_cash_is_conserved_end_to_end`. Both are regression tests for real bugs, described where
they sit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from polywatch.copytrade import book, fees, gates, replay, sizing
from polywatch.copytrade.engine import Engine, Fill
from polywatch.copytrade.task import Task
from polywatch.db import store
from polywatch.parse import records

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init_db(c)
    return c


# --- fees -----------------------------------------------------------------


def test_fee_is_charged_on_the_cheaper_side_of_the_price():
    """A share at 0.97 is charged on 0.03. This is why the average entry price of a trader is
    a cost statistic and not just a style one."""
    market = {"fee_rate": 0.05, "fee_exponent": 1.0, "fee_source": "market"}
    assert fees.taker_fee(100, 0.97, market) == pytest.approx(fees.taker_fee(100, 0.03, market))
    assert fees.taker_fee(100, 0.5, market) > fees.taker_fee(100, 0.9, market)


def test_fee_prefers_the_markets_own_schedule_over_the_category_fallback():
    own = {"fee_rate": 0.02, "fee_exponent": 1.0, "fee_source": "market"}
    assert fees.fee_params(own) == (0.02, 1.0, "market")


def test_fee_falls_back_to_the_category_when_the_market_shipped_no_schedule():
    rate, _, source = fees.fee_params({"fee_rate": None, "fee_type": "crypto_fees_v2",
                                       "category": None})
    assert source == "fallback" and rate == 0.07


def test_an_unknown_market_is_charged_rather_than_let_off():
    """Under-charging fees flatters exactly the high-frequency copying fees exist to punish."""
    assert fees.taker_fee(100, 0.5, None) > 0


def test_a_market_with_fees_disabled_pays_nothing():
    assert fees.taker_fee(100, 0.5, {"fees_enabled": 0, "fee_rate": 0.05}) == 0.0


# --- order book -----------------------------------------------------------


def _book():
    return records.parse_book(json.loads((FIXTURES / "book.json").read_text(), strict=False))


def test_walking_the_book_pays_the_volume_weighted_price_not_the_top_of_book():
    asks = _book()["asks"]
    shares, vwap = book.walk(asks, 10.0)
    assert shares > 0
    assert vwap > asks[0][0], "eating into worse levels must cost more than the best ask"
    assert shares * vwap == pytest.approx(10.0)


def test_a_book_without_the_depth_fills_partially_rather_than_lying():
    shares, vwap = book.walk([(0.5, 10.0)], 100.0)
    assert (shares, vwap) == (10.0, 0.5)


def test_walking_exactly_one_level_pays_exactly_that_level():
    assert book.walk([(0.25, 100.0)], 25.0) == (100.0, 0.25)


def test_selling_walks_the_book_in_shares_not_dollars():
    proceeds, vwap = book.walk_shares([(0.6, 10.0), (0.5, 10.0)], 15.0)
    assert proceeds == pytest.approx(0.6 * 10 + 0.5 * 5)
    assert vwap == pytest.approx(proceeds / 15.0)


def test_tick_rounding_moves_each_side_outward():
    """Rounding a buy limit down or a sell limit up would quietly tighten the tolerance we just
    chose, and the order would stop filling for a reason nothing logs."""
    assert book.limit_price(0.5, 0.07, 0.01, "BUY") >= 0.535
    assert book.limit_price(0.5, 0.07, 0.01, "SELL") <= 0.465


def test_a_limit_price_never_lands_on_0_or_1():
    assert 0 < book.limit_price(0.999, 0.5, 0.001, "BUY") < 1
    assert 0 < book.limit_price(0.001, 0.5, 0.001, "SELL") < 1


# --- task -----------------------------------------------------------------


def test_style_and_risk_intersect_rather_than_layer():
    t = Task(name="t", trader="0xA", style="safe_and_steady", risk="conservative")
    lo, hi = t.price_band()
    assert (lo, hi) == (0.60, 0.92)


def test_a_task_rejects_settings_that_cannot_trade():
    with pytest.raises(ValueError):
        Task(name="t", trader="0xA", bankroll=0)
    with pytest.raises(ValueError):
        Task(name="t", trader="0xA", slippage=1.5)
    with pytest.raises(ValueError):
        Task(name="t", trader="0xA", buy_method="fixed", fixed_usd=0)


def test_a_task_round_trips_through_the_database(con):
    t = Task(name="t", trader="0xABC", style="value_hunter")
    store.upsert_task(con, t.to_row())
    back = Task.from_row(store.get_task(con, "t"))
    assert back.trader == "0xabc" and back.style == "value_hunter"


# --- sizing ---------------------------------------------------------------


def _run(con, task):
    store.upsert_task(con, task.to_row())
    return store.start_run(con, task.name, task.mode, task.bankroll, started_at=1_700_000_000)


def _sig(**kw):
    base = {"wallet": "0xa", "kind": "TRADE", "token_id": "tok", "condition_id": "cond",
            "side": "BUY", "size": 100.0, "price": 0.5, "usdc_size": 50.0,
            "ts": 1_700_000_100, "outcome": "Yes", "outcome_index": 0, "tx_hash": "0xtx"}
    return {**base, **kw}


def test_fixed_sizing_stakes_the_same_amount_regardless_of_their_conviction(con):
    task = Task(name="t", trader="0xa", fixed_usd=10.0)
    run_id = _run(con, task)
    usd, reason = sizing.size_usd(con, run_id, task, _sig(usdc_size=9999.0), None)
    assert (usd, reason) == (10.0, None)


def test_mirror_sizing_is_capped_so_an_idle_cash_pile_cannot_size_the_whole_bankroll(con):
    """/value sees open positions only, so the denominator is a floor on their account and
    every fraction derived from it is an overstatement."""
    task = Task(name="t", trader="0xa", buy_method="mirror", bankroll=100.0)
    run_id = _run(con, task)
    usd, _ = sizing.size_usd(con, run_id, task, _sig(usdc_size=500.0), None,
                             trader_account=1000.0)
    assert usd == pytest.approx(10.0)  # 10% of their book, capped at MIRROR_MAX_FRACTION
    usd, _ = sizing.size_usd(con, run_id, task, _sig(usdc_size=900.0), None,
                             trader_account=1000.0)
    assert usd == pytest.approx(10.0), "the cap, not 90% of the bankroll"


def test_mirror_sizing_says_so_when_it_has_no_denominator(con):
    task = Task(name="t", trader="0xa", buy_method="mirror")
    run_id = _run(con, task)
    assert sizing.size_usd(con, run_id, task, _sig(), None, None)[1] == "mirror_size_unknown"


def test_the_per_market_cap_is_measured_across_every_outcome(con):
    task = Task(name="t", trader="0xa", fixed_usd=20.0, max_market_usd=25.0)
    run_id = _run(con, task)
    store.open_position(con, {"run_id": run_id, "token_id": "yes", "condition_id": "cond",
                              "shares": 40, "avg_price": 0.5, "cost_usd": 20.0,
                              "fees_paid": 0.0, "opened_ts": 1})
    usd, reason = sizing.size_usd(con, run_id, task, _sig(token_id="no"), None)
    assert reason is None and usd == pytest.approx(5.0), "only the remaining room"


def test_sizing_refuses_a_stake_that_cannot_clear_the_minimum_order(con):
    task = Task(name="t", trader="0xa", fixed_usd=1.0)
    run_id = _run(con, task)
    _, reason = sizing.size_usd(con, run_id, task, _sig(price=0.5),
                                {"order_min_size": 5.0})
    assert reason == "below_min_order_size"


def test_sizing_stops_when_the_cash_is_gone(con):
    task = Task(name="t", trader="0xa", fixed_usd=10.0, bankroll=10.0, max_market_usd=100.0)
    run_id = _run(con, task)
    store.open_position(con, {"run_id": run_id, "token_id": "x", "condition_id": "other",
                              "shares": 20, "avg_price": 0.5, "cost_usd": 10.0,
                              "fees_paid": 0.0, "opened_ts": 1})
    assert sizing.size_usd(con, run_id, task, _sig(), None)[1] == "no_cash"


# --- gates ----------------------------------------------------------------


def _market(**kw):
    base = {"condition_id": "cond", "end_ts": 1_700_009_999, "order_min_size": 5.0,
            "accepting_orders": 1, "fee_rate": 0.02, "fee_exponent": 1.0,
            "fee_source": "market", "fees_enabled": 1}
    return {**base, **kw}


@pytest.mark.parametrize("sig,expected", [
    ({"kind": "REDEEM"}, "not_a_trade:redeem"),
    ({"side": "SELL"}, "not_an_entry"),
    ({"price": 0.0}, "bad_price"),
    ({"price": 1.0}, "bad_price"),
])
def test_each_gate_names_itself_when_it_rejects(con, sig, expected):
    task = Task(name="t", trader="0xa")
    run_id = _run(con, task)
    ok, reason = gates.check(con, run_id, task, _sig(**sig), _market())
    assert not ok and reason == expected


def test_an_unknown_market_is_not_copied(con):
    task = Task(name="t", trader="0xa")
    run_id = _run(con, task)
    assert gates.check(con, run_id, task, _sig(), None) == (False, "market_unknown")


def test_style_bands_reject_prices_outside_them(con):
    task = Task(name="t", trader="0xa", style="value_hunter")  # 0.03 - 0.40
    run_id = _run(con, task)
    assert gates.check(con, run_id, task, _sig(price=0.8), _market())[1] \
        == "price_above_value_hunter_band"


def test_max_concurrent_counts_positions_not_trades(con):
    """Adding to a market we already hold must not consume a fresh slot, or a full book would
    stop us following a trader deeper into a position we are already committed to."""
    task = Task(name="t", trader="0xa", max_concurrent=1)
    run_id = _run(con, task)
    store.open_position(con, {"run_id": run_id, "token_id": "tok", "condition_id": "cond",
                              "shares": 10, "avg_price": 0.5, "cost_usd": 5.0,
                              "fees_paid": 0.0, "opened_ts": 1})
    assert gates.check(con, run_id, task, _sig(token_id="tok"), _market())[0] is True
    assert gates.check(con, run_id, task, _sig(token_id="other"), _market())[1] \
        == "max_concurrent_positions"


def test_the_live_gate_and_the_replay_gate_are_not_shared(con):
    """`accepting_orders` is false for every resolved market, so as a shared gate it would
    reject 100% of a backtest's signals. It has to stay live-only."""
    task = Task(name="t", trader="0xa")
    run_id = _run(con, task)
    sig, market = _sig(), _market(accepting_orders=0)
    assert gates.check(con, run_id, task, sig, market)[0] is True
    assert gates.check(con, run_id, task, sig, market,
                       (gates.market_accepting_orders,))[1] == "market_not_accepting_orders"


# --- position splitting ---------------------------------------------------


def test_a_partial_exit_leaves_the_average_price_untouched(con):
    """avg_price is what a stop-loss measures against, so a partial exit must not move it."""
    task = Task(name="t", trader="0xa")
    run_id = _run(con, task)
    pid = store.open_position(con, {"run_id": run_id, "token_id": "tok",
                                    "condition_id": "cond", "shares": 100.0,
                                    "avg_price": 0.4, "cost_usd": 40.0, "fees_paid": 2.0,
                                    "opened_ts": 10})
    pnl, kept = store.reduce_position(con, pid, 0.25, proceeds_usd=15.0, exit_fee=0.5,
                                      reason="mirror_sell_partial", ts=20)
    sold = con.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
    rest = con.execute("SELECT * FROM positions WHERE id=?", (kept,)).fetchone()
    assert sold["avg_price"] == rest["avg_price"] == 0.4
    assert sold["shares"] == pytest.approx(25.0) and rest["shares"] == pytest.approx(75.0)
    assert pnl == pytest.approx(15.0 - 10.0 - 0.5 - 0.5)
    assert rest["opened_ts"] == 10, "the remainder keeps the original entry time"


def test_a_partial_exit_keeps_the_cash_calculation_whole(con):
    task = Task(name="t", trader="0xa", bankroll=100.0)
    run_id = _run(con, task)
    pid = store.open_position(con, {"run_id": run_id, "token_id": "tok",
                                    "condition_id": "cond", "shares": 100.0,
                                    "avg_price": 0.4, "cost_usd": 40.0, "fees_paid": 0.0,
                                    "opened_ts": 10})
    store.reduce_position(con, pid, 0.5, proceeds_usd=25.0, exit_fee=0.0,
                          reason="mirror_sell_partial", ts=20)
    # 100 spent 40, sold half (cost 20) for 25 -> 5 profit, 20 still tied up.
    assert store.run_cash(con, run_id) == pytest.approx(85.0)


# --- engine ---------------------------------------------------------------


class _FlatFills:
    """Fills at the signal price, no slippage, no fee -- so a test measures the engine's
    bookkeeping rather than the fill model's arithmetic."""

    def buy(self, sig, usd, market):
        shares = usd / sig["price"]
        return Fill(shares, sig["price"], 0.0, sig["price"], sig["price"])

    def sell(self, token_id, shares, price_hint, ts, market):
        return Fill(shares, price_hint, 0.0, price_hint, price_hint)


def _engine(con, task, run_id):
    return Engine(con, run_id, task, _FlatFills())


def _seed_market(con, condition_id="cond", token_id="tok", end_ts=1_700_009_999):
    store.upsert_markets(con, [{
        "condition_id": condition_id, "gamma_id": "1", "question": "q", "slug": "s",
        "category": None, "closed": 1, "active": 0, "archived": 0, "start_ts": 1,
        "end_ts": end_ts, "outcomes_json": "[]", "prices_json": "[]", "uma_status_json": "[]",
        "resolved": 1, "winning_index": 0, "neg_risk": 0, "fees_enabled": 1,
        "fee_type": "sports_fees_v3", "fee_rate": 0.0, "fee_exponent": 1.0,
        "fee_taker_only": 1, "fee_rebate_rate": None, "fee_source": "market",
        "tick_size": 0.001, "order_min_size": 5.0, "accepting_orders": 0,
        "enable_order_book": 1}])
    store.upsert_assets(con, [{"token_id": token_id, "condition_id": condition_id,
                               "outcome_index": 0, "outcome": "Yes"}])


def test_a_copied_buy_opens_a_position_and_logs_an_order(con):
    task = Task(name="t", trader="0xa", fixed_usd=10.0)
    run_id = _run(con, task)
    _seed_market(con)
    _engine(con, task, run_id).handle(_sig())
    pos = store.open_position_for(con, run_id, "tok")
    assert pos["shares"] == pytest.approx(20.0) and pos["cost_usd"] == pytest.approx(10.0)
    assert len(store.orders(con, run_id)) == 1
    assert store.signals(con, run_id, "copied")


def test_a_second_buy_folds_into_the_same_position(con):
    task = Task(name="t", trader="0xa", fixed_usd=10.0, max_market_usd=100.0)
    run_id = _run(con, task)
    _seed_market(con)
    e = _engine(con, task, run_id)
    e.handle(_sig(tx_hash="0x1"))
    e.handle(_sig(tx_hash="0x2", price=0.25, ts=1_700_000_200))
    pos = store.open_position_for(con, run_id, "tok")
    assert pos["shares"] == pytest.approx(20.0 + 40.0)
    assert pos["avg_price"] == pytest.approx(20 / 60)


def test_a_skipped_trade_is_still_recorded_with_its_reason(con):
    """A skipped signal is as much of a result as a filled order -- it is the whole content of
    the skip histogram."""
    task = Task(name="t", trader="0xa", style="value_hunter")
    run_id = _run(con, task)
    _seed_market(con)
    _engine(con, task, run_id).handle(_sig(price=0.9))
    assert store.skip_reasons(con, run_id) == [("price_above_value_hunter_band", 1)]


def test_mirroring_a_full_exit_closes_the_position(con):
    task = Task(name="t", trader="0xa", fixed_usd=10.0)
    run_id = _run(con, task)
    _seed_market(con)
    e = _engine(con, task, run_id)
    e.handle(_sig(size=100.0))
    e.handle(_sig(side="SELL", size=100.0, price=0.75, ts=1_700_000_300, tx_hash="0x2"))
    assert store.open_position_for(con, run_id, "tok") is None
    assert store.run_cash(con, run_id) == pytest.approx(105.0)


def test_mirroring_a_half_exit_sells_half(con):
    task = Task(name="t", trader="0xa", fixed_usd=10.0)
    run_id = _run(con, task)
    _seed_market(con)
    e = _engine(con, task, run_id)
    e.handle(_sig(size=100.0))
    e.handle(_sig(side="SELL", size=50.0, price=0.5, ts=1_700_000_300, tx_hash="0x2"))
    pos = store.open_position_for(con, run_id, "tok")
    assert pos["shares"] == pytest.approx(10.0)


def test_behavior_buys_only_declines_to_follow_them_out(con):
    task = Task(name="t", trader="0xa", fixed_usd=10.0, behavior="buys")
    run_id = _run(con, task)
    _seed_market(con)
    e = _engine(con, task, run_id)
    e.handle(_sig(size=100.0))
    e.handle(_sig(side="SELL", size=100.0, ts=1_700_000_300, tx_hash="0x2"))
    assert store.open_position_for(con, run_id, "tok") is not None
    assert ("sell_ignored_by_behavior", 1) in store.skip_reasons(con, run_id)


def test_settlement_pays_face_value_and_charges_no_fee(con):
    task = Task(name="t", trader="0xa", fixed_usd=10.0)
    run_id = _run(con, task)
    _seed_market(con)
    e = _engine(con, task, run_id)
    e.handle(_sig())
    pnl = e.settle("tok", 1_700_009_999, won=True)
    assert pnl == pytest.approx(10.0), "20 shares at 0.5 redeem for 20"
    assert store.run_cash(con, run_id) == pytest.approx(110.0)


def test_a_losing_settlement_costs_exactly_the_stake(con):
    task = Task(name="t", trader="0xa", fixed_usd=10.0)
    run_id = _run(con, task)
    _seed_market(con)
    e = _engine(con, task, run_id)
    e.handle(_sig())
    assert e.settle("tok", 1_700_009_999, won=False) == pytest.approx(-10.0)


# --- replay ---------------------------------------------------------------


def _seed_history(con, wallet="0xa", n=6, condition="cond", token="tok"):
    _seed_market(con, condition, token, end_ts=1_700_050_000)
    store.upsert_wallets(con, [{"address": wallet, "source": "leaderboard", "rank": 1,
                                "username": "u", "vol": 1.0, "pnl": 1.0}])
    rows = [{"wallet": wallet, "token_id": token, "condition_id": condition, "side": "BUY",
             "size": 20.0, "price": 0.5, "ts": 1_700_000_000 + i * 60, "outcome": "Yes",
             "outcome_index": 0, "tx_hash": f"0x{i}"} for i in range(n)]
    store.insert_trades(con, rows)


def test_the_replay_refuses_a_wallet_with_no_ingested_history(con):
    with pytest.raises(ValueError, match="no ingested trades"):
        replay.run(con, Task(name="t", trader="0xnope"), 0.0)


def test_a_position_never_settles_at_the_instant_it_opens(con):
    """Regression. gamma's `end_ts` is the market's *scheduled* end and in-play markets trade
    straight past it, so an earlier version dated settlement at max(end_ts, opened_ts) and
    every position closed on the same second it opened. That is look-ahead -- the run learned
    the outcome before holding anything -- and it turned a $100 account into exactly $0."""
    _seed_history(con)
    task = Task(name="t", trader="0xa", fixed_usd=10.0, max_market_usd=100.0)
    summary = replay.run(con, task, 0.0)
    bad = con.execute(
        "SELECT COUNT(*) FROM positions WHERE run_id=? AND open=0 AND closed_ts<=opened_ts",
        (summary["run_id"],)).fetchone()[0]
    assert bad == 0


def test_cash_is_conserved_end_to_end(con):
    """start + realized - still-tied == cash, exactly. This is what catches a partial exit
    whose proceeds went somewhere the cash calculation cannot see."""
    _seed_history(con)
    task = Task(name="t", trader="0xa", fixed_usd=10.0, max_market_usd=100.0)
    s = replay.run(con, task, 0.0)
    realized = con.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM positions "
                           "WHERE run_id=? AND open=0", (s["run_id"],)).fetchone()[0]
    assert s["start_bankroll"] + realized - s["open_cost"] == pytest.approx(s["cash"])


def test_a_replay_is_deterministic(con):
    _seed_history(con)
    a = replay.run(con, Task(name="a", trader="0xa", fixed_usd=10.0), 0.03)
    b = replay.run(con, Task(name="b", trader="0xa", fixed_usd=10.0), 0.03)
    assert a["cash"] == pytest.approx(b["cash"])
    assert (a["copied"], a["skipped"]) == (b["copied"], b["skipped"])


def test_more_slippage_never_makes_a_run_richer(con):
    _seed_history(con)
    rich = replay.run(con, Task(name="a", trader="0xa", fixed_usd=10.0), 0.0)
    poor = replay.run(con, Task(name="b", trader="0xa", fixed_usd=10.0), 0.07)
    assert poor["cash"] <= rich["cash"]


def test_the_fill_model_spends_the_budget_on_shares_and_fee_together(con):
    """Sizing reserves exactly `usd` against the bankroll, so a fee charged on top would spend
    money the cash calculation never set aside -- and the overspend compounds all run."""
    fill = replay.ReplayFills(slippage=0.0).buy(_sig(price=0.5), 10.0,
                                                {"fee_rate": 0.02, "fee_exponent": 1.0,
                                                 "fee_source": "market", "fees_enabled": 1})
    assert fill.shares * fill.avg_price + fill.fee == pytest.approx(10.0)


def test_a_trades_row_becomes_the_same_shape_the_live_poller_will_produce(con):
    """The engine must not be able to tell a replay from a live run, or the backtest stops
    validating the thing it is meant to validate."""
    _seed_history(con, n=1)
    row = store.trader_trades(con, "0xa")[0]
    sig = replay.signal_from_trade(row)
    live_keys = set(records.parse_activity([{
        "proxyWallet": "0xA", "type": "TRADE", "asset": "tok", "conditionId": "cond",
        "side": "BUY", "size": 1, "price": 0.5, "usdcSize": 0.5, "timestamp": 1,
        "outcome": "Yes", "outcomeIndex": 0, "transactionHash": "0x", "title": "t",
        "slug": "s"}])[0])
    assert set(sig) == live_keys
