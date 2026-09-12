"""Phase 1: the defects that cost money rather than accuracy.

Every test here fails against the code as it stood before this phase. They are grouped by the
thing that was wrong, and each one states the loss it prevents rather than the branch it covers.
"""

from __future__ import annotations

import pytest

from polywatch.copytrade import book as bk
from polywatch.copytrade import reconcile
from polywatch.copytrade.engine import Engine, ReconcileError
from polywatch.copytrade.execution import Executor, Fill, LiveExecutor, PaperExecutor
from polywatch.copytrade.task import Task
from polywatch.db import store


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init_db(c)
    return c


def a_run(con, bankroll=100.0):
    store.upsert_task(con, Task(name="t", trader="0xt", bankroll=bankroll).as_row())
    return store.start_run(con, "t", "paper", bankroll)


def a_position(con, run_id, shares=100.0, price=0.50, fee=2.0):
    return store.open_position(con, {
        "run_id": run_id, "token_id": "tok", "condition_id": "cond", "shares": shares,
        "avg_price": price, "cost_usd": shares * price, "fees_paid": fee, "opened_ts": 1000})


# --- partial-exit accounting ----------------------------------------------
# The remainder of a partly-sold position must cost what the remainder cost. Leaving the whole
# entry fee on it inflates the cost basis, which fires the stop-loss against a loss that is
# partly fictional and reports less cash than the run has.


def test_a_partial_exit_leaves_the_remainder_carrying_only_its_own_fees(con):
    run = a_run(con)
    pid = a_position(con, run, shares=100, price=0.50, fee=2.0)

    store.settle_position(con, pid, 40.0, proceeds_usd=24.0, exit_fee=0.8, reason="stop_loss")

    pos = con.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
    assert pos["open"] == 1
    assert pos["shares"] == pytest.approx(60)
    assert pos["cost_usd"] == pytest.approx(30.0)          # 60 shares at 0.50
    assert pos["fees_paid"] == pytest.approx(1.2)          # 60% of the $2 entry fee
    # ...and the fee per remaining share is exactly what it was before the partial.
    assert pos["fees_paid"] / pos["shares"] == pytest.approx(2.0 / 100)


def test_a_partial_exit_banks_its_pnl_instead_of_losing_it(con):
    run = a_run(con)
    pid = a_position(con, run, shares=100, price=0.50, fee=2.0)

    pnl, closed = store.settle_position(con, pid, 40.0, proceeds_usd=24.0, exit_fee=0.8,
                                        reason="stop_loss")
    assert not closed
    # 40 shares cost $20 and carried $0.80 of entry fee; sold for $24 less $0.80 exit fee.
    assert pnl == pytest.approx(24.0 - 20.0 - 0.8 - 0.8)
    assert con.execute("SELECT realized_pnl FROM positions WHERE id=?",
                       (pid,)).fetchone()[0] == pytest.approx(pnl)


def test_cash_after_a_partial_exit_counts_the_money_that_came_back(con):
    """Cash is bankroll minus what is tied up plus what has been realized. A partial exit
    changes both halves, and the old code changed only one."""
    run = a_run(con, bankroll=100.0)
    pid = a_position(con, run, shares=100, price=0.50, fee=2.0)
    assert store.run_cash(con, run) == pytest.approx(100 - 52.0)

    pnl, _ = store.settle_position(con, pid, 50.0, proceeds_usd=30.0, exit_fee=1.0,
                                   reason="take_profit")
    # $26 of basis freed (25 cost + 1 fee) and the realized PnL banked on top.
    assert store.run_cash(con, run) == pytest.approx(100 - 26.0 + pnl)


def test_fees_reported_for_a_run_survive_a_partial_exit(con):
    """Splitting the fee across held and sold shares must not lose any of it."""
    run = a_run(con)
    pid = a_position(con, run, shares=100, price=0.50, fee=2.0)
    store.settle_position(con, pid, 40.0, proceeds_usd=24.0, exit_fee=0.8, reason="stop_loss")
    assert store.run_summary(con, run)["fees_paid"] == pytest.approx(2.8)

    store.settle_position(con, pid, 60.0, proceeds_usd=36.0, exit_fee=1.2, reason="stop_loss")
    assert store.run_summary(con, run)["fees_paid"] == pytest.approx(4.0)


