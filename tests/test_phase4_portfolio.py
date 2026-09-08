"""Phase 4: one bankroll, several traders, and the ability to tell them apart afterwards.

Copying one wallet bets the whole account on one person's judgement, and the ordinary way a copy
bot ends is that the person tilts. Copying several is only diversification if the bad one can be
identified and stopped, which is what most of this file is about.
"""

from __future__ import annotations

import pytest

from polywatch.copytrade import report
from polywatch.copytrade.engine import Engine
from polywatch.copytrade.execution import PaperExecutor
from polywatch.copytrade.task import Task, preset
from polywatch.db import store

from test_copytrade_engine import FakeAPI, activity_event


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init_db(c)
    return c


class MultiAPI(FakeAPI):
    """An activity feed per wallet, so the traders can be given different histories."""

    def __init__(self, feeds, books):
        super().__init__([], books)
        self.feeds = feeds

    def activity(self, client, user, **kw):
        self.calls.append(("activity", user))
        return [] if kw.get("offset") else self.feeds.get(user, [])


def engine_for(con, feeds, books, task, now=1010):
    import polywatch.copytrade.engine as engine_mod
    fake = MultiAPI(feeds, books)
    engine_mod.api = fake
    store.upsert_task(con, task.as_row())
    eng = Engine(con, task, PaperExecutor(), client=None, log=lambda *a: None, now=lambda: now)
    eng._trader_account = lambda _a: 10_000.0
    eng.start()
    return eng, fake


BOOKS = {"tok_a": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]},
         "tok_b": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}


def a_task(**kw):
    base = dict(name="t", trader="0xa", traders=["0xa", "0xb"], bankroll=100.0,
                **preset("quick_flips"))
    return Task(**{**base, **kw})


# --- the roster -----------------------------------------------------------


def test_a_task_with_one_trader_is_unchanged_by_any_of_this():
    """Every saved task predates the roster. None of them may behave differently."""
    t = Task(name="t", trader="0xA")
    assert t.traders == ["0xa"] and t.trader == "0xa"
    assert t.per_trader_cap == t.bankroll, "one trader may use the whole bankroll, as before"


def test_a_roster_is_deduplicated_rather_than_rejected():
    """The same wallet topping two leaderboards is how it comes to be named twice."""
    assert Task(name="t", trader="0xA", traders=["0xa", "0xB", "0xb"]).traders == ["0xa", "0xb"]


def test_the_first_trader_is_the_one_the_denormalised_column_keeps_naming():
    t = Task(name="t", trader="0xa", traders=["0xa", "0xb"])
    assert t.as_row()["trader"] == "0xa"


def test_a_task_must_copy_somebody():
    with pytest.raises(ValueError):
        Task(name="t", trader="")


def test_each_trader_gets_an_equal_share_of_the_bankroll_by_default():
    assert a_task(bankroll=90.0).per_trader_cap == pytest.approx(45.0)
    assert a_task(bankroll=90.0, per_trader_usd=10.0).per_trader_cap == pytest.approx(10.0)


def test_an_old_saved_task_still_loads(con):
    """config_json written before `traders` existed must round-trip into a one-wallet roster."""
    import json
    row = Task(name="t", trader="0xa").as_row()
    cfg = json.loads(row["config_json"])
    del cfg["traders"]
    row["config_json"] = json.dumps(cfg)
    store.upsert_task(con, row)
    assert Task.from_row(store.get_task(con, "t")).traders == ["0xa"]


# --- polling several feeds ------------------------------------------------


def test_every_trader_on_the_roster_is_polled(con):
    eng, fake = engine_for(con, {}, BOOKS, a_task())
    eng.poll_signals()
    assert {u for kind, u in fake.calls if kind == "activity"} == {"0xa", "0xb"}


def test_a_quiet_traders_watermark_is_not_dragged_forward_by_a_busy_one(con):
    eng, _ = engine_for(con, {"0xa": [activity_event(proxyWallet="0xa", asset="tok_a",
                                                     timestamp=1008)]}, BOOKS, a_task())
    eng.state.last_seen = {"0xa": 900, "0xb": 900}
    eng.poll_signals()
    assert eng.state.last_seen["0xa"] == 1008
    assert eng.state.last_seen["0xb"] == 900


