"""Phase 1: the fetch/parse/store foundation the copy trader is built on.

Fixtures under tests/fixtures/ are real captured Polymarket payloads, not hand-written ones,
so a rename upstream surfaces here rather than in a live run.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from polywatch.copytrade import skill
from polywatch.db import store
from polywatch.parse import records

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(), strict=False)


# --- endpoint parameters --------------------------------------------------


def test_leaderboard_sends_the_param_names_the_server_actually_reads():
    """The original probe guessed `window` and `rankBy`, which the server drops in silence --
    so every sweep returned the same PnL-desc page and looked like the endpoint had no filters."""
    seen = {}

    class FakeClient:
        def get_json(self, url, params=None, **kw):
            seen.update(params)
            return []

    from polywatch.fetch import polymarket as api
    api.leaderboard(FakeClient(), category="CRYPTO", time_period="WEEK", order_by="VOL")

    assert seen["category"] == "CRYPTO"
    assert seen["timePeriod"] == "WEEK"
    assert seen["orderBy"] == "VOL"
    assert "window" not in seen and "rankBy" not in seen


def test_closed_positions_defaults_to_time_order_not_pnl_order():
    """The server's own default is REALIZEDPNL desc, which makes any sampled prefix a list of
    the wallet's best trades. Scoring that gave a 100% win rate for a trader who loses 44% of
    the time."""
    seen = {}

    class FakeClient:
        def get_json(self, url, params=None, **kw):
            seen.update(params)
            return []

    from polywatch.fetch import polymarket as api
    api.closed_positions(FakeClient(), "0xabc")
    assert seen["sortBy"] == "TIMESTAMP"
    assert seen["sortDirection"] == "DESC"


# --- parsers --------------------------------------------------------------


def test_activity_parses_and_lowercases_the_wallet():
    rows = records.parse_activity(fixture("activity"))
    assert rows
    r = rows[0]
    assert r["wallet"] == r["wallet"].lower()
    assert r["kind"] == "TRADE"
    assert r["side"] in ("BUY", "SELL")
    assert r["ts"] > 1_700_000_000
    assert r["usdc_size"] >= 0
    assert r["tx_hash"].startswith("0x")


def test_activity_tolerates_events_that_have_no_side():
    """A REDEEM has no side and no price, and dropping those rows would hide the way a
    position can leave the book without a sell."""
    redeem = [{"proxyWallet": "0xABC", "type": "REDEEM", "timestamp": 1788000000,
               "conditionId": "0xcond", "asset": "123", "size": 10}]
    row = records.parse_activity(redeem)[0]
    assert row["kind"] == "REDEEM"
    assert row["side"] == ""
    assert row["price"] == 0.0


def test_closed_positions_derive_cost_from_shares_and_entry_price():
    rows = records.parse_closed_positions(fixture("closed_positions"))
    assert rows
    for r in rows:
        assert r["cost"] == pytest.approx(r["shares"] * r["avg_price"])


def test_positions_carry_polymarkets_own_marks():
    rows = records.parse_positions(fixture("positions"))
    assert rows
    assert {"shares", "avg_price", "cash_pnl", "pct_pnl", "entry_fees"} <= set(rows[0])


def test_book_is_sorted_best_price_first_on_both_sides():
    """Upstream sends both sides price-ascending, so the best bid is last and the best ask is
    first -- an asymmetry that silently produces a plausible wrong price if you assume either
    ordering. The parser normalises it."""
    book = records.parse_book(fixture("book"))
    bids = [p for p, _ in book["bids"]]
    asks = [p for p, _ in book["asks"]]
    assert bids == sorted(bids, reverse=True), "best bid must be first"
    assert asks == sorted(asks), "best ask must be first"
    if bids and asks:
        assert bids[0] < asks[0], "book must not be crossed"


def test_book_casts_string_prices_to_float():
    book = records.parse_book(fixture("book"))
    for price, size in book["bids"] + book["asks"]:
        assert isinstance(price, float) and isinstance(size, float)


def test_market_parses_execution_constraints_and_gamma_id():
    """Without gamma's numeric id, /markets/{id}/tags is uncallable and per-trade category
    logging is impossible."""
    market, assets = records.parse_market(fixture("market")[0])
    assert market["gamma_id"]
    assert market["tick_size"] in (0.1, 0.01, 0.001, 0.0001)
    assert market["order_min_size"] > 0
    assert market["accepting_orders"] in (0, 1)
    assert assets and all(a["condition_id"] == market["condition_id"] for a in assets)


@pytest.mark.parametrize("fee_type,expected", [
    ("sports_fees_v3", "sports"),
    ("crypto_fees_v2", "crypto"),
    ("politics_fees", "politics"),
    ("geopolitics_fees", "geopolitics"),   # must not match the shorter 'politics'
    ("", None),
    (None, None),
])
def test_category_is_derived_from_fee_type(fee_type, expected):
    assert records.derive_category(fee_type) == expected


# --- store ----------------------------------------------------------------


@pytest.fixture()
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init_db(c)
    return c


def test_migration_adds_market_columns_to_an_older_database(tmp_path):
    """The live database predates the trading columns, and CREATE TABLE IF NOT EXISTS will not
    add a column to a table that already exists."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE markets (condition_id TEXT PRIMARY KEY, fetched_at INTEGER)")
    old.execute("INSERT INTO markets VALUES ('0xdead', 1)")
    old.commit()
    old.close()

    c = store.connect(path)
    store.init_db(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(markets)")}
    assert {"gamma_id", "tick_size", "order_min_size", "accepting_orders"} <= cols
    assert c.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 1


def _task(name="t"):
    return {"name": name, "trader": "0xabc", "mode": "paper", "bankroll": 100.0,
            "buy_method": "fixed", "fixed_usd": 12.0, "max_market_usd": 25.0,
            "max_concurrent": 5, "slippage": 0.07, "sl_kind": "pct", "sl_value": 0.3,
            "tp_kind": "pct", "tp_value": 0.4, "behavior": "buys_sells", "risk": "moderate",
            "category": "CRYPTO", "style": "value_hunter", "hold": "quick_flips",
            "activity": "active", "config_json": "{}"}


def test_task_round_trips(con):
    store.upsert_task(con, _task())
    row = store.get_task(con, "t")
    assert row["trader"] == "0xabc" and row["slippage"] == 0.07
    assert row["style"] == "value_hunter"


def test_upserting_a_task_updates_rather_than_duplicates(con):
    store.upsert_task(con, _task())
    store.upsert_task(con, {**_task(), "slippage": 0.05})
    assert len(store.list_tasks(con)) == 1
    assert store.get_task(con, "t")["slippage"] == 0.05


def _signal(run_id, **kw):
    base = {"run_id": run_id, "trader": "0xabc", "kind": "TRADE", "tx_hash": "0xtx",
            "token_id": "tok", "condition_id": "cond", "side": "BUY", "size": 10.0,
            "price": 0.4, "usdc_size": 4.0, "trader_ts": 1788000000,
            "action": "copied", "reason": None}
    return {**base, **kw}


def test_a_repeated_signal_is_dropped_by_the_database(con):
    """Consecutive polls always overlap. Dedupe lives in a UNIQUE constraint rather than an
    in-memory set precisely because a set is lost on restart -- which is exactly when copying a
    stale trade would be most expensive."""
    store.upsert_task(con, _task())
    run = store.start_run(con, "t", "paper", 100.0)
    assert store.insert_signal(con, _signal(run)) is not None
    assert store.insert_signal(con, _signal(run)) is None
    assert len(store.signals(con, run)) == 1


def test_a_different_fill_in_the_same_transaction_is_a_new_signal(con):
    """One transaction can carry several fills, so tx_hash alone is not a key."""
    store.upsert_task(con, _task())
    run = store.start_run(con, "t", "paper", 100.0)
    store.insert_signal(con, _signal(run, size=10.0))
    assert store.insert_signal(con, _signal(run, size=25.0)) is not None
    assert len(store.signals(con, run)) == 2


def test_skip_reasons_are_countable(con):
    store.upsert_task(con, _task())
    run = store.start_run(con, "t", "paper", 100.0)
    store.insert_signal(con, _signal(run, tx_hash="0x1", action="skipped", reason="min_size"))
    store.insert_signal(con, _signal(run, tx_hash="0x2", action="skipped", reason="min_size"))
    store.insert_signal(con, _signal(run, tx_hash="0x3", action="skipped", reason="price_ceiling"))
    assert store.skip_reasons(con, run) == [("min_size", 2), ("price_ceiling", 1)]


def test_market_exposure_sums_every_outcome_of_one_market(con):
    """Holding YES and NO of the same market is two positions but one market's worth of risk."""
    store.upsert_task(con, _task())
    run = store.start_run(con, "t", "paper", 100.0)
    for tok in ("yes", "no"):
        store.open_position(con, {"run_id": run, "token_id": tok, "condition_id": "cond",
                                  "shares": 10.0, "avg_price": 0.5, "cost_usd": 5.0,
                                  "fees_paid": 0.1})
    assert store.market_exposure(con, run, "cond") == pytest.approx(10.2)


def test_closing_a_position_nets_out_both_sides_fees(con):
    store.upsert_task(con, _task())
    run = store.start_run(con, "t", "paper", 100.0)
    pid = store.open_position(con, {"run_id": run, "token_id": "tok", "condition_id": "cond",
                                    "shares": 20.0, "avg_price": 0.5, "cost_usd": 10.0,
                                    "fees_paid": 0.25})
    pnl = store.close_position(con, pid, proceeds_usd=13.0, exit_fee=0.30, reason="take_profit")
    assert pnl == pytest.approx(13.0 - 10.0 - 0.25 - 0.30)
    row = store.open_positions(con, run)
    assert row == []


def test_adding_to_a_position_moves_the_average_price(con):
    """If avg_price did not move with the new cost, the stop-loss would keep measuring against
    the first entry only -- which is how a position ends up with no working stop."""
    store.upsert_task(con, _task())
    run = store.start_run(con, "t", "paper", 100.0)
    pid = store.open_position(con, {"run_id": run, "token_id": "tok", "condition_id": "cond",
                                    "shares": 10.0, "avg_price": 0.40, "cost_usd": 4.0,
                                    "fees_paid": 0.0})
    store.add_to_position(con, pid, shares=10.0, cost_usd=6.0, fee=0.0)
    pos = store.open_position_for(con, run, "tok")
    assert pos["shares"] == 20.0
    assert pos["avg_price"] == pytest.approx(0.50)


def test_cash_tracks_money_out_and_back(con):
    store.upsert_task(con, _task())
    run = store.start_run(con, "t", "paper", 100.0)
    pid = store.open_position(con, {"run_id": run, "token_id": "tok", "condition_id": "cond",
                                    "shares": 20.0, "avg_price": 0.5, "cost_usd": 10.0,
                                    "fees_paid": 0.25})
    assert store.run_cash(con, run) == pytest.approx(100.0 - 10.25)
    store.close_position(con, pid, proceeds_usd=12.0, exit_fee=0.0, reason="tp")
    assert store.run_cash(con, run) == pytest.approx(101.75)


def test_trader_score_upserts_in_place(con):
    store.upsert_trader_score(con, {"address": "0xabc", "n_closed": 10, "win_rate": 0.5,
                                    "roi": 0.1, "realized_pnl": 5.0, "brier": 0.2,
                                    "avg_entry_price": 0.3, "avg_stake_usd": 20.0,
                                    "max_drawdown": 0.1, "consistency": 0.8,
                                    "est_account_usd": 500.0, "top_category": "crypto",
                                    "persona_fit": None, "rank_score": 1.0,
                                    "metrics_json": "{}"})
    store.upsert_trader_score(con, {"address": "0xabc", "n_closed": 20, "win_rate": 0.6,
                                    "roi": 0.2, "realized_pnl": 9.0, "brier": 0.2,
                                    "avg_entry_price": 0.3, "avg_stake_usd": 20.0,
                                    "max_drawdown": 0.1, "consistency": 0.8,
                                    "est_account_usd": 500.0, "top_category": "crypto",
                                    "persona_fit": None, "rank_score": 2.0,
                                    "metrics_json": "{}"})
    row = store.get_trader_score(con, "0xABC")
    assert row["n_closed"] == 20
    assert len(store.top_trader_scores(con)) == 1


# --- skill metrics --------------------------------------------------------


def _pos(pnl, cost, avg_price, cur_price, end_ts):
    return {"realized_pnl": pnl, "cost": cost, "avg_price": avg_price,
            "cur_price": cur_price, "end_ts": end_ts, "shares": cost / max(avg_price, 1e-9)}


JAN, FEB, MAR = 1767225600, 1769904000, 1772323200


def test_win_rate_counts_profit_not_correctness():
    """A trader who buys at 0.95 and is right nine times in ten still loses money, so the
    outcome is not the thing worth counting."""
    closed = [_pos(-1.0, 95.0, 0.95, 1, JAN)] * 9 + [_pos(-95.0, 95.0, 0.95, 0, JAN)]
    assert skill.win_rate(closed) == 0.0


def test_roi_is_pnl_over_capital_deployed():
    closed = [_pos(50.0, 100.0, 0.2, 1, JAN), _pos(-100.0, 100.0, 0.4, 0, FEB)]
    r, pnl, invested = skill.roi(closed)
    assert (pnl, invested) == (-50.0, 200.0)
    assert r == pytest.approx(-0.25)


def test_brier_is_the_mean_squared_error_of_the_entry_price():
    closed = [_pos(50.0, 100.0, 0.2, 1, JAN), _pos(-100.0, 100.0, 0.4, 0, FEB)]
    score, n = skill.brier(closed)
    assert n == 2
    assert score == pytest.approx(((0.2 - 1) ** 2 + (0.4 - 0) ** 2) / 2)


def test_brier_ignores_markets_that_never_settled_cleanly():
    """Brier needs a hard 0/1 target; a price of 0.6 is a market opinion, not a result."""
    closed = [_pos(1.0, 10.0, 0.2, 1, JAN), _pos(0.0, 10.0, 0.5, 0.6, FEB)]
    score, n = skill.brier(closed)
    assert n == 1
    assert score == pytest.approx((0.2 - 1) ** 2)


def test_brier_is_none_when_nothing_settled():
    assert skill.brier([_pos(0.0, 10.0, 0.5, 0.6, JAN)]) == (None, 0)


def test_entry_price_is_weighted_by_cost():
    """One $5 lottery ticket must not drag the average of a book full of $500 positions."""
    closed = [_pos(0.0, 5.0, 0.05, 0, JAN), _pos(0.0, 500.0, 0.80, 1, JAN)]
    avg_entry, _, _ = skill.entry_stats(closed)
    assert avg_entry == pytest.approx((0.05 * 5 + 0.80 * 500) / 505)


def test_max_drawdown_is_measured_against_capital_staked():
    """Against a running peak that starts at zero, a $100 fall from a $50 peak reads as 200% --
    arithmetically correct and useless."""
    closed = [_pos(50.0, 100.0, 0.5, 1, JAN), _pos(-100.0, 100.0, 0.5, 0, FEB)]
    assert skill.max_drawdown(closed) == pytest.approx(100.0 / 200.0)


def test_a_curve_that_only_rises_has_no_drawdown():
    closed = [_pos(10.0, 50.0, 0.5, 1, JAN), _pos(10.0, 50.0, 0.5, 1, FEB)]
    assert skill.max_drawdown(closed) == 0.0


def test_consistency_is_the_share_of_months_in_profit():
    """Separates a trader who is up nine months in ten from one whose whole record is a single
    enormous month, at identical total PnL."""
    closed = [_pos(100.0, 10.0, 0.5, 1, JAN),
              _pos(-1.0, 10.0, 0.5, 0, FEB),
              _pos(-1.0, 10.0, 0.5, 0, MAR)]
    assert skill.consistency(closed) == pytest.approx(1 / 3)


def test_scoring_an_empty_record_does_not_explode():
    sc = skill.score("0xabc", [])
    assert sc.n_closed == 0 and sc.win_rate == 0.0 and sc.brier is None
