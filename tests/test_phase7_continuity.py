"""Phase 7: the handoff between one operator of the account and the next.

The invariant under test is easy to break by accident: a session must hand over *information*
and never *state*. `Engine.start` closes an abandoned run rather than adopting it, seeds the
watermark to now, and discards the trailing high-water marks -- all deliberately. A handoff that
quietly restored any of that would make the next report a fiction, so the last tests here assert
that starting a run after a session inherits nothing.
"""

import json

import pytest

from polywatch.copytrade import session
from polywatch.copytrade.task import Task
from polywatch.db import store


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init_db(c)
    store.upsert_task(c, Task(name="alpha", trader="0xa").as_row())
    return c


def a_run(con, *, positions=(), skips=(), stop_reason="session_end", resting=(),
          end_bankroll=100.0):
    run_id = store.start_run(con, "alpha", "paper", 100.0)
    for i, p in enumerate(positions):
        pid = store.open_position(con, {"run_id": run_id, "trader": p.get("trader", "0xa"),
                                        "token_id": f"tok{i}", "condition_id": f"c{i}",
                                        "shares": 10.0, "avg_price": 0.5, "cost_usd": 5.0,
                                        "fees_paid": 0.1, "opened_ts": 0})
        if p.get("close") is not None:
            store.settle_position(con, pid, 10.0, 5.0 + p["close"], 0.1,
                                  p.get("reason", "follow_exit"))
    for i, reason in enumerate(skips):
        store.insert_signal(con, {"run_id": run_id, "trader": "0xa", "kind": "TRADE",
                                  "tx_hash": f"h{i}", "token_id": f"tok{i}",
                                  "condition_id": "c", "side": "BUY", "size": 1.0,
                                  "price": 0.5, "usdc_size": 5.0, "trader_ts": 0,
                                  "action": "skipped", "reason": reason})
    for r in resting:
        store.insert_resting(con, {"run_id": run_id, "position_id": None, "mode": r,
                                   "token_id": "tok0", "condition_id": "c0", "side": "SELL",
                                   "shares": 10.0, "price": 0.9, "kind": "take_profit",
                                   "status": "open", "placed_ts": 0})
    store.finish_run(con, run_id, end_bankroll, stop_reason)
    return run_id


# --- state ------------------------------------------------------------------


def test_state_reads_the_database_not_an_engine(con):
    """It has to work identically after a clean stop, after a crash, and hours later from a
    different process."""
    rid = a_run(con, positions=[{"close": 3.0}, {"close": None}], skips=["stale", "stale"])
    s = session.account_state(con, "alpha")
    assert s["run_id"] == rid
    assert len(s["open_positions"]) == 1
    assert s["skips"][0] == ("stale", 2)
    assert s["summary"]["positions_closed"] == 1


def test_a_task_that_has_never_run_produces_an_empty_but_valid_state(con):
    s = session.account_state(con, "alpha")
    assert s["run_id"] is None and s["open_positions"] == [] and s["n_proposals"] == 0


# --- warnings and next steps (pure) -----------------------------------------


def test_open_positions_produce_the_unwatched_stop_loss_warning():
    """The exact warning Engine.stop prints, restated where the next operator will read it: the
    CLOB has no stop order type, so nothing watches a stop while nothing is running."""
    w = session.warnings({"open_positions": [{}, {}]})
    assert len(w) == 1 and "NO STOP-LOSS IS RUNNING" in w[0]
    steps = session.next_steps({"open_positions": [{}, {}]})
    assert "open positions first" in steps[0]


def test_live_resting_orders_are_flagged_and_paper_ones_are_not():
    assert session.warnings({"resting": [{"mode": "live"}]})
    assert not session.warnings({"resting": [{"mode": "paper"}]})


def test_orphans_from_earlier_runs_are_reported_with_the_command_that_clears_them():
    w = session.warnings({"run_id": 7, "orphans": [{"run_id": 3}]})
    assert w and "task orders --cancel" in w[0]