def test_a_signal_is_attributed_to_the_trader_whose_feed_it_came_from(con):
    feeds = {"0xa": [activity_event(proxyWallet="0xa", asset="tok_a", transactionHash="0x1")],
             "0xb": [activity_event(proxyWallet="0xb", asset="tok_b", conditionId="cond_b", transactionHash="0x2")]}
    eng, _ = engine_for(con, feeds, BOOKS, a_task())
    eng.poll_signals()
    got = {r["token_id"]: r["trader"] for r in store.signals(con, eng.state.run_id)}
    assert got == {"tok_a": "0xa", "tok_b": "0xb"}


def test_a_position_records_whose_signal_opened_it(con):
    feeds = {"0xb": [activity_event(proxyWallet="0xb", asset="tok_b", conditionId="cond_b", transactionHash="0x2")]}
    eng, _ = engine_for(con, feeds, BOOKS, a_task())
    eng.poll_signals()
    assert store.open_positions(con, eng.state.run_id)[0]["trader"] == "0xb"


# --- the per-trader cap ---------------------------------------------------


def test_one_trader_cannot_take_the_whole_bankroll(con):
    """Without this, copying several wallets is a race to spend the account first."""
    # A $10 stake fills the whole cap, so the second signal meets it head-on rather than
    # being squeezed under the minimum order size, which is a different (and also correct) skip.
    t = a_task(bankroll=100.0, per_trader_usd=10.0, fixed_usd=10.0, max_market_usd=100.0)
    feeds = {"0xa": [activity_event(proxyWallet="0xa", asset="tok_a", transactionHash="0x1"),
                     activity_event(proxyWallet="0xa", asset="tok_b", conditionId="cond_b",
                                    transactionHash="0x2", timestamp=1005)]}
    eng, _ = engine_for(con, feeds, BOOKS, t)
    eng.poll_signals()
    assert store.trader_exposure(con, eng.state.run_id, "0xa") <= 11.0
    assert "max_trader_usd" in dict(store.skip_reasons(con, eng.state.run_id))


def test_the_cap_is_per_trader_so_another_wallet_can_still_trade(con):
    t = a_task(bankroll=100.0, per_trader_usd=12.0, fixed_usd=10.0, max_market_usd=100.0)
    feeds = {"0xa": [activity_event(proxyWallet="0xa", asset="tok_a", transactionHash="0x1")],
             "0xb": [activity_event(proxyWallet="0xb", asset="tok_b", conditionId="cond_b", transactionHash="0x2")]}
    eng, _ = engine_for(con, feeds, BOOKS, t)
    eng.poll_signals()
    owners = {p["trader"] for p in store.open_positions(con, eng.state.run_id)}
    assert owners == {"0xa", "0xb"}


# --- whose exit is it -----------------------------------------------------


def test_only_the_trader_who_opened_a_position_gets_to_close_it(con):
    """Following B out of a trade A put us into attributes A's loss to B's judgement, and
    exits on the opinion of someone who never took the trade."""
    feeds = {"0xa": [activity_event(proxyWallet="0xa", asset="tok_a", transactionHash="0x1")]}
    eng, fake = engine_for(con, feeds, BOOKS, a_task())
    eng.poll_signals()
    assert store.open_positions(con, eng.state.run_id)

    fake.feeds = {"0xb": [activity_event(proxyWallet="0xb", asset="tok_a", side="SELL",
                                         transactionHash="0x9", timestamp=1005)]}
    eng.poll_signals()
    assert store.open_positions(con, eng.state.run_id), "B does not get to sell A's position"
    assert "position_owned_by_other" in dict(store.skip_reasons(con, eng.state.run_id))


def test_the_owner_selling_does_close_it(con):
    feeds = {"0xa": [activity_event(proxyWallet="0xa", asset="tok_a", transactionHash="0x1")]}
    eng, fake = engine_for(con, feeds, BOOKS, a_task())
    eng.poll_signals()

    fake.feeds = {"0xa": [activity_event(proxyWallet="0xa", asset="tok_a", side="SELL",
                                         transactionHash="0x9", timestamp=1005)]}
    eng.poll_signals()
    assert not store.open_positions(con, eng.state.run_id)


# --- dropping a trader ----------------------------------------------------


