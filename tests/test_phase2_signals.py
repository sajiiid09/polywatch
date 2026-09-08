"""Phase 2: what reaches the copy decision, and what the histogram says about what did not.

The theme is that a refusal to trade must be visible. Every gate here existed in some form
already; what they had in common was deciding too late, in a place nothing reports on.
"""

from __future__ import annotations

import pytest

from polywatch.config import ACTIVITY_PAGE
from polywatch.copytrade import book as bk
from polywatch.copytrade import exits
from polywatch.copytrade.engine import Engine
from polywatch.copytrade.execution import PaperExecutor
from polywatch.copytrade.task import Task, preset
from polywatch.db import store

from test_copytrade_engine import FakeAPI, activity_event, book_of, engine_for


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init_db(c)
    return c


# --- slippage is measured against the touch, not against our own impact ---


def test_slippage_is_measured_from_the_touch_not_from_our_own_impact():
    """Priced off the walk's VWAP, a 7% allowance permits impact AND 7%. On a two-level book
    that is not the number the operator asked for."""
    thin = book_of([], [(0.50, 10), (0.80, 1000)])
    w = bk.buy_for_usd(thin, 100)
    touch = bk.best_ask(thin)
    assert w.vwap > touch                                   # our size already moved the price
    assert bk.limit_price(touch, 0.07, 0.001, "BUY") < bk.limit_price(w.vwap, 0.07, 0.001, "BUY")


def test_the_spread_is_expressed_in_the_same_unit_as_the_fee_floor():
    """A one-cent spread is 2% of the price at 0.50 and 20% of it at 0.05."""
    assert bk.spread_frac(book_of([(0.495, 10)], [(0.505, 10)])) == pytest.approx(0.02)
    assert bk.spread_frac(book_of([(0.045, 10)], [(0.055, 10)])) == pytest.approx(0.20)
    assert bk.spread_frac(book_of([], [(0.50, 10)])) is None


# --- the book is a gate, not a discovery --------------------------------


def test_a_book_too_wide_to_cross_is_refused_by_name():
    t = Task(name="a", trader="0x1", max_spread_frac=0.10)
    # 10 cents wide on a 0.50 mid: 20% to cross, twice the fee floor at that price.
    assert exits.book_gates(t, book_of([(0.45, 500)], [(0.55, 500)]), usd=10) == "wide_spread"
    assert exits.book_gates(t, book_of([(0.499, 500)], [(0.501, 500)]), usd=10) is None


def test_a_book_that_cannot_supply_our_stake_is_refused_by_name():
    t = Task(name="a", trader="0x1")
    assert exits.book_gates(t, book_of([(0.50, 500)], [(0.51, 2)]), usd=10) == "thin_book"
    assert exits.book_gates(t, book_of([(0.50, 500)], [(0.51, 500)]), usd=10) is None


def test_a_missing_book_is_refused_rather_than_assumed_empty():
    t = Task(name="a", trader="0x1")
    assert exits.book_gates(t, None, usd=10) == "no_book"
    assert exits.book_gates(t, book_of([], []), usd=10) == "no_book"


def test_depth_can_be_demanded_beyond_our_own_stake():
    t = Task(name="a", trader="0x1", min_depth_usd=100.0)
    assert exits.book_gates(t, book_of([(0.50, 500)], [(0.51, 50)]), usd=10) == "thin_book"


def test_illiquidity_reaches_the_skip_histogram_instead_of_a_rejected_order(con):
    """The histogram is the main output of a paper run. It could not see the most common reason
    a copy is not worth making, because the book was only consulted inside the executor."""
    books = {"tok": {"bids": [(0.30, 1000)], "asks": [(0.70, 1000)]}}   # 80% spread
    eng, _ = engine_for(con, [activity_event()], books)
    eng.poll_signals()
    assert dict(store.skip_reasons(con, eng.state.run_id)) == {"wide_spread": 1}
    assert not store.orders(con, eng.state.run_id), "no order should have been attempted"


def test_a_stake_squeezed_under_the_market_minimum_is_a_skip_not_a_rejection(con):
    """Caps can shrink a stake below the minimum order size after every other gate has passed."""
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    t = Task(name="t", trader="0xtrader", bankroll=100, max_market_usd=1.0,
             **preset("quick_flips"))
    eng, _ = engine_for(con, [activity_event()], books, task=t)
    eng.poll_signals()
    assert dict(store.skip_reasons(con, eng.state.run_id)) == {"below_min_size": 1}


# --- reading the feed ----------------------------------------------------


class PagedAPI(FakeAPI):
    """An activity feed with more events in it than one page can carry."""

    def __init__(self, events, books):
        super().__init__(events, books)
        self.pages_read = 0

    def activity(self, client, user, **kw):
        self.pages_read += 1
        off = kw.get("offset", 0)
        return self.feed[off:off + ACTIVITY_PAGE]


def test_a_busy_trader_is_read_past_the_first_page(con):
    """One page is the most recent hundred events, not everything since the watermark. The
    remainder used to vanish while the watermark advanced past it."""
    import polywatch.copytrade.engine as engine_mod
    feed = [activity_event(transactionHash=f"0x{i}", timestamp=1000 + i, side="SELL")
            for i in range(ACTIVITY_PAGE + 25)]
    fake = PagedAPI(feed, {})
    engine_mod.api = fake
    t = Task(name="t", trader="0xtrader", **preset("quick_flips"))
    store.upsert_task(con, t.as_row())
    eng = Engine(con, t, PaperExecutor(), client=None, log=lambda *a: None, now=lambda: 1200)
    eng._trader_account = lambda: 0.0
    eng.start()
    eng.state.last_seen_ts = 900

    eng.poll_signals()
    assert fake.pages_read > 1
    assert len(store.signals(con, eng.state.run_id)) == len(feed)