def test_an_orphan_from_this_same_run_is_not_double_reported():
    assert session.warnings({"run_id": 3, "orphans": [{"run_id": 3}]}) == []


def test_a_circuit_breaker_stop_is_called_out_as_different_from_the_clock():
    w = session.warnings({"stop_reason": "circuit_breaker"})
    assert w and "circuit breaker" in w[0]


def test_a_reconcile_failure_says_not_to_start_another_run_yet():
    w = session.warnings({"stop_reason": "reconcile_failed"})
    assert w and "L5" in w[0]


def test_an_auto_dropped_trader_is_named_and_said_to_persist():
    steps = session.next_steps({"dropped": [{"address": "0xdeadbeefcafe",
                                             "dropped_reason": "pnl -8.10"}]})
    assert any("0xdeadbeefca" in s and "persists" in s for s in steps)


def test_the_dominant_skip_reason_is_mapped_to_what_it_falsifies():
    steps = session.next_steps({"skips": [("fee_floor", 40), ("stale", 2)]})
    assert any("fee_floor" in s and "verdict on the markets" in s for s in steps)


def test_max_hold_dominating_the_exits_is_reported_as_a_pace_mismatch():
    steps = session.next_steps({"exits": [("max_hold", 9, -3.0)]})
    assert any("slower than" in s for s in steps)


def test_unapplied_proposals_are_a_pointer_and_explicitly_not_a_queue():
    steps = session.next_steps({"n_proposals": 3})
    assert any("not a queue" in s for s in steps)


def test_a_clean_session_still_produces_an_instruction():
    steps = session.next_steps({})
    assert len(steps) == 1 and "Nothing outstanding" in steps[0]


# --- briefing ---------------------------------------------------------------


def test_the_briefing_leads_with_what_is_unsafe(con):
    a_run(con, positions=[{"close": None}])
    h = session.close(con, "alpha", write_doc=False)
    text = session.briefing(h)
    assert text.index("Attention") < text.index("Next steps")
    assert "NO STOP-LOSS IS RUNNING" in text


def test_the_briefing_names_the_run_and_its_money(con):
    a_run(con, positions=[{"close": 4.0}], end_bankroll=104.0)
    h = session.close(con, "alpha", write_doc=False)
    text = session.briefing(h)
    assert "task `alpha`" in text and "$100.00 → $104.00" in text


def test_a_task_with_no_run_briefs_that_nothing_is_held(con):
    h = session.close(con, "alpha", write_doc=False)
    assert "Nothing was traded and nothing is held" in session.briefing(h)


def test_a_note_reaches_the_briefing(con):
    a_run(con)
    h = session.close(con, "alpha", note="left running over lunch", write_doc=False)
    assert "left running over lunch" in session.briefing(h)


# --- persistence ------------------------------------------------------------


def test_close_stores_the_briefing_in_the_database(con):
    """RULES.md I1: a finished run must be explicable from the database alone, so a handoff that
    exists only as a file on disk is not one."""
    rid = a_run(con, skips=["stale"])
    session.close(con, "alpha", write_doc=False)
    row = store.last_session(con, "alpha")
    assert row["run_id"] == rid
    assert "Next steps" in row["briefing_md"]
    assert json.loads(row["next_steps_json"])


def test_close_records_the_top_skip_and_exit_reasons(con):
    a_run(con, positions=[{"close": 1.0, "reason": "stop_loss"}], skips=["fee_floor", "stale",
                                                                        "fee_floor"])
    session.close(con, "alpha", write_doc=False)
    row = store.last_session(con, "alpha")
    assert row["top_skip_reason"] == "fee_floor" and row["top_exit_reason"] == "stop_loss"


def test_close_counts_resting_orders_left_on_the_book(con):
    a_run(con, resting=["live"])
    session.close(con, "alpha", write_doc=False)
    assert store.last_session(con, "alpha")["resting_open"] == 1


