"""Phase 8: the gates between a working paper bot and a live one.

None of these tests place an order or import py-clob-client. What they cover is the surface an
operator meets before the first live session: the preflight that says whether credentials are
present and coherent, the wallet-type setting that decides whether the exchange will accept a
signature at all, and the guard that stops `--yes` from trading unattended.
"""

import argparse

import pytest

from polywatch import account
from polywatch.config import ENV_FUNDER, ENV_PRIVATE_KEY, ENV_SIGNATURE_TYPE, ENV_UNATTENDED
from polywatch.copytrade import commands
from polywatch.copytrade.task import Task
from polywatch.db import store


@pytest.fixture
def clean_env(monkeypatch):
    for k in (ENV_PRIVATE_KEY, ENV_FUNDER, ENV_SIGNATURE_TYPE, ENV_UNATTENDED,
              "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _row(rows, name):
    return next(r for r in rows if r[0] == name)


# --- preflight ------------------------------------------------------------------------------

def test_preflight_reports_missing_credentials_rather_than_raising(clean_env):
    rows = account.live_preflight()
    assert _row(rows, ENV_PRIVATE_KEY)[1] is False
    assert _row(rows, ENV_FUNDER)[1] is False


def test_preflight_never_prints_the_private_key(clean_env):
    clean_env.setenv(ENV_PRIVATE_KEY, "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    clean_env.setenv(ENV_FUNDER, "0xfunder")
    rows = account.live_preflight()
    assert all("deadbeef" not in detail for _, _, detail in rows)
    assert _row(rows, ENV_PRIVATE_KEY)[2] == "set"


def test_preflight_stops_before_auth_when_the_key_is_missing(clean_env):
    clean_env.setenv(ENV_FUNDER, "0xfunder")
    rows = account.live_preflight()
    auth = [r for r in rows if r[0] == "CLOB auth"]
    # Either the extra is absent (so the walk ends there) or auth is explicitly skipped -- what
    # must never happen is an authentication attempt with no key to sign with.
    assert not auth or auth[0][1] is False


def test_preflight_names_the_wallet_type_it_would_sign_as(clean_env):
    clean_env.setenv(ENV_SIGNATURE_TYPE, "2")
    assert "browser-wallet proxy" in _row(account.live_preflight(), "signature type")[2]


def test_preflight_defaults_to_the_email_login_wallet(clean_env):
    assert _row(account.live_preflight(), "signature type")[2].startswith("1 ")


def test_an_unusable_signature_type_fails_the_preflight(clean_env):
    clean_env.setenv(ENV_SIGNATURE_TYPE, "7")
    assert _row(account.live_preflight(), "signature type")[1] is False


def test_format_preflight_says_ready_only_when_every_check_passed():
    assert "not ready" in account.format_preflight([("a", True, ""), ("b", False, "")])
    assert "not ready" not in account.format_preflight([("a", True, ""), ("b", True, "")])


# --- collateral: the checks that decide whether an order can settle -----------------------

def test_a_funded_and_approved_account_passes_both_collateral_checks():
    rows = account.collateral_rows(
        {"balance": "25000000", "allowances": {"0xa": "1000000", "0xb": "0"}})
    assert all(ok for _, ok, _ in rows)
    assert "$25.00" in _row(rows, "USDC balance")[2]
    assert "1/2" in _row(rows, "allowances")[2]


def test_usdc_is_read_with_six_decimals_not_eighteen():
    assert "$1.00" in _row(account.collateral_rows({"balance": "1000000"}), "USDC balance")[2]


def test_an_empty_account_fails_the_balance_check():
    assert _row(account.collateral_rows({"balance": "0"}), "USDC balance")[1] is False


def test_a_never_traded_account_fails_on_allowances_before_it_fails_on_an_order():
    rows = account.collateral_rows(
        {"balance": "10000000", "allowances": {"0xa": "0", "0xb": "0"}})
    assert _row(rows, "USDC balance")[1] is True
    assert _row(rows, "allowances")[1] is False
    assert "Polymarket UI" in _row(rows, "allowances")[2]


def test_a_response_missing_the_allowance_map_is_read_as_unapproved():
    assert _row(account.collateral_rows({"balance": "10000000"}), "allowances")[1] is False


# --- address resolution ---------------------------------------------------------------------

def test_the_funder_is_the_account_read_by_default(clean_env):
    clean_env.setenv(ENV_FUNDER, "0xFunder")
    assert account.resolve_address(None) == "0xFunder"


def test_an_explicit_address_wins_over_the_funder(clean_env):
    clean_env.setenv(ENV_FUNDER, "0xFunder")
    assert account.resolve_address("0xother") == "0xother"


def test_reading_no_account_at_all_is_an_error_not_an_empty_report(clean_env):
    with pytest.raises(RuntimeError):
        account.resolve_address(None)


# --- roster filters: the kind of trader, not the quality of one ----------------------------

def _seed(con, addr, archetype, hold_s, rank):
    store.upsert_trader_score(con, {"address": addr, "rank_score": rank, "n_closed": 50})
    store.upsert_trader_strategy(con, {"address": addr, "archetype": archetype,
                                       "hold_p50_s": hold_s, "confidence": 1.0,
                                       "evidence_n": 50})


@pytest.fixture
def scored(tmp_path):
    con = store.connect(tmp_path / "s.db")
    store.init_db(con)
    _seed(con, "0xfast", "scalper", 120, 0.9)
    _seed(con, "0xslow", "resolution-holder", 5 * 86400, 0.95)
    _seed(con, "0xmid", "momentum-chaser", 4 * 3600, 0.8)
    return con


def test_the_shortlist_is_unfiltered_by_default(scored):
    assert len(store.top_trader_scores(scored, limit=10)) == 3


def test_an_archetype_filter_keeps_only_that_kind(scored):
    rows = store.top_trader_scores(scored, limit=10, archetypes=["scalper", "momentum-chaser"])
    assert {r["address"] for r in rows} == {"0xfast", "0xmid"}


def test_the_highest_ranked_wallet_is_dropped_when_it_is_the_wrong_kind(scored):
    # 0xslow ranks highest. A roster built for flips must not take it anyway.
    rows = store.top_trader_scores(scored, limit=10, archetypes=["scalper"])
    assert [r["address"] for r in rows] == ["0xfast"]


def test_a_hold_ceiling_drops_wallets_too_slow_to_produce_flips(scored):
    rows = store.top_trader_scores(scored, limit=10, max_hold_s=3600)
    assert [r["address"] for r in rows] == ["0xfast"]


def test_the_two_filters_compose(scored):
    assert store.top_trader_scores(scored, limit=10, archetypes=["momentum-chaser"],
                                   max_hold_s=3600) == []


def test_an_unclassified_wallet_is_dropped_by_a_filter_rather_than_passed_through(scored):
    store.upsert_trader_score(scored, {"address": "0xunknown", "rank_score": 0.99,
                                       "n_closed": 50})
    assert "0xunknown" not in {r["address"]
                               for r in store.top_trader_scores(scored, limit=10,
                                                                archetypes=["scalper"])}
    assert "0xunknown" in {r["address"] for r in store.top_trader_scores(scored, limit=10)}


# --- deleting a task ------------------------------------------------------------------------

def test_a_task_with_runs_behind_it_is_not_deletable(tmp_path):
    con = store.connect(tmp_path / "d.db")
    store.init_db(con)
    store.upsert_task(con, Task(name="hasrun", trader="0xa").as_row())
    store.start_run(con, "hasrun", "paper", 100.0)
    with pytest.raises(ValueError):
        store.delete_task(con, "hasrun")


def test_deleting_a_task_takes_its_roster_with_it(tmp_path):
    con = store.connect(tmp_path / "d2.db")
    store.init_db(con)
    store.upsert_task(con, Task(name="fresh", trader="0xa").as_row())
    store.set_task_traders(con, "fresh", [{"address": "0xa"}])
    assert store.delete_task(con, "fresh") == 1
    assert con.execute("SELECT COUNT(*) FROM task_traders WHERE task='fresh'").fetchone()[0] == 0


# --- cost basis: what a closed trade can still say about itself ---------------------------

@pytest.fixture
def run(tmp_path):
    con = store.connect(tmp_path / "c.db")
    store.init_db(con)
    store.upsert_task(con, Task(name="t", trader="0xa").as_row())
    return con, store.start_run(con, "t", "paper", 100.0)


def _pos(con, run_id, shares=10.0, cost=8.0, fee=0.2):
    return store.open_position(con, {"run_id": run_id, "trader": "0xa", "token_id": "tok",
                                     "condition_id": "c", "shares": shares, "avg_price": cost / shares,
                                     "cost_usd": cost, "fees_paid": fee,
                                     "opened_ts": 1_700_000_000})


def _pos_row(con, pid):
    return con.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()


def test_a_new_position_records_what_it_cost(run):
    con, rid = run
    assert _pos_row(con, _pos(con, rid))["cost_basis"] == 8.0


def test_adding_to_a_position_adds_to_its_cost_basis(run):
    con, rid = run
    pid = _pos(con, rid)
    store.add_to_position(con, pid, shares=5.0, cost_usd=4.0, fee=0.1)
    r = _pos_row(con, pid)
    assert r["cost_basis"] == 12.0
    assert r["cost_usd"] == 12.0


def test_closing_a_position_clears_what_is_tied_up_but_not_what_it_cost(run):
    con, rid = run
    pid = _pos(con, rid)
    store.close_position(con, pid, proceeds_usd=9.0, exit_fee=0.2, reason="tp")
    r = _pos_row(con, pid)
    assert r["cost_usd"] == 0.0, "money is no longer tied up"
    assert r["cost_basis"] == 8.0, "but the trade still knows what it cost"


def test_a_closed_trade_can_be_scored_as_roi(run):
    con, rid = run
    pid = _pos(con, rid)
    store.close_position(con, pid, proceeds_usd=10.0, exit_fee=0.2, reason="tp")
    r = _pos_row(con, pid)
    # (10.0 - 8.0 - 0.2 - 0.2) / 8.0 -- computable only because cost_basis survived
    assert abs(r["realized_pnl"] / r["cost_basis"] - 0.2) < 1e-9


def test_a_partial_sale_leaves_the_whole_entry_cost_on_the_trade(run):
    con, rid = run
    pid = _pos(con, rid)
    store.settle_position(con, pid, shares_sold=5.0, proceeds_usd=4.5, exit_fee=0.1,
                          reason="partial")
    r = _pos_row(con, pid)
    assert r["open"] == 1
    assert r["cost_usd"] == 4.0, "half the money is still tied up"
    assert r["cost_basis"] == 8.0, "the trade still cost what it cost"


# --- the unattended guard -------------------------------------------------------------------

@pytest.fixture
def live_task(tmp_path):
    con = store.connect(tmp_path / "t.db")
    store.init_db(con)
    store.upsert_task(con, Task(name="live1", trader="0xa", mode="live").as_row())
    return con


def _run_args(**kw):
    base = dict(name="live1", mode=None, session_hours=None, yes=True, stream=False,
                no_stream=False, no_session_log=True, note=None, operator=None, rps=1.0)
    base.update(kw)
    return argparse.Namespace(**base)


def test_yes_alone_does_not_trade_live_with_no_terminal_attached(live_task, clean_env,
                                                                 monkeypatch, capsys):
    monkeypatch.setattr(commands.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(commands.execution, "make",
                        lambda *_a, **_k: pytest.fail("an executor was built anyway"))
    assert commands._run(live_task, _run_args()) == 1
    assert ENV_UNATTENDED in capsys.readouterr().out


def test_the_unattended_opt_in_is_what_allows_it(live_task, clean_env, monkeypatch):
    monkeypatch.setattr(commands.sys.stdin, "isatty", lambda: False, raising=False)
    clean_env.setenv(ENV_UNATTENDED, "1")
    built = []
    monkeypatch.setattr(commands.execution, "make",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no live extra"))
                        if built.append(1) is None else None)
    # It gets past the guard and fails at the executor instead, which is the next real gate.
    assert commands._run(live_task, _run_args()) == 1
    assert built, "the guard blocked a run that had opted in"


def test_a_person_at_a_terminal_is_not_asked_for_the_opt_in(live_task, clean_env, monkeypatch):
    monkeypatch.setattr(commands.sys.stdin, "isatty", lambda: True, raising=False)
    built = []
    monkeypatch.setattr(commands.execution, "make",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no live extra"))
                        if built.append(1) is None else None)
    assert commands._run(live_task, _run_args()) == 1
    assert built, "an interactive --yes was blocked"


def test_paper_mode_is_never_subject_to_the_live_guard(live_task, clean_env, monkeypatch):
    monkeypatch.setattr(commands.sys.stdin, "isatty", lambda: False, raising=False)
    built = []
    monkeypatch.setattr(commands.execution, "make",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here"))
                        if built.append(1) is None else None)
    commands._run(live_task, _run_args(mode="paper"))
    assert built, "paper mode was stopped by a live-mode gate"
