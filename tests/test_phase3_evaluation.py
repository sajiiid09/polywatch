"""Phase 3: whether a wallet is any good, and -- separately -- whether we can copy it.

The second question is the one this phase exists to answer, and it is the one nothing in the
project could answer before. A wallet can pass every behavioural screen, post a real Brier score
and a real ROI, and be worth nothing to a copier because the price it bought at was gone fifteen
seconds later.
"""

from __future__ import annotations

import pytest

from polywatch.copytrade import rank, replay, skill, sweep
from polywatch.copytrade.task import Task
from polywatch.db import store


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init_db(c)
    return c


def trade(ts, side, size, price, token="tok", cond="cond"):
    return {"wallet": "0xa", "token_id": token, "condition_id": cond, "side": side,
            "size": size, "price": price, "ts": ts, "outcome": "Yes", "outcome_index": 0,
            "tx_hash": f"0x{ts}{side}"}


def closed(pnl, cost=100.0, avg=0.50, end_ts=1_700_000_000, cond="cond"):
    return {"realized_pnl": pnl, "cost": cost, "avg_price": avg, "cur_price": 1.0,
            "end_ts": end_ts, "condition_id": cond, "shares": cost / avg}


# --- round trips ----------------------------------------------------------


def test_a_buy_and_the_sell_that_closed_it_become_one_round_trip():
    trips = skill.match_round_trips([trade(100, "BUY", 10, 0.40), trade(700, "SELL", 10, 0.55)])
    assert len(trips) == 1
    assert trips[0].hold_s == 600
    assert trips[0].entry_price == 0.40 and trips[0].exit_price == 0.55


def test_lots_are_matched_first_in_first_out():
    """Averaging their entries would smear a two-minute flip and a two-week hold into one
    number describing neither. The question is temporal, so the matching is too."""
    trips = skill.match_round_trips([
        trade(100, "BUY", 10, 0.40), trade(200, "BUY", 10, 0.60),
        trade(300, "SELL", 10, 0.70)])
    assert len(trips) == 1
    assert trips[0].entry_price == 0.40 and trips[0].entry_ts == 100


def test_a_partial_sale_closes_part_of_the_lot():
    trips = skill.match_round_trips([trade(100, "BUY", 10, 0.40), trade(300, "SELL", 4, 0.50)])
    assert len(trips) == 1 and trips[0].shares == pytest.approx(4)
    # ...and the rest of the lot is still open, so a later sale matches it too.
    trips = skill.match_round_trips([trade(100, "BUY", 10, 0.40), trade(300, "SELL", 4, 0.50),
                                     trade(400, "SELL", 6, 0.60)])
    assert len(trips) == 2 and sum(t.shares for t in trips) == pytest.approx(10)


def test_a_sell_with_no_matching_buy_is_dropped_rather_than_invented():
    """It belongs to a position opened before this window. Guessing its entry price would put a
    fabricated number into the metric that exists to measure entry prices."""
    assert skill.match_round_trips([trade(300, "SELL", 10, 0.50)]) == []


def test_holding_period_reports_the_tail_as_well_as_the_middle():
    """A fine median hides a fat tail of scalps that a 15s poll can never reach."""
    trades = []
    for i, hold in enumerate([30, 60, 600, 900, 1200, 1800, 3600]):
        trades += [trade(10_000 * i, "BUY", 1, 0.5, token=f"t{i}"),
                   trade(10_000 * i + hold, "SELL", 1, 0.6, token=f"t{i}")]
    h = skill.hold_times(skill.match_round_trips(trades))
    assert h["n"] == 7 and h["p10"] == 30 and h["p50"] == 900 and h["p90"] == 3600


# --- fees, recency, luck --------------------------------------------------


def test_a_winning_trader_can_be_a_losing_copy_once_the_fees_are_charged():
    """At even odds a round trip costs 10% of stake. An 8% edge is not an edge."""
    positions = [closed(pnl=8.0, cost=100.0, avg=0.50)]
    assert skill.roi(positions)[0] == pytest.approx(0.08)
    assert skill.fee_adjusted_roi(positions, lambda p: 0.05) == pytest.approx(-0.02)


def test_fees_barely_touch_a_trader_who_works_at_the_extremes():
    """The fee is proportional to min(p, 1-p), so the same edge survives at 0.90."""
    positions = [closed(pnl=8.0, cost=100.0, avg=0.90)]
    assert skill.fee_adjusted_roi(positions, lambda p: 0.05) > 0.06