def test_a_read_that_hits_the_page_cap_is_counted_rather_than_swallowed(con):
    import polywatch.copytrade.engine as engine_mod
    from polywatch.config import MAX_ACTIVITY_PAGES
    feed = [activity_event(transactionHash=f"0x{i}", timestamp=2000 + i, side="SELL")
            for i in range(ACTIVITY_PAGE * (MAX_ACTIVITY_PAGES + 2))]
    fake = PagedAPI(feed, {})
    engine_mod.api = fake
    t = Task(name="t", trader="0xtrader", **preset("quick_flips"))
    store.upsert_task(con, t.as_row())
    eng = Engine(con, t, PaperExecutor(), client=None, log=lambda *a: None, now=lambda: 9999)
    eng._trader_account = lambda: 0.0
    eng.start()
    eng.state.last_seen_ts = 1

    eng.poll_signals()
    assert eng.state.truncated_polls == 1
    assert eng.state.errors == 1


def test_the_watermark_stays_behind_an_event_that_could_not_be_handled(con):
    """Advancing it first meant a failure mid-page lost every event after it, permanently."""
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    feed = [activity_event(transactionHash="0xa", timestamp=1000),
            activity_event(transactionHash="0xb", timestamp=1001),
            activity_event(transactionHash="0xc", timestamp=1002)]
    eng, _ = engine_for(con, feed, books)
    eng.state.last_seen_ts = 999

    calls = {"n": 0}
    real = eng.handle_event

    def flaky(ev, seen_ts, fetch_ts=None):
        calls["n"] += 1
        if ev["tx_hash"] == "0xb":
            raise RuntimeError("upstream shape changed")
        return real(ev, seen_ts, fetch_ts)

    eng.handle_event = flaky
    eng.poll_signals()
    assert eng.state.last_seen_ts == 1000, "the watermark must not pass the event that failed"
    assert eng.state.errors == 1


# --- following the trader out -------------------------------------------


def test_following_a_trader_who_scales_out_sells_the_same_fraction(con):
    """Answering a 25% sale by dumping everything copies neither their entry nor their exit."""
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, fake = engine_for(con, [activity_event(size=400)], books)
    eng.poll_signals()
    ours = store.open_positions(con, eng.state.run_id)[0]["shares"]

    fake.feed = [activity_event(side="SELL", size=100, transactionHash="0xsell",
                                timestamp=1005)]
    eng.poll_signals()

    left = store.open_positions(con, eng.state.run_id)
    assert len(left) == 1, "a quarter sale must not close the whole position"
    assert left[0]["shares"] == pytest.approx(ours * 0.75, rel=0.02)


def test_a_trader_selling_everything_they_are_known_to_hold_closes_us_out(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, fake = engine_for(con, [activity_event(size=400)], books)
    eng.poll_signals()

    fake.feed = [activity_event(side="SELL", size=400, transactionHash="0xsell",
                                timestamp=1005)]
    eng.poll_signals()
    assert not store.open_positions(con, eng.state.run_id)


def test_a_sale_by_a_trader_we_never_saw_buy_closes_us_out(con):
    """Their holding is only known as far as this run watched it. Not knowing means exiting.

    This is the case where we joined a position they had already built: the shares they are
    selling were bought before we were watching, so the observed floor is zero.
    """
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, fake = engine_for(con, [activity_event()], books)
    eng.poll_signals()
    con.execute("UPDATE signals SET size=0 WHERE side='BUY'")     # as if we never saw the buy
    con.commit()

    fake.feed = [activity_event(side="SELL", size=1, transactionHash="0xsell", timestamp=1005)]
    eng.poll_signals()
    assert not store.open_positions(con, eng.state.run_id)


# --- neg-risk events ------------------------------------------------------


def test_exposure_is_capped_across_a_neg_risk_event_not_per_market(con):
    """Three outcomes of one neg-risk event are one view held three times."""
    store.upsert_task(con, Task(name="t", trader="0xt").as_row())
    run = store.start_run(con, "t", "paper", 100.0)
    for i, cid in enumerate(("c1", "c2")):
        store.upsert_market_meta(con, {"condition_id": cid, "neg_risk_id": "evt"})
        store.open_position(con, {
            "run_id": run, "token_id": f"t{i}", "condition_id": cid, "shares": 20,
            "avg_price": 0.5, "cost_usd": 10.0, "fees_paid": 0.0, "opened_ts": 1})

    assert store.market_exposure(con, run, "c1") == pytest.approx(10.0)
    assert store.event_exposure(con, run, "evt") == pytest.approx(20.0)


# --- latency, split ------------------------------------------------------


def test_the_report_can_tell_the_feeds_lag_from_our_own(con):
    """Their sum is what we always had, and it cannot say which half to go and fix."""
    store.upsert_task(con, Task(name="t", trader="0xt").as_row())
    run = store.start_run(con, "t", "paper", 100.0)
    for i in range(4):
        store.insert_signal(con, {
            "run_id": run, "trader": "0xt", "kind": "TRADE", "tx_hash": f"0x{i}",
            "token_id": "tok", "condition_id": "c", "side": "BUY", "size": 1.0, "price": 0.5,
            "usdc_size": 1.0, "trader_ts": 100, "fetch_ts": 112, "seen_ts": 115,
            "action": "copied", "reason": None})
    split = store.latency_split(con, run)
    assert split["n"] == 4
    assert split["feed"] == pytest.approx(12.0)
    assert split["loop"] == pytest.approx(3.0)