def test_closing_a_position_outright_still_nets_every_fee(con):
    run = a_run(con)
    pid = a_position(con, run, shares=100, price=0.50, fee=2.0)
    pnl = store.close_position(con, pid, proceeds_usd=60.0, exit_fee=2.0, reason="take_profit")
    assert pnl == pytest.approx(60.0 - 50.0 - 2.0 - 2.0)
    assert store.run_summary(con, run)["realized_pnl"] == pytest.approx(pnl)


def test_the_two_halves_of_a_partial_exit_sum_to_one_whole_exit(con):
    """Selling in two pieces at one price must earn what selling once would have."""
    run_a, run_b = a_run(con), store.start_run(con, "t", "paper", 100.0)
    one = a_position(con, run_a, shares=100, price=0.50, fee=2.0)
    two = a_position(con, run_b, shares=100, price=0.50, fee=2.0)

    whole = store.close_position(con, one, proceeds_usd=60.0, exit_fee=2.0, reason="tp")
    first, _ = store.settle_position(con, two, 50.0, 30.0, 1.0, "tp")
    second, closed = store.settle_position(con, two, 50.0, 30.0, 1.0, "tp")
    assert closed
    assert first + second == pytest.approx(whole)


# --- holding both sides of one market -------------------------------------


def test_buying_the_other_outcome_of_a_market_we_are_in_is_refused(con):
    """YES and NO of the same market is two entry fees and two exit fees for no exposure."""
    run = a_run(con)
    a_position(con, run, shares=100, price=0.50)
    other = store.open_position_other_outcome(con, run, "cond", "tok_no")
    assert other is not None
    assert store.open_position_other_outcome(con, run, "cond", "tok") is None


# --- an unreadable live response is not a rejection -----------------------


def test_a_response_that_says_nothing_matched_is_a_rejection():
    """`sizeMatched: 0` is the exchange stating a fact. It is safe to believe."""
    assert LiveExecutor._filled({"sizeMatched": 0, "makingAmount": 0}) == (0.0, 0.0)


def test_a_response_we_cannot_read_is_unknown_rather_than_zero():
    """Zeros here would book a real fill as a rejection and leave shares with no row."""
    assert LiveExecutor._filled({"status": "live", "orderID": "0xabc"}) is None
    assert LiveExecutor._filled({"sizeMatched": "not-a-number"}) is None
    assert LiveExecutor._filled(None) is None


def test_a_readable_response_reports_what_matched():
    assert LiveExecutor._filled({"size_matched": "12", "amount": "6"}) == (12.0, 6.0)


def test_a_price_the_order_could_not_have_got_is_unknown_rather_than_booked():
    """`makingAmount` is USDC on a buy and shares on a sell, so the same pairing that reads a
    buy correctly yields exactly 1.0000 on a sell -- a dollar a share, and a profit the run
    never made. The band between the touch and our own limit is what catches it."""
    imp = LiveExecutor._implausible
    assert imp(1.0, side="SELL", limit=0.50, touch=0.52)          # the misread
    assert not imp(0.52, side="SELL", limit=0.50, touch=0.52)     # sold at the touch
    assert not imp(0.51, side="SELL", limit=0.50, touch=0.52)     # walked one level down
    assert imp(0.40, side="SELL", limit=0.50, touch=0.52)         # below our own limit
    assert imp(0.0, side="BUY", limit=0.55, touch=0.51)
    assert imp(0.90, side="BUY", limit=0.55, touch=0.51)          # worse than we signed for
    assert not imp(0.53, side="BUY", limit=0.55, touch=0.51)
    # A book that cannot say where the touch was still bounds the fill by the limit.
    assert not imp(0.51, side="SELL", limit=0.50, touch=None)


def test_an_unknown_fill_is_not_ok_and_says_so():
    f = Fill("unknown", reason="the response did not say what matched")
    assert not f.ok and f.unknown


# --- reconciliation -------------------------------------------------------


class FakeLive(Executor):
    """A live executor whose exchange we control. Only reconciliation touches it."""

    mode = "live"

    def __init__(self, positions=None, orders=None):
        self._positions = positions
        self._orders = orders or {}

    def account_positions(self):
        return self._positions

    def order_status(self, exchange_id):
        return self._orders.get(exchange_id)


def an_unknown_order(con, run_id, token="tok", side="BUY", exchange_id="0xoid"):
    return store.insert_order(con, {
        "run_id": run_id, "signal_id": None, "mode": "live", "token_id": token,
        "condition_id": "cond", "side": side, "intent_usd": 10.0, "limit_price": 0.52,
        "book_vwap": 0.51, "filled_shares": 0.0, "avg_price": None, "fee": 0.0,
        "status": "unknown", "reason": "response did not say", "ts": 1000,
        "exchange_id": exchange_id} | {})