def test_a_trader_who_loses_too_much_stops_being_copied(con):
    t = a_task(auto_drop_usd=5.0)
    eng, _ = engine_for(con, {}, BOOKS, t)
    run = eng.state.run_id
    pid = store.open_position(con, {"run_id": run, "trader": "0xa", "token_id": "tok_a",
                                    "condition_id": "cond", "shares": 100, "avg_price": 0.5,
                                    "cost_usd": 50.0, "fees_paid": 0.0, "opened_ts": 1})
    store.close_position(con, pid, proceeds_usd=42.0, exit_fee=0.0, reason="stop_loss")

    assert eng.check_trader_drops() == ["0xa"]
    assert "0xa" in eng.state.dropped
    roster = {r["address"]: r["active"] for r in store.task_traders(con, "t")}
    assert roster == {"0xa": 0, "0xb": 1}


def test_a_dropped_traders_signals_are_recorded_and_refused(con):
    t = a_task(auto_drop_usd=5.0)
    eng, _ = engine_for(con, {}, BOOKS, t)
    from polywatch.parse import records
    eng.state.dropped["0xa"] = "down too much"
    ev = records.parse_activity([activity_event(proxyWallet="0xa", asset="tok_a")])[0]
    eng.handle_event(ev, 1010, trader="0xa")
    assert dict(store.skip_reasons(con, eng.state.run_id)) == {"trader_dropped": 1}


def test_dropping_a_trader_does_not_abandon_their_open_positions(con):
    """It stops taking their advice, not their trades. The exit ladder still runs."""
    t = a_task(auto_drop_usd=5.0)
    feeds = {"0xa": [activity_event(proxyWallet="0xa", asset="tok_a", transactionHash="0x1")]}
    eng, _ = engine_for(con, feeds, BOOKS, t)
    eng.poll_signals()
    eng.state.dropped["0xa"] = "down too much"
    assert store.open_positions(con, eng.state.run_id)
    eng.manage_positions()      # still managed, not orphaned
    assert store.open_positions(con, eng.state.run_id)


def test_a_run_without_auto_drop_never_drops_anybody(con):
    eng, _ = engine_for(con, {}, BOOKS, a_task(auto_drop_usd=0.0))
    run = eng.state.run_id
    pid = store.open_position(con, {"run_id": run, "trader": "0xa", "token_id": "tok_a",
                                    "condition_id": "cond", "shares": 100, "avg_price": 0.5,
                                    "cost_usd": 50.0, "fees_paid": 0.0, "opened_ts": 1})
    store.close_position(con, pid, proceeds_usd=1.0, exit_fee=0.0, reason="stop_loss")
    assert eng.check_trader_drops() == []


def test_unrealized_loss_alone_does_not_drop_a_trader(con):
    """That is what the stop-loss is for. Dropping on an open position fires on noise."""
    t = a_task(auto_drop_usd=5.0)
    eng, _ = engine_for(con, {}, BOOKS, t)
    store.open_position(con, {"run_id": eng.state.run_id, "trader": "0xa", "token_id": "tok_a",
                              "condition_id": "cond", "shares": 100, "avg_price": 0.5,
                              "cost_usd": 50.0, "fees_paid": 0.0, "opened_ts": 1})
    assert eng.check_trader_drops() == []


# --- attribution ----------------------------------------------------------


def test_the_report_separates_what_each_trader_earned(con):
    """Without this a portfolio run prints one number and the bad wallet hides inside it."""
    feeds = {"0xa": [activity_event(proxyWallet="0xa", asset="tok_a", transactionHash="0x1")],
             "0xb": [activity_event(proxyWallet="0xb", asset="tok_b", conditionId="cond_b", transactionHash="0x2")]}
    eng, _ = engine_for(con, feeds, BOOKS, a_task())
    eng.poll_signals()

    by = store.trader_pnl(con, eng.state.run_id)
    assert set(by) == {"0xa", "0xb"}
    assert by["0xa"]["copied"] == 1 and by["0xb"]["copied"] == 1
    assert "by trader" in report.run_report(con, eng.state.run_id, eng.task)


def test_a_single_trader_run_does_not_grow_an_attribution_table(con):
    """One wallet does not need a breakdown, and printing one is noise."""
    feeds = {"0xa": [activity_event(proxyWallet="0xa", asset="tok_a", transactionHash="0x1")]}
    t = Task(name="t", trader="0xa", **preset("quick_flips"))
    eng, _ = engine_for(con, feeds, BOOKS, t)
    eng.poll_signals()
    assert "by trader" not in report.run_report(con, eng.state.run_id, t)