def test_recent_form_outweighs_a_good_month_last_year():
    now = 1_700_000_000
    old_win = closed(pnl=100.0, cost=100.0, end_ts=now - 400 * 86400)
    new_loss = closed(pnl=-20.0, cost=100.0, end_ts=now - 2 * 86400)
    assert skill.roi([old_win, new_loss])[0] == pytest.approx(0.40)
    assert skill.recency_weighted_roi([old_win, new_loss], now) < 0


def test_a_record_that_is_mostly_one_lucky_trade_fails_the_luck_test():
    """One enormous winner among many small losers is variance, not an edge."""
    positions = [closed(pnl=-5.0) for _ in range(29)] + [closed(pnl=200.0)]
    assert skill.roi(positions)[0] > 0
    assert skill.luck_pvalue(positions) > 0.10


def test_a_record_of_many_small_consistent_wins_passes_it():
    positions = [closed(pnl=6.0) for _ in range(28)] + [closed(pnl=-4.0) for _ in range(4)]
    assert skill.luck_pvalue(positions) < 0.05


def test_the_luck_test_refuses_to_pronounce_on_a_thin_record():
    """A p-value over four trades is theatre. None says so; a number would not."""
    assert skill.luck_pvalue([closed(pnl=5.0) for _ in range(4)]) is None


def test_the_luck_test_gives_the_same_wallet_the_same_answer_twice():
    positions = [closed(pnl=3.0) for _ in range(25)] + [closed(pnl=-9.0) for _ in range(5)]
    assert skill.luck_pvalue(positions) == skill.luck_pvalue(positions)


def test_category_concentration_says_where_the_money_came_from():
    rows = [closed(pnl=90.0, cond="a"), closed(pnl=10.0, cond="b"), closed(pnl=-50.0, cond="c")]
    cats = {"a": "sports", "b": "crypto", "c": "politics"}
    top, hhi = skill.category_mix(rows, lambda p: cats[p["condition_id"]])
    assert top == "sports"
    assert hhi == pytest.approx(0.9 ** 2 + 0.1 ** 2)


# --- the lag replay -------------------------------------------------------


def prices(series):
    """A price_at over a {token: {ts: price}} table, reading backwards like the real one."""
    def price_at(token, ts):
        pts = series.get(token) or {}
        keys = [k for k in pts if k <= ts]
        return pts[max(keys)] if keys else None
    return price_at


def test_a_copier_pays_both_taker_fees_where_the_trader_may_not_have():
    trip = skill.RoundTrip("tok", "cond", 100, 400, 10, 0.50, 0.60)
    px = prices({"tok": {100: 0.50, 400: 0.60}})
    gross = (1 / 0.50) * 0.60 - 1.0
    assert replay.replay_one(trip, 0, px, rate=0.0) == pytest.approx(gross)
    assert replay.replay_one(trip, 0, px, rate=0.05) < gross


def test_an_edge_that_is_gone_in_a_minute_shows_up_as_a_decaying_curve():
    """This is the whole module in one test: they made 20%, and a copier made nothing."""
    px = prices({"tok": {100: 0.50, 115: 0.58, 160: 0.60, 400: 0.60, 460: 0.60}})
    trades = [trade(100, "BUY", 10, 0.50), trade(400, "SELL", 10, 0.60)]
    res = replay.replay("0xa", trades, px, lambda t: 0.0, lags=(0, 15, 60))

    assert res.at(0).copier_roi == pytest.approx(0.20)
    assert res.at(15).copier_roi == pytest.approx((1 / 0.58) * 0.60 - 1)
    assert res.at(60).copier_roi == pytest.approx(0.0)
    assert res.at(0).copier_roi > res.at(15).copier_roi > res.at(60).copier_roi


def test_capture_ratio_is_the_share_of_their_edge_that_survives_being_copied():
    px = prices({"tok": {100: 0.50, 115: 0.55, 400: 0.60, 415: 0.60}})
    trades = [trade(100, "BUY", 10, 0.50), trade(400, "SELL", 10, 0.60)]
    res = replay.replay("0xa", trades, px, lambda t: 0.0, lags=(0, 15))
    assert res.capture_ratio == pytest.approx(res.at(15).copier_roi / res.at(0).copier_roi)
    assert 0 < res.capture_ratio < 1


def test_capture_ratio_is_withheld_when_there_was_no_edge_to_capture():
    """Against a negative baseline the ratio reads backwards: getting worse looks like
    capturing more."""
    px = prices({"tok": {100: 0.60, 115: 0.58, 400: 0.50, 415: 0.50}})
    trades = [trade(100, "BUY", 10, 0.60), trade(400, "SELL", 10, 0.50)]
    assert replay.replay("0xa", trades, px, lambda t: 0.0, lags=(0, 15)).capture_ratio is None