def test_paper_mode_has_no_exchange_to_disagree_with(con):
    run = a_run(con)
    a_position(con, run)
    assert reconcile.check(con, run, PaperExecutor()) == ([], [])


def test_an_exchange_that_cannot_be_asked_stops_the_run(con):
    """Silence is not agreement. An unanswerable account is a reason to stop, not to proceed."""
    run = a_run(con)
    _, problems = reconcile.check(con, run, FakeLive(positions=None))
    assert [p.kind for p in problems] == ["unreadable"]


def test_shares_the_exchange_does_not_show_stop_the_run(con):
    """These shares back a stop-loss that would sell something we do not have."""
    run = a_run(con)
    a_position(con, run, shares=100)
    _, problems = reconcile.check(con, run, FakeLive(positions={"tok": 40.0}))
    assert [p.kind for p in problems] == ["shortfall"]
    assert problems[0].local == 100 and problems[0].remote == 40


def test_an_account_that_matches_the_database_reconciles_clean(con):
    run = a_run(con)
    a_position(con, run, shares=100)
    resolutions, problems = reconcile.check(con, run, FakeLive(positions={"tok": 100.0}))
    assert resolutions == [] and problems == []


def test_an_unknown_order_the_exchange_can_account_for_is_adopted(con):
    run = a_run(con)
    an_unknown_order(con, run)
    ex = FakeLive(positions={"tok": 20.0},
                  orders={"0xoid": {"size_matched": "20", "price": "0.52"}})
    resolutions, problems = reconcile.check(con, run, ex)
    assert problems == []
    assert len(resolutions) == 1
    assert resolutions[0].shares == pytest.approx(20) and resolutions[0].source == "order_status"


def test_an_unknown_order_is_settled_by_the_account_when_the_exchange_will_not_talk(con):
    """One unresolved order and one unexplained surplus is an answer, not a guess."""
    run = a_run(con)
    an_unknown_order(con, run)
    resolutions, problems = reconcile.check(con, run, FakeLive(positions={"tok": 19.0}))
    assert problems == []
    assert resolutions[0].source == "account_diff"
    assert resolutions[0].shares == pytest.approx(19.0)


def test_a_surplus_that_belongs_to_an_earlier_run_is_not_adopted(con):
    """The exchange answers for the account, not for one run. Yesterday's still-open position
    is not this order's fill, and crediting it books the same shares to two runs."""
    store.upsert_task(con, Task(name="t", trader="0xt", bankroll=100.0).as_row())
    old_run = store.start_run(con, "t", "live", 100.0)
    a_position(con, old_run, shares=19.0)
    run = store.start_run(con, "t", "live", 100.0)
    an_unknown_order(con, run)

    # The account shows exactly the 19 shares the earlier run already holds, and nothing more.
    resolutions, problems = reconcile.check(con, run, FakeLive(positions={"tok": 19.0}))
    assert [r.source for r in resolutions] == ["nothing_matched"]
    assert resolutions[0].shares == 0.0
    assert [p.kind for p in problems] == []


def test_two_unknown_orders_and_one_surplus_is_a_guess_and_is_refused(con):
    run = a_run(con)
    an_unknown_order(con, run, exchange_id="0xa")
    an_unknown_order(con, run, exchange_id="0xb")
    _, problems = reconcile.check(con, run, FakeLive(positions={"tok": 19.0}))
    assert [p.kind for p in problems] == ["unresolved_order", "unresolved_order"]


def test_an_unknown_order_that_changed_nothing_is_closed_as_a_rejection(con):
    run = a_run(con)
    an_unknown_order(con, run)
    resolutions, problems = reconcile.check(con, run, FakeLive(positions={}))
    assert problems == []
    assert resolutions[0].shares == 0 and resolutions[0].source == "nothing_matched"


def test_adopted_shares_are_not_then_reported_as_a_shortfall(con):
    """The order is about to become a position, so it is not missing."""
    run = a_run(con)
    a_position(con, run, shares=50)
    an_unknown_order(con, run)
    ex = FakeLive(positions={"tok": 50.0},
                  orders={"0xoid": {"size_matched": "20", "price": "0.52"}})
    resolutions, problems = reconcile.check(con, run, ex)
    assert problems == []
    assert resolutions[0].shares == pytest.approx(20)