def test_progress_entries_are_written_newest_first(tmp_path, con):
    """The reader is an operator with a budget -- a person skimming or an agent with a context
    window. Chronological order buries the only entry that matters."""
    doc = tmp_path / "PROGRESS.md"
    a_run(con)
    session.close(con, "alpha", note="ENTRY-ONE", path=doc)
    a_run(con)
    session.close(con, "alpha", note="ENTRY-TWO", path=doc)
    text = doc.read_text()
    assert text.startswith("# PROGRESS")
    assert text.index("ENTRY-TWO") < text.index("ENTRY-ONE")
    assert text.count("## ") == 2


def test_the_progress_preamble_is_written_once(tmp_path, con):
    doc = tmp_path / "PROGRESS.md"
    a_run(con)
    session.close(con, "alpha", path=doc)
    session.close(con, "alpha", path=doc)
    assert doc.read_text().count("# PROGRESS") == 1


# --- reading back -----------------------------------------------------------


def test_session_open_says_so_when_there_is_no_history(con):
    out = session.format_open(con, "alpha")
    assert "No previous session recorded" in out


def test_session_open_prints_the_last_briefing_and_denies_being_a_resume(con):
    a_run(con, skips=["stale"])
    session.close(con, "alpha", note="watch 0xabc", write_doc=False)
    out = session.format_open(con, "alpha")
    assert "watch 0xabc" in out
    assert "not a resume" in out


def test_the_log_lists_sessions_newest_first(con):
    a_run(con, end_bankroll=90.0)
    session.close(con, "alpha", write_doc=False)
    a_run(con, end_bankroll=110.0)
    session.close(con, "alpha", write_doc=False)
    rows = store.sessions(con, "alpha")
    assert [r["end_equity"] for r in rows] == [110.0, 90.0]
    assert "110.00" in session.format_log(rows)


# --- the invariant ----------------------------------------------------------


def test_a_handoff_does_not_resurrect_a_run(con):
    """`session close` reads; it must never reopen a finished run or reattach its positions."""
    rid = a_run(con, positions=[{"close": None}])
    session.close(con, "alpha", write_doc=False)
    run = con.execute("SELECT * FROM task_runs WHERE id=?", (rid,)).fetchone()
    assert run["stopped_at"] is not None
    assert store.running_runs(con, "alpha") == []


def test_the_next_run_inherits_no_positions_from_the_briefing(con):
    """The engine's refusal to inherit is the thing the handoff is designed around: it hands over
    information so that nothing has to hand over state."""
    a_run(con, positions=[{"close": None}])
    session.close(con, "alpha", write_doc=False)
    new_run = store.start_run(con, "alpha", "paper", 100.0)
    assert store.open_positions(con, new_run) == []


def test_close_is_idempotent_enough_to_run_twice_after_a_crash(con):
    """`session close` exists as its own command precisely because the run that ended badly is
    the one least likely to have reached the end of `task run`."""
    a_run(con)
    session.close(con, "alpha", write_doc=False)
    session.close(con, "alpha", write_doc=False)
    assert len(store.sessions(con, "alpha")) == 2       # a log, not a state machine


# --- CLI wiring -------------------------------------------------------------


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_a_run_closes_its_own_session_by_default(con, capsys):
    from polywatch.copytrade import commands
    rid = a_run(con, positions=[{"close": None}])
    commands._close_session(con, "alpha", rid, Args())
    row = store.last_session(con, "alpha")
    assert row is not None and row["run_id"] == rid
    assert "next steps" in capsys.readouterr().out


def test_no_session_log_opts_out(con):
    from polywatch.copytrade import commands
    rid = a_run(con)
    commands._close_session(con, "alpha", rid, Args(no_session_log=True))
    assert store.last_session(con, "alpha") is None


def test_a_failed_handoff_never_fails_the_run(con, capsys, monkeypatch):
    """The trading is already done and already recorded; this is the narrative on top of it."""
    from polywatch.copytrade import commands, session as session_mod
    rid = a_run(con)
    monkeypatch.setattr(session_mod, "close",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))
    commands._close_session(con, "alpha", rid, Args())          # must not raise
    assert "could not write the session handoff" in capsys.readouterr().out