def test_edge_half_life_interpolates_where_the_curve_crosses_zero():
    lags = [replay.LagResult(0, 5, 0, 0.10, 0.5, 1.0),
            replay.LagResult(60, 5, 0, 0.02, 0.1, 0.6),
            replay.LagResult(120, 5, 0, -0.02, -0.1, 0.4)]
    assert replay.edge_half_life(lags) == pytest.approx(90.0)


def test_edge_half_life_is_none_when_the_edge_never_dies_or_never_lived():
    alive = [replay.LagResult(0, 5, 0, 0.10, 0.5, 1.0), replay.LagResult(900, 5, 0, 0.08, 0.4, 1.0)]
    assert replay.edge_half_life(alive) is None
    stillborn = [replay.LagResult(0, 5, 0, -0.10, -0.5, 0.0),
                 replay.LagResult(900, 5, 0, -0.20, -1.0, 0.0)]
    assert replay.edge_half_life(stillborn) is None


def test_a_round_trip_with_no_price_coverage_is_counted_not_guessed():
    px = prices({"tok": {100: 0.50}})
    trades = [trade(100, "BUY", 10, 0.50), trade(400, "SELL", 10, 0.60),
              trade(500, "BUY", 10, 0.50, token="other"),
              trade(800, "SELL", 10, 0.60, token="other")]
    res = replay.replay("0xa", trades, px, lambda t: 0.0, lags=(0,))
    assert res.at(0).n == 1 and res.at(0).n_missing == 1


def test_replay_plans_price_windows_that_reach_past_the_longest_lag(con):
    trips = [skill.RoundTrip("tok", "cond", 1000, 2000, 10, 0.5, 0.6)]
    windows = replay.needed_windows(trips, lags=(0, 900))
    start, end = windows["tok"][0]
    assert start < 1000 and end > 2000 + 900


def test_windows_already_on_disk_are_not_planned_again(con):
    trips = [skill.RoundTrip("tok", "cond", 1000, 2000, 10, 0.5, 0.6)]
    assert replay.plan_backfill(con, trips, lags=(0,))
    for start, end in replay.needed_windows(trips, lags=(0,))["tok"]:
        store.record_window(con, "tok", start, end, 10)
    assert replay.plan_backfill(con, trips, lags=(0,)) == []


# --- ranking --------------------------------------------------------------


def a_score(**kw):
    base = dict(address="0xa", n_closed=50, win_rate=0.6, roi=0.2, realized_pnl=100.0,
                invested=500.0, brier=0.18, n_brier=50, avg_entry_price=0.5,
                avg_stake_usd=10.0, median_stake_usd=10.0, max_drawdown=0.1,
                consistency=0.8, last_close_ts=1, hold_p50_s=1200, hold_p10_s=600,
                fee_adjusted_roi=0.12, recent_roi=0.10, luck_p=0.01, top_category="sports",
                category_concentration=0.5)
    return skill.SkillScore(**{**base, **kw})


def a_replay(capture=0.6, half_life=600.0, coverage=0.9):
    return replay.ReplayResult("0xa", [], capture, half_life, 50, coverage)


def test_a_wallet_whose_edge_does_not_survive_being_copied_is_excluded_not_penalised():
    """It is not a near-miss to consider anyway. It failed a statement about candidacy."""
    r = rank.rank(a_score(), a_replay(capture=0.05))
    assert r.excluded and "does not survive" in r.excluded


def test_a_wallet_whose_edge_dies_before_we_could_act_is_excluded():
    r = rank.rank(a_score(), a_replay(half_life=20.0))
    assert r.excluded and "half-life" in r.excluded


def test_a_replay_drawn_through_a_minority_of_trades_may_not_exclude_a_wallet():
    """Thin coverage is a fact about which price windows we happened to fetch, not about the
    trader. Acting on it would quietly reject wallets for being unfamiliar."""
    thin = a_replay(capture=0.05, half_life=10.0, coverage=0.1)
    assert not thin.trustworthy
    assert rank.rank(a_score(), thin).excluded is None
    assert rank.components(a_score(), thin)["capture"] == 0.5, "and it may not score them either"


def test_a_well_covered_replay_still_excludes():
    assert rank.rank(a_score(), a_replay(capture=0.05, coverage=0.9)).excluded


