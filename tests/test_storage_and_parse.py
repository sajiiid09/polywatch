"""Step 2/3 tests: interval algebra, parsers, and the hot query. No network."""

import json

import pytest

from polywatch.db import store
from polywatch.ingest import chunk, merge_intervals, subtract
from polywatch.parse import records
from polywatch.parse.fields import FieldError, iso_ts


# --- interval algebra: this is what makes resume free and keeps request counts sane ---

def test_merge_joins_overlapping_and_near_windows():
    assert merge_intervals([(0, 10), (5, 20), (100, 110)]) == [(0, 20), (100, 110)]
    assert merge_intervals([(0, 10), (30, 40)], gap=25) == [(0, 40)]
    assert merge_intervals([]) == []


def test_subtract_removes_covered_ranges():
    assert subtract([(0, 100)], [(20, 40)]) == [(0, 20), (40, 100)]
    assert subtract([(0, 100)], [(0, 100)]) == []
    assert subtract([(0, 100)], []) == [(0, 100)]
    assert subtract([(0, 100)], [(0, 30), (30, 60)]) == [(60, 100)]


def test_chunk_splits_long_spans():
    out = chunk([(0, 250)], max_span=100)
    assert out == [(0, 100), (100, 200), (200, 250)]


# --- parsers ---

def _market(**over):
    base = {
        "conditionId": "0xabc",
        "question": "Will X happen?",
        "slug": "will-x",
        "category": "Sports",
        "closed": True,
        "active": True,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
        "clobTokenIds": '["111", "222"]',
        "umaResolutionStatuses": '["resolved"]',
        "endDate": "2026-09-05T14:00:00Z",
        "feesEnabled": True,
        "feeType": "sports_fees_v3",
        "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15},
    }
    base.update(over)
    return base


def test_market_resolution_and_assets():
    m, assets = records.parse_market(_market())
    assert m["resolved"] == 1 and m["winning_index"] == 0
    assert m["fee_rate"] == 0.05 and m["fee_source"] == "market"
    assert [a["token_id"] for a in assets] == ["111", "222"]
    assert assets[1]["outcome"] == "No" and assets[1]["outcome_index"] == 1


def test_market_unresolved_when_prices_not_binary():
    # 0.98 is a market opinion, not a result -- Brier needs a hard outcome.
    m, _ = records.parse_market(_market(outcomePrices='["0.98", "0.02"]'))
    assert m["resolved"] == 0 and m["winning_index"] is None
    m, _ = records.parse_market(_market(closed=False, outcomePrices='["1", "0"]'))
    assert m["resolved"] == 0


def test_fee_fallback_when_market_ships_no_schedule():
    rec = _market(category="Crypto", feeType="crypto_fees")
    rec.pop("feeSchedule")
    m, _ = records.parse_market(rec)
    assert m["fee_rate"] == 0.07 and m["fee_source"] == "fallback"


def test_trade_parsing_normalises_wallet_and_side():
    rows = records.parse_trades([{
        "proxyWallet": "0xABC", "side": "buy", "asset": "111", "conditionId": "0xabc",
        "size": 29, "price": 0.45, "timestamp": 1788617946, "outcome": "Yes",
        "outcomeIndex": 0, "transactionHash": "0xdead",
    }])
    assert rows[0]["wallet"] == "0xabc" and rows[0]["side"] == "BUY"


def test_missing_field_names_itself():
    with pytest.raises(FieldError) as e:
        records.parse_trades([{"proxyWallet": "0xabc"}])
    assert "asset" in str(e.value)


def test_iso_ts_handles_gammas_three_date_formats():
    assert iso_ts({"d": "2026-09-05T14:00:00Z"}, "d", "t") == 1788616800
    assert iso_ts({"d": "2020-11-02 16:31:01+00"}, "d", "t") == 1604334661
    assert iso_ts({"d": ""}, "d", "t") is None


# --- storage ---

def test_price_at_returns_last_quote_at_or_before(tmp_path):
    con = store.connect(tmp_path / "t.db")
    store.init_db(con)
    store.insert_prices(con, "111", [(100, 0.4), (160, 0.5), (220, 0.6)])
    assert store.price_at(con, "111", 100) == 0.4
    assert store.price_at(con, "111", 200) == 0.5   # no quote at 200, last one stands
    assert store.price_at(con, "111", 99) is None   # before any quote: no guess
    assert store.price_at(con, "222", 200) is None


def test_trade_insert_is_idempotent(tmp_path):
    con = store.connect(tmp_path / "t.db")
    store.init_db(con)
    rows = records.parse_trades([{
        "proxyWallet": "0xabc", "side": "BUY", "asset": "111", "conditionId": "0xabc",
        "size": 1, "price": 0.5, "timestamp": 10, "outcome": "Yes", "outcomeIndex": 0,
        "transactionHash": "0xdead",
    }])
    assert store.insert_trades(con, rows) == 1
    assert store.insert_trades(con, rows) == 0


def test_same_tx_two_fills_both_stored(tmp_path):
    # One transaction can carry several fills; tx_hash alone must not dedupe them away.
    con = store.connect(tmp_path / "t.db")
    store.init_db(con)
    base = {"proxyWallet": "0xabc", "side": "BUY", "asset": "111", "conditionId": "0xabc",
            "timestamp": 10, "outcome": "Yes", "outcomeIndex": 0, "transactionHash": "0xdead"}
    rows = records.parse_trades([{**base, "size": 1, "price": 0.5},
                                 {**base, "size": 2, "price": 0.51}])
    assert store.insert_trades(con, rows) == 2


def test_market_upsert_updates_resolution(tmp_path):
    con = store.connect(tmp_path / "t.db")
    store.init_db(con)
    open_m, _ = records.parse_market(_market(closed=False, outcomePrices='["0.5","0.5"]'))
    store.upsert_markets(con, [open_m])
    assert store.counts(con)["markets_resolved"] == 0
    settled, _ = records.parse_market(_market())
    store.upsert_markets(con, [settled])
    assert store.counts(con)["markets"] == 1
    assert store.counts(con)["markets_resolved"] == 1
