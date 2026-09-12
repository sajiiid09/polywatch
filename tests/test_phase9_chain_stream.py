"""Phase 9: seeing a copied wallet's fill from the chain instead of waiting to be told.

Phase 5's docstring says entry latency "cannot be engineered away". That was measured honestly
and concluded wrongly: it is true of Polymarket's websockets and false of the Polygon logs the
fills settle into. data-api's timestamp for a fill *is* the block timestamp, so the 13-21s of
feed lag in every recorded run was the indexer's queue, not the trade being unknowable.

These tests hold that claim to the fixtures. The decoder is checked against a real captured
OrderFilled log and the real /activity record for the same transaction, so "identical" means
identical rather than approximately so. The rest is about the two properties that make the
second route safe to add: it cannot cause a double buy, and it cannot make the run worse when
it fails.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from polywatch.copytrade import tradestream
from polywatch.copytrade.engine import Engine
from polywatch.copytrade.execution import PaperExecutor
from polywatch.copytrade.task import Task, preset
from polywatch.db import store
from polywatch.parse import records

from test_copytrade_engine import FakeAPI, activity_event, book_of

FIXTURES = Path(__file__).parent / "fixtures"
TARGET = "0x67ac9e1ad7d7e74ef0215d14fc8edb538e4fedf1"


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init_db(c)
    return c


def _load(stem):
    fx = json.loads((FIXTURES / f"orderfilled{stem}_log.json").read_text())
    act = json.loads((FIXTURES / f"activity_for_orderfilled{stem}.json").read_text())
    return fx["logs"], int(fx["block_timestamp"], 16), records.parse_activity(act)[0]


@pytest.fixture
def captured():
    """One real buy, as the chain saw it and as data-api reported it."""
    return _load("")


@pytest.fixture
def captured_sell():
    """And one real sell, which is the half an earlier decoder silently threw away."""
    return _load("_sell")


# --- the decoder against the real thing -----------------------------------


def test_the_chain_reconstructs_data_apis_record_exactly(captured):
    """Not "close to". The same token, the same shares, the same dollars, the same second.

    If this ever drifts, the two routes stop deduping against each other and the run can buy the
    same fill twice -- so an approximate comparison here would be testing the wrong thing.
    """
    logs, ts, expected = captured
    got = [e for e in (tradestream.decode_order_filled(l, TARGET, ts) for l in logs) if e]
    assert len(got) == 1, "exactly one leg of this match belongs to the wallet we copy"
    ev = got[0]
    for field in ("wallet", "kind", "token_id", "side", "size", "price", "usdc_size",
                  "ts", "tx_hash"):
        assert ev[field] == expected[field], field


def test_the_block_timestamp_is_what_data_api_would_have_called_the_trade_time(captured):
    """The whole argument for this module in one assertion."""
    _logs, ts, expected = captured
    assert ts == expected["ts"]


def test_a_log_belonging_to_nobody_we_copy_is_ignored(captured):
    logs, ts, _ = captured
    assert tradestream.decode_order_filled(logs[0], "0xdeadbeef" + "0" * 31, ts) is None


def test_a_sell_decodes_as_a_sell(captured_sell):
    """The regression that matters most, so it gets its own fixture and its own name.

    The first cut read word0 as `makerAssetId` and called the collateral leg the buyer. For a
    buy the flag happens to be 0 and that reading is accidentally right; for a sell it is 1 and
    the reading finds no collateral leg at all, so every sell a copied wallet made was dropped
    without a trace. Measured against the live feed at 52.8% coverage -- and 0% on a wallet that
    was, that afternoon, only selling.
    """
    logs, ts, expected = captured_sell
    got = [e for e in (tradestream.decode_order_filled(l, expected["wallet"], ts) for l in logs)
           if e]
    assert len(got) == 1
    for field in ("side", "size", "price", "usdc_size", "token_id", "ts"):
        assert got[0][field] == expected[field], field
    assert got[0]["side"] == "SELL"


def test_the_taker_is_the_other_side_of_the_same_trade(captured):
    """The side flag is written from the maker's point of view, so the taker's is its opposite.

    Which is why the roster is subscribed on both the maker and the taker topic rather than
    assuming end users only ever land in one slot.
    """
    logs, ts, _ = captured
    log = logs[0]
    maker = "0x" + log["topics"][2][-40:].lower()
    taker = "0x" + log["topics"][3][-40:].lower()
    as_maker = tradestream.decode_order_filled(log, maker, ts)
    as_taker = tradestream.decode_order_filled(log, taker, ts)
    assert as_maker["side"] == "BUY" and as_taker["side"] == "SELL"
    # Same trade: same token, same shares, same dollars. Only the label flips.
    for field in ("token_id", "size", "usdc_size", "price"):
        assert as_maker[field] == as_taker[field], field


def test_a_frame_we_cannot_read_is_declined_rather_than_guessed_at(captured):
    logs, ts, _ = captured
    bad = dict(logs[0], data="0x00")
    assert tradestream.decode_order_filled(bad, TARGET, ts) is None
    wrong_topic = dict(logs[0], topics=["0x" + "11" * 32] + logs[0]["topics"][1:])
    assert tradestream.decode_order_filled(wrong_topic, TARGET, ts) is None
    unknown_flag = dict(logs[0], data="0x" + format(7, "064x") + logs[0]["data"][2 + 64:])
    assert tradestream.decode_order_filled(unknown_flag, TARGET, ts) is None, \
        "a side flag we do not recognise has no side we can honestly name"
    no_token = dict(logs[0], data="0x" + format(0, "064x") * 2 + logs[0]["data"][2 + 128:])
    assert tradestream.decode_order_filled(no_token, TARGET, ts) is None


def test_an_address_becomes_a_padded_topic():
    assert (tradestream.topic_for_address(TARGET)
            == "0x00000000000000000000000067ac9e1ad7d7e74ef0215d14fc8edb538e4fedf1")


# --- the float that could have bought twice -------------------------------


@pytest.mark.parametrize("micro", [825090910, 952750000, 1, 999999999999, 333333333])
def test_both_routes_compute_the_identical_float(micro):
    """A uint256 divided by 1e6, and Polymarket's decimal string parsed -- bit-identical.

    `signals` is UNIQUE on (run_id, tx_hash, token_id, side, size) with `size` stored as REAL.
    One ULP of disagreement between the two routes means the constraint does not fire and the
    run buys the same fill twice, which is the failure the constraint exists to prevent.
    """
    chain = round(micro / 10 ** 6, records.AMOUNT_DP)
    feed = records.parse_activity([activity_event(size=f"{micro / 10 ** 6:.6f}")])[0]["size"]
    assert chain == feed
    assert chain.hex() == feed.hex(), "equal is not enough; they must be the same float"


# --- the engine: one fill, two routes, one order --------------------------


def engine_with_chain(con, feed, books, events, now=1010):
    """An engine whose chain feed is a list rather than a socket."""
    import polywatch.copytrade.engine as engine_mod
    engine_mod.api = FakeAPI(feed, books)
    t = Task(name="t", trader="0xtrader", **preset("quick_flips"))
    store.upsert_task(con, t.as_row())
    eng = Engine(con, t, PaperExecutor(), client=None, log=lambda *a: None, now=lambda: now)
    eng._trader_account = lambda _a: 10_000.0
    eng.trades = FakeTradeStream(events)
    eng.start()
    return eng


class FakeTradeStream:
    def __init__(self, events):
        self._events = list(events)
        self.changed = threading.Event()
        self.live = True
        if events:
            self.changed.set()

    def drain(self, limit: int = 256):
        out, self._events = self._events[:limit], self._events[limit:]
        return out

    def stop(self):
        self.live = False


def chain_event(**kw):
    base = {"wallet": "0xtrader", "kind": "TRADE", "token_id": "tok", "condition_id": "cond",
            "side": "BUY", "size": 100.0, "price": 0.50, "usdc_size": 50.0, "ts": 1000,
            "outcome": "Yes", "outcome_index": 0, "tx_hash": "0xtx", "title": None,
            "slug": None, "source": "chain"}
    return {**base, **kw}


def test_the_same_fill_down_both_routes_buys_once(con):
    """The chain sees it first; the poll sees it three seconds later; one position results."""
    books = {"tok": book_of([(0.49, 1000)], [(0.51, 1000)])}
    eng = engine_with_chain(con, [activity_event()], books, [chain_event()])

    assert eng.drain_trades() == 1
    assert eng.poll_signals() == 0, "the poll must recognise what the chain already handled"

    orders = con.execute("SELECT COUNT(*) FROM orders WHERE run_id=?",
                         (eng.state.run_id,)).fetchone()[0]
    positions = con.execute("SELECT COUNT(*) FROM positions WHERE run_id=?",
                            (eng.state.run_id,)).fetchone()[0]
    assert orders == 1 and positions == 1


def test_the_guard_still_separates_two_partial_fills_in_one_transaction(con):
    """Dedupe must not be so eager that it collapses a genuinely split fill into one."""
    books = {"tok": book_of([(0.49, 5000)], [(0.51, 5000)])}
    eng = engine_with_chain(con, [], books, [
        chain_event(size=100.0, usdc_size=50.0),
        chain_event(size=40.0, usdc_size=20.0),
    ])
    assert eng.drain_trades() == 2
    assert con.execute("SELECT COUNT(*) FROM signals WHERE run_id=?",
                       (eng.state.run_id,)).fetchone()[0] == 2


def test_a_chain_signal_records_where_it_came_from(con):
    books = {"tok": book_of([(0.49, 1000)], [(0.51, 1000)])}
    eng = engine_with_chain(con, [], books, [chain_event()])
    eng.drain_trades()
    rows = store.latency_by_source(con, eng.state.run_id)
    assert [r[0] for r in rows] == ["chain"]
    assert rows[0][1] == 1


# --- failing means falling back, never blocking ---------------------------


def test_a_token_the_index_cannot_place_falls_through_to_the_poll(con):
    """The chain names a fill by token id alone. Not knowing its market is not an error.

    It must not block, must not fetch, and must not lose the fill -- the poll carries the
    condition id and picks the same trade up moments later.
    """
    books = {"tok": book_of([(0.49, 1000)], [(0.51, 1000)])}
    eng = engine_with_chain(con, [activity_event()], books,
                            [chain_event(condition_id="", token_id="unknown-token")])
    eng.drain_trades()
    reason = con.execute("SELECT reason FROM signals WHERE run_id=? AND action='skipped'",
                         (eng.state.run_id,)).fetchone()
    assert reason is not None and reason[0] == "token_unresolved"
    assert con.execute("SELECT COUNT(*) FROM orders WHERE run_id=?",
                       (eng.state.run_id,)).fetchone()[0] == 0
    assert eng.poll_signals() == 1, "the poll still has to be able to copy it"


def test_no_chain_feed_at_all_behaves_exactly_as_before(con):
    books = {"tok": book_of([(0.49, 1000)], [(0.51, 1000)])}
    eng = engine_with_chain(con, [activity_event()], books, [])
    eng.trades = None
    assert eng.drain_trades() == 0
    assert eng.poll_signals() == 1


def test_one_undecodable_fill_does_not_cost_the_rest_of_the_batch(con):
    """The poll breaks on error to protect its watermark. The drain has none to protect."""
    books = {"tok": book_of([(0.49, 5000)], [(0.51, 5000)])}
    broken = chain_event(tx_hash="0xbad")
    del broken["token_id"]                    # the shape the gates assume, missing
    eng = engine_with_chain(con, [], books, [broken, chain_event(tx_hash="0xgood")])
    assert eng.drain_trades() == 1
    assert con.execute(
        "SELECT COUNT(*) FROM orders WHERE run_id=?", (eng.state.run_id,)).fetchone()[0] == 1


def test_the_watermark_only_moves_forward(con):
    """A chain fill seen ahead of the poll must not drag the watermark back over handled history."""
    books = {"tok": book_of([(0.49, 5000)], [(0.51, 5000)])}
    eng = engine_with_chain(con, [], books, [chain_event(ts=500, tx_hash="0xold")])
    eng.state.last_seen["0xtrader"] = 9000
    eng.drain_trades()
    assert eng.state.last_seen["0xtrader"] == 9000


def test_a_reorged_out_log_is_never_enqueued():
    ts = TradeStreamProbe()
    ts._note_log({"removed": True, "transactionHash": "0x1", "logIndex": "0x0",
                  "topics": ["0x0"] * 4})
    assert ts.events.qsize() == 0


def test_the_two_endpoints_race_and_the_loser_is_dropped(captured):
    """Both nodes push the same log. The second copy must not become a second trade."""
    logs, ts, _ = captured
    probe = TradeStreamProbe()
    probe._note_block({"number": logs[0]["blockNumber"], "timestamp": hex(ts)})
    for _ in range(2):                        # the same log, from each endpoint
        probe._note_log(logs[0])
    assert probe.events.qsize() == 1
    assert probe.duplicates == 1


class TradeStreamProbe(tradestream.TradeStream):
    """The decode-and-enqueue half, without any sockets."""

    def __init__(self):
        super().__init__([TARGET], urls=(), log=lambda *a: None)


def test_an_undated_log_is_counted_rather_than_quietly_timestamped_now(captured):
    """Falling back to the clock overstates our speed, so it has to be visible."""
    logs, _ts, _ = captured
    probe = TradeStreamProbe()
    probe._note_log(logs[0])                  # no newHeads seen yet
    assert probe.undated == 1
    assert probe.events.qsize() == 1


def test_a_match_between_two_copied_wallets_is_two_events(captured):
    """Each side traded. Recording one of them would silently drop a signal."""
    logs, ts, _ = captured
    log = logs[0]
    both = ["0x" + log["topics"][2][-40:].lower(), "0x" + log["topics"][3][-40:].lower()]
    probe = TradeStreamProbe()
    probe.addresses = set(both)
    probe._note_block({"number": log["blockNumber"], "timestamp": hex(ts)})
    probe._note_log(log)
    assert probe.events.qsize() == 2
    sides = sorted(probe.events.get()["side"] for _ in range(2))
    assert sides == ["BUY", "SELL"]


def test_resolution_happens_on_the_engines_thread_not_the_sockets(con):
    """A sqlite3 connection belongs to the thread that opened it.

    An earlier cut handed `Engine.resolve_token` to the stream as its decoder callback, which
    ran it on the socket threads and turned every cold token into
    `ProgrammingError: SQLite objects created in a thread can only be used in that same thread`
    -- silently dropping those fills back to the poll. The wiring must leave resolution to
    `drain_trades`, so this asserts the stream is built without a resolver at all.
    """
    import inspect

    from polywatch.copytrade import commands
    src = inspect.getsource(commands._run)
    assert "tradestream.connect(t.traders, log=print)" in src
    assert "resolve=" not in src, "the stream must not be given a database-backed resolver"


def test_the_engine_resolves_a_cold_token_from_the_store(con):
    """The other half: what the socket thread cannot do, the engine thread must."""
    store.upsert_assets(con, [{"token_id": "tok", "condition_id": "cond",
                               "outcome": "Yes", "outcome_index": 0}])
    books = {"tok": book_of([(0.49, 1000)], [(0.51, 1000)])}
    eng = engine_with_chain(con, [], books, [chain_event(condition_id="")])
    eng._token_markets.clear()                # nothing pre-warmed; force the store lookup
    assert eng.drain_trades() == 1
    row = con.execute("SELECT condition_id, action FROM signals WHERE run_id=?",
                      (eng.state.run_id,)).fetchone()
    assert row[0] == "cond" and row[1] == "copied"