def test_coverage_is_the_share_of_round_trips_priced_at_both_ends():
    px = prices({"tok": {100: 0.50, 115: 0.50, 400: 0.60, 415: 0.60}})
    trades = [trade(100, "BUY", 10, 0.50), trade(400, "SELL", 10, 0.60)]
    for i in range(3):                                   # three round trips with no quotes
        trades += [trade(500 + i, "BUY", 10, 0.50, token=f"dark{i}"),
                   trade(800 + i, "SELL", 10, 0.60, token=f"dark{i}")]
    res = replay.replay("0xa", trades, px, lambda t: 0.0, lags=(0, 15))
    assert res.coverage == pytest.approx(0.25)
    assert not res.trustworthy, "a quarter of the trades is not a verdict"


def test_a_thin_record_is_excluded_however_good_it_looks():
    assert rank.rank(a_score(n_closed=6, roi=3.0), a_replay()).excluded


def test_copyability_outranks_being_good():
    """A great trader we cannot follow is worth zero; a decent one we can follow is worth
    something. The weights have to say so."""
    great_uncopyable = rank.rank(a_score(roi=0.9, fee_adjusted_roi=0.8, brier=0.05),
                                 a_replay(capture=0.32, half_life=70.0))
    decent_copyable = rank.rank(a_score(roi=0.15, fee_adjusted_roi=0.10, brier=0.20),
                                a_replay(capture=0.75, half_life=900.0))
    assert decent_copyable.rank_score > great_uncopyable.rank_score


def test_a_wallet_that_scalps_scores_worse_than_one_that_flips():
    fast = rank.rank(a_score(hold_p50_s=40, hold_p10_s=10), a_replay())
    flip = rank.rank(a_score(hold_p50_s=1200, hold_p10_s=600), a_replay())
    assert flip.rank_score > fast.rank_score


def test_a_fat_tail_of_scalps_is_penalised_even_behind_a_healthy_median():
    """Those trades are skipped as stale, which shows up as a low fill rate, not as a loss."""
    assert rank.hold_score(1200, p10_s=20) < rank.hold_score(1200, p10_s=600)


def test_a_brier_score_over_four_markets_is_discounted_toward_neutral():
    assert rank.brier_score(0.10, n=4) < rank.brier_score(0.10, n=50)
    assert rank.brier_score(0.10, n=4) > 0.5


def test_guessing_fifty_fifty_scores_the_zero_of_the_brier_scale():
    assert rank.brier_score(0.25, n=100) == pytest.approx(0.0)


def test_a_missing_metric_neither_rewards_nor_punishes():
    """Absent data must not flatter a wallet, and must not condemn one either."""
    assert rank.components(a_score(luck_p=None))["luck"] == 0.5
    assert rank.components(a_score(hold_p50_s=None))["hold"] == 0.5


def test_every_component_is_bounded_so_one_enormous_number_cannot_buy_a_ranking():
    comp = rank.components(a_score(roi=500.0, fee_adjusted_roi=500.0, recent_roi=500.0),
                           a_replay(capture=50.0))
    assert all(0.0 <= v <= 1.0 for v in comp.values())
    assert rank.rank(a_score(fee_adjusted_roi=500.0), a_replay()).rank_score <= 1.0


def test_persona_fit_is_separate_from_being_good():
    """A superb slow-thesis trader ranks well and fits a quick-flip task badly."""
    slow = a_score(hold_p50_s=5 * 3600, hold_p10_s=3600)
    quick_task = Task(name="t", trader="0x1", hold="quick_flips")
    assert rank.persona_fit(slow, quick_task) < rank.persona_fit(a_score(), quick_task)


def test_persona_fit_prefers_a_wallet_that_earns_in_the_category_asked_for():
    t = Task(name="t", trader="0x1", category="sports")
    assert rank.persona_fit(a_score(top_category="sports"), t) \
        > rank.persona_fit(a_score(top_category="crypto"), t)


# --- the sweep ------------------------------------------------------------


class FakeLeaderboard:
    """Every board returns one wallet, so the pool is exactly the sweep's own breadth."""

    def __init__(self):
        self.asked = []

    def leaderboard(self, client, offset=0, limit=50, category="OVERALL",
                    time_period="ALL", order_by="PNL"):
        self.asked.append((category, time_period, order_by))
        if offset:
            return []
        return [{"proxyWallet": f"0x{category[:3]}{time_period[:1]}".lower(), "rank": 1,
                 "userName": "u", "vol": 100.0, "pnl": 10.0}]


def test_the_sweep_reads_every_category_window_and_ordering(monkeypatch):
    """One slice returns the same whales every time; the breadth is the point."""
    fake = FakeLeaderboard()
    monkeypatch.setattr(sweep.api, "leaderboard", fake.leaderboard)
    sweep.sweep(None, log=lambda *a: None)
    from polywatch.config import (LEADERBOARD_CATEGORIES, LEADERBOARD_ORDERINGS,
                                  LEADERBOARD_PERIODS)
    assert len(set(fake.asked)) == (len(LEADERBOARD_CATEGORIES) * len(LEADERBOARD_PERIODS)
                                    * len(LEADERBOARD_ORDERINGS))


def test_a_wallet_on_many_boards_records_every_one_of_them(monkeypatch):
    """Appearing on eight boards is a different proposition from one day's crypto list."""
    def one_wallet(client, **kw):
        return [] if kw.get("offset") else [{"proxyWallet": "0xa", "rank": 3, "vol": 5.0,
                                             "pnl": 1.0}]
    monkeypatch.setattr(sweep.api, "leaderboard", one_wallet)
    pool = sweep.sweep(None, categories=("SPORTS", "CRYPTO"), periods=("DAY",),
                       orderings=("PNL",), log=lambda *a: None)
    assert pool["0xa"].appearances == 2


def test_the_sweep_keeps_the_best_rank_a_wallet_reached_anywhere(monkeypatch):
    ranks = iter([9, 2])

    def varying(client, **kw):
        return [] if kw.get("offset") else [{"proxyWallet": "0xa", "rank": next(ranks),
                                             "vol": 5.0, "pnl": 1.0}]
    monkeypatch.setattr(sweep.api, "leaderboard", varying)
    pool = sweep.sweep(None, categories=("SPORTS", "CRYPTO"), periods=("DAY",),
                       orderings=("PNL",), log=lambda *a: None)
    assert pool["0xa"].best_rank == 2


def test_a_board_that_fails_does_not_lose_the_other_hundred_and_ninety_nine(monkeypatch):
    from polywatch.fetch.client import FetchError

    def flaky(client, **kw):
        if kw.get("category") == "CRYPTO":
            raise FetchError("502")
        return [] if kw.get("offset") else [{"proxyWallet": "0xa", "rank": 1, "vol": 5.0,
                                             "pnl": 1.0}]
    monkeypatch.setattr(sweep.api, "leaderboard", flaky)
    pool = sweep.sweep(None, categories=("SPORTS", "CRYPTO"), periods=("DAY",),
                       orderings=("PNL",), log=lambda *a: None)
    assert "0xa" in pool


def test_the_pool_lands_in_wallets_so_the_existing_screen_can_run_over_it(con):
    pool = {"0xa": sweep.Candidate("0xa", username="u", vol=5.0, pnl=1.0, best_rank=3,
                                   boards=["SPORTS/DAY/PNL", "CRYPTO/WEEK/VOL"])}
    sweep.persist(con, pool)
    row = con.execute("SELECT * FROM wallets WHERE address='0xa'").fetchone()
    assert row["rank"] == 3 and row["source"] == "sweep:2"


# --- persistence ----------------------------------------------------------


def test_a_ranked_wallet_is_finally_returned_by_the_shortlist_query(con):
    """rank_score was hardcoded None, so this query could never return a row."""
    store.upsert_trader_score(con, {"address": "0xa", "n_closed": 40, "rank_score": 0.8})
    store.upsert_trader_score(con, {"address": "0xb", "n_closed": 40, "rank_score": 0.6})
    rows = store.top_trader_scores(con)
    assert [r["address"] for r in rows] == ["0xa", "0xb"]


def test_an_excluded_wallet_is_held_back_from_the_shortlist(con):
    store.upsert_trader_score(con, {"address": "0xa", "n_closed": 40, "rank_score": 0.9,
                                    "excluded": "edge does not survive being copied"})
    store.upsert_trader_score(con, {"address": "0xb", "n_closed": 40, "rank_score": 0.3})
    assert [r["address"] for r in store.top_trader_scores(con)] == ["0xb"]
    assert len(store.top_trader_scores(con, include_excluded=True)) == 2


def test_rescanning_a_wallet_replaces_its_whole_decay_curve(con):
    store.upsert_trader_replay(con, "0xa", [
        {"lag_s": 0, "n": 5, "n_missing": 0, "copier_roi": 0.1, "copier_pnl": 1.0,
         "win_rate": 0.6}])
    store.upsert_trader_replay(con, "0xa", [
        {"lag_s": 0, "n": 9, "n_missing": 1, "copier_roi": 0.2, "copier_pnl": 2.0,
         "win_rate": 0.7}])
    rows = store.trader_replay(con, "0xa")
    assert len(rows) == 1 and rows[0]["n"] == 9
