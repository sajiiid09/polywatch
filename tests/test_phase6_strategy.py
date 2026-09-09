"""Phase 6: what kind of trader a wallet is, and what the record of that kind suggests.

Two things are being defended here. The classifier must refuse to label rather than guess -- an
archetype is the grouping key of every learned finding, so a coin-flip label corrupts everything
downstream. And the proposal rules must not reach confident conclusions the data does not carry:
the `stale`-versus-feed-latency test below is the one this module exists to prevent.
"""

import json

import pytest

from polywatch.copytrade import learn, strategy
from polywatch.db import store


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init_db(c)
    return c


def trades(n=15, price=0.5, exit_price=0.6, hold=600, spacing=3600, side_both=True):
    out = []
    for i in range(n):
        t = i * spacing
        out.append({"side": "BUY", "token_id": f"tok{i}", "condition_id": f"c{i}",
                    "size": 10.0, "price": price, "ts": t})
        if side_both:
            out.append({"side": "SELL", "token_id": f"tok{i}", "condition_id": f"c{i}",
                        "size": 10.0, "price": exit_price, "ts": t + hold})
    return out


# --- features ---------------------------------------------------------------


def test_pre_drift_is_positive_when_they_buy_after_a_rise():
    rows = trades(n=12)
    prices = {(f"tok{i}", i * 3600 - strategy.PRE_DRIFT_S): 0.40 for i in range(12)}
    drift = strategy.pre_drift(rows, lambda tok, ts: prices.get((tok, ts)))
    assert drift == pytest.approx(0.10)


def test_pre_drift_is_negative_when_they_buy_into_weakness():
    rows = trades(n=12)
    prices = {(f"tok{i}", i * 3600 - strategy.PRE_DRIFT_S): 0.62 for i in range(12)}
    assert strategy.pre_drift(rows, lambda tok, ts: prices.get((tok, ts))) < 0


def test_pre_drift_is_none_without_a_price_lookup():
    assert strategy.pre_drift(trades(), None) is None


def test_a_settled_quote_is_treated_as_absent_not_as_cheap():
    """A price of exactly 0 or 1 in the prices table means resolved, not a bargain."""
    rows = trades(n=12)
    assert strategy.pre_drift(rows, lambda tok, ts: 1.0) is None


def test_resolution_frac_is_one_when_nothing_is_ever_sold():
    f = strategy.features(trades(n=20, side_both=False))
    assert f["resolution_frac"] == 1.0
    assert f["n_round_trips"] == 0


def test_resolution_frac_is_zero_when_everything_is_flipped():
    f = strategy.features(trades(n=20))
    assert f["resolution_frac"] == 0.0


def test_scale_in_frac_counts_tokens_bought_more_than_once():
    rows = [{"side": "BUY", "token_id": "a", "condition_id": "c", "size": 5, "price": 0.5,
             "ts": 0},
            {"side": "BUY", "token_id": "a", "condition_id": "c", "size": 5, "price": 0.5,
             "ts": 10},
            {"side": "SELL", "token_id": "a", "condition_id": "c", "size": 10, "price": 0.6,
             "ts": 20},
            {"side": "BUY", "token_id": "b", "condition_id": "c", "size": 5, "price": 0.5,
             "ts": 30}]
    assert strategy.features(rows)["scale_in_frac"] == 0.5


def test_cut_ratio_is_above_one_for_big_winners_and_small_losers():
    rows = []
    for i in range(10):                      # ten small losses
        rows += [{"side": "BUY", "token_id": f"l{i}", "condition_id": "c", "size": 1,
                  "price": 0.20, "ts": i * 100},
                 {"side": "SELL", "token_id": f"l{i}", "condition_id": "c", "size": 1,
                  "price": 0.18, "ts": i * 100 + 50}]
    for i in range(2):                       # two large wins
        rows += [{"side": "BUY", "token_id": f"w{i}", "condition_id": "c", "size": 1,
                  "price": 0.20, "ts": 5000 + i * 100},
                 {"side": "SELL", "token_id": f"w{i}", "condition_id": "c", "size": 1,
                  "price": 0.60, "ts": 5000 + i * 100 + 50}]
    from polywatch.copytrade import skill
    assert strategy.cut_ratio(skill.match_round_trips(rows)) > 3


def test_cut_ratio_is_none_when_there_are_no_losers_to_compare_against():
    from polywatch.copytrade import skill
    assert strategy.cut_ratio(skill.match_round_trips(trades(n=10))) is None


def test_a_missing_feature_is_none_and_never_a_flattering_default():
    f = strategy.features([])
    assert f["entry_p50"] is None and f["cut_ratio"] is None and f["cat_hhi"] is None


# --- classification ---------------------------------------------------------


def test_each_archetype_classifies_to_itself():
    cases = {
        "scalper": {"n_trades": 200, "hold_p50_s": 40, "hold_p10_s": 15},
        "market-maker": {"n_trades": 500, "burst_frac": 0.8, "median_gap_s": 10,
                         "trades_per_day": 400},
        "momentum-chaser": {"n_trades": 100, "pre_drift_p50": 0.08, "hold_p50_s": 600,
                            "sell_frac": 0.5},
        "fade-the-move": {"n_trades": 100, "pre_drift_p50": -0.08, "hold_p50_s": 600,
                          "sell_frac": 0.5},
        "longshot-hunter": {"n_trades": 100, "entry_p50": 0.10, "cut_ratio": 4.0},
        "favourite-grinder": {"n_trades": 100, "entry_p50": 0.88, "trip_win_rate": 0.9,
                              "cut_ratio": 0.3},
        "resolution-holder": {"n_trades": 100, "resolution_frac": 0.9, "sell_frac": 0.05},
        "event-specialist": {"n_trades": 100, "cat_hhi": 0.95},
    }
    for expected, feat in cases.items():
        got, conf, _ = strategy.classify(feat)
        assert got == expected, f"{expected} classified as {got}"
        assert conf > 0


def test_an_ambiguous_wallet_is_unclassified_rather_than_a_coin_flip():
    """Two archetypes within a hair of each other is not a finding, and saying so is the
    behaviour that keeps every downstream grouping honest."""
    feat = {"n_trades": 100, "entry_p50": 0.26, "cut_ratio": 1.0, "trip_win_rate": 0.5}
    got, conf, scores = strategy.classify(feat)
    top = sorted(scores.values(), reverse=True)
    assert top[0] - top[1] < strategy.ARCHETYPE_MARGIN_SCALE
    assert got == strategy.UNCLASSIFIED


def test_a_wallet_below_the_trade_floor_is_never_labelled():
    got, conf, _ = strategy.classify({"n_trades": 4, "entry_p50": 0.05, "cut_ratio": 9.0})
    assert got == strategy.UNCLASSIFIED and conf == 0.0


def test_an_unmeasurable_archetype_scores_neutral_rather_than_inheriting():
    """momentum-chaser and fade-the-move are mirror images sharing two of three components.
    Without pre-entry drift neither claim can be made, so neither may be scored off the shared
    terms -- otherwise they tie at the top and bury whatever the wallet actually is."""
    feat = {"n_trades": 100, "hold_p50_s": 600, "sell_frac": 0.5, "entry_p50": 0.10,
            "cut_ratio": 4.0}
    scores = strategy._scores(feat)
    assert scores["momentum-chaser"] == 0.5 and scores["fade-the-move"] == 0.5
    assert strategy.classify(feat)[0] == "longshot-hunter"


def test_classification_is_deterministic():
    rows = trades(n=20, price=0.15, exit_price=0.4)
    a = strategy.profile("0xA", rows)
    b = strategy.profile("0xA", rows)
    assert (a.archetype, a.confidence, a.scores) == (b.archetype, b.confidence, b.scores)


def test_event_specialist_yields_to_an_archetype_that_describes_the_how():
    """Category concentration says where a wallet trades, not how. It should only win when
    nothing describes the how."""
    feat = {"n_trades": 100, "cat_hhi": 0.95, "resolution_frac": 0.95, "sell_frac": 0.02}
    assert strategy.classify(feat)[0] == "resolution-holder"


def test_profile_round_trips_through_the_database(con):
    p = strategy.profile("0xABC", trades(n=25, price=0.12, exit_price=0.45))
    store.upsert_trader_strategy(con, p.as_row())
    row = store.get_trader_strategy(con, "0xabc")
    assert row["archetype"] == p.archetype
    assert json.loads(row["features_json"])["n_trades"] == 50
    # A rescan overwrites cleanly rather than accumulating rows.
    store.upsert_trader_strategy(con, p.as_row())
    assert len(store.strategies(con)) == 1


# --- aggregation ------------------------------------------------------------


def _run_with(con, positions, signals=(), task="t"):
    from polywatch.copytrade.task import Task
    store.upsert_task(con, Task(name=task, trader="0xa").as_row())
    run_id = store.start_run(con, task, "paper", 100.0)
    for p in positions:
        pid = store.open_position(con, {"run_id": run_id, "trader": p["trader"],
                                        "token_id": p["token"], "condition_id": "c",
                                        "shares": 10.0, "avg_price": 0.5, "cost_usd": 5.0,
                                        "fees_paid": 0.1, "opened_ts": 0})
        if p.get("close") is not None:
            store.settle_position(con, pid, 10.0, 5.0 + p["close"], 0.1, p.get("reason", "x"))
    for s in signals:
        store.insert_signal(con, {"run_id": run_id, "trader": s["trader"], "kind": "TRADE",
                                  "tx_hash": s["tx"], "token_id": s["tx"],
                                  "condition_id": "c", "side": "BUY",
                                  "size": 1.0, "price": 0.5, "usdc_size": 5.0,
                                  "trader_ts": 0, "action": s["action"],
                                  "reason": s.get("reason")})
    return run_id


def test_archetype_pnl_groups_positions_by_the_archetype_of_the_trader_copied(con):
    store.upsert_trader_strategy(con, {"address": "0xa", "archetype": "longshot-hunter",
                                       "confidence": 0.8, "evidence_n": 30})
    store.upsert_trader_strategy(con, {"address": "0xb", "archetype": "favourite-grinder",
                                       "confidence": 0.8, "evidence_n": 30})
    _run_with(con, [{"trader": "0xa", "token": "t1", "close": -2.0},
                    {"trader": "0xa", "token": "t2", "close": -1.0},
                    {"trader": "0xb", "token": "t3", "close": 3.0}])
    got = store.archetype_pnl(con)
    assert got["longshot-hunter"]["closed"] == 2
    assert got["longshot-hunter"]["realized_pnl"] < 0
    assert got["favourite-grinder"]["win_rate"] == 1.0


def test_an_unprofiled_trader_groups_under_unknown_rather_than_vanishing(con):
    """Silently dropping an unattributed position would flatter whichever archetypes happen to
    have been profiled."""
    _run_with(con, [{"trader": "0xzz", "token": "t1", "close": 4.0}])
    assert store.archetype_pnl(con)["unknown"]["closed"] == 1


def test_archetype_signals_splits_the_skip_histogram(con):
    store.upsert_trader_strategy(con, {"address": "0xa", "archetype": "scalper",
                                       "confidence": 0.9, "evidence_n": 50})
    _run_with(con, [], [{"trader": "0xa", "tx": "1", "action": "skipped", "reason": "stale"},
                        {"trader": "0xa", "tx": "2", "action": "skipped", "reason": "stale"},
                        {"trader": "0xa", "tx": "3", "action": "copied"}])
    got = store.archetype_signals(con)["scalper"]
    assert got["seen"] == 3 and got["copied"] == 1 and got["skips"] == [("stale", 2)]


def test_snapshots_append_rather_than_overwrite(con):
    """The question a snapshot answers is a trend; an upsert would erase the only evidence of
    drift."""
    store.insert_strategy_snapshot(con, [{"taken_at": 100, "archetype": "scalper",
                                          "realized_pnl": 1.0}])
    store.insert_strategy_snapshot(con, [{"taken_at": 200, "archetype": "scalper",
                                          "realized_pnl": -3.0}])
    rows = store.strategy_snapshots(con, archetype="scalper")
    assert [r["realized_pnl"] for r in rows] == [1.0, -3.0]


# --- proposals --------------------------------------------------------------


def findings(n_runs=1, **kw) -> learn.Findings:
    f = learn.Findings(taken_at=0, n_runs=n_runs)
    f.archetypes = kw.pop("archetypes", {})
    f.skips = kw.pop("skips", [])
    f.exits = kw.pop("exits", [])
    f.latency = kw.pop("latency", {})
    f.totals = kw.pop("totals", {})
    return f


def arch(**kw) -> dict:
    base = {"n_traders": 0, "median_rank_score": None, "median_capture_ratio": None,
            "n_positions": 0, "closed": 0, "open": 0, "realized_pnl": 0.0, "fees": 0.0,
            "win_rate": None, "seen": 0, "copied": 0, "skipped": 0, "skips": [], "exits": []}
    base.update(kw)
    return base


def test_fee_floor_dominating_proposes_a_tighter_ceiling():
    f = findings(skips=[("fee_floor", 60), ("stale", 10)],
                 archetypes={"favourite-grinder": arch(skips=[("fee_floor", 55)])})
    props = learn.fee_floor_rule(f)
    assert len(props) == 1
    assert props[0].knob == "max_fee_frac" and props[0].scope == "task"
    assert "favourite-grinder" in props[0].evidence


def test_a_reason_below_the_dominance_threshold_proposes_nothing():
    f = findings(skips=[("fee_floor", 5), ("stale", 95)])
    assert learn.fee_floor_rule(f) == []


def test_stale_proposes_a_faster_poll_only_when_the_delay_is_ours():
    f = findings(skips=[("stale", 80), ("fee_floor", 5)],
                 latency={"n": 80, "feed": 1.0, "loop": 9.0})
    props = learn.stale_rule(f)
    assert len(props) == 1 and props[0].knob == "poll_interval_s"


def test_a_feed_bottleneck_proposes_nothing_and_explains_why():
    """The wrong conclusion this whole module exists to avoid. `signals` carries fetch_ts
    precisely so the delay can be split; a shorter poll cannot recover time already spent
    before the response arrived."""
    f = findings(skips=[("stale", 80), ("fee_floor", 5)],
                 latency={"n": 80, "feed": 40.0, "loop": 1.0})
    assert learn.stale_rule(f) == []
    obs = learn.observations(f)
    assert any("feed" in o and "cannot recover" in o for o in obs)


def test_a_losing_archetype_is_proposed_for_the_roster_not_for_a_risk_limit():
    f = findings(archetypes={"scalper": arch(closed=40, realized_pnl=-30.0, fees=6.0)})
    props = learn.archetype_pnl_rule(f)
    assert len(props) == 1 and props[0].scope == "roster"
    assert "40 closed scalper positions" in props[0].evidence


def test_a_losing_archetype_under_the_position_floor_proposes_nothing():
    f = findings(archetypes={"scalper": arch(closed=4, realized_pnl=-30.0)})
    assert learn.archetype_pnl_rule(f) == []
    assert any("floor" in o for o in learn.observations(f))


def test_an_uncopyable_archetype_is_named_even_when_its_wallets_are_good():
    f = findings(archetypes={"market-maker": arch(n_traders=8, median_capture_ratio=0.05)})
    props = learn.capture_rule(f)
    assert len(props) == 1 and "does not survive being copied" in props[0].evidence


def test_max_hold_dominating_an_archetypes_exits_proposes_a_longer_time_stop():
    f = findings(archetypes={"resolution-holder": arch(
        exits=[("max_hold", 18, -4.0), ("stop_loss", 2, -1.0)])})
    props = learn.max_hold_rule(f)
    assert len(props) == 1 and "max_hold_s" in props[0].knob


def test_every_proposal_carries_its_sample_size_and_a_confidence():
    f = findings(skips=[("fee_floor", 60)],
                 archetypes={"scalper": arch(closed=60, realized_pnl=-20.0)})
    props = learn.proposals(f)
    assert props and all(p.n > 0 and p.confidence in ("low", "medium", "high") for p in props)


def test_proposals_are_ordered_by_how_much_evidence_stands_behind_them():
    f = findings(skips=[("fee_floor", 200)],
                 archetypes={"scalper": arch(closed=16, realized_pnl=-5.0)})
    props = learn.proposals(f)
    assert props[0].confidence == "high"


def test_no_runs_yet_is_reported_rather_than_producing_findings():
    f = findings(n_runs=0)
    assert learn.proposals(f) == []
    assert any("No runs recorded" in o for o in learn.observations(f))


# --- rendering --------------------------------------------------------------


def test_the_generated_document_marks_every_proposal_unapplied():
    f = findings(skips=[("fee_floor", 60)])
    doc = learn.render(f, learn.proposals(f), learn.observations(f))
    assert "UNAPPLIED" in doc
    assert "generated" in doc.lower()
    assert "n=60" in doc


def test_the_document_states_its_own_scope_and_sample():
    f = findings(skips=[("stale", 9)])
    f.n_runs = 3
    doc = learn.render(f, [], [], task="alpha")
    assert "task `alpha`" in doc and "3 run(s)" in doc


def test_learn_writes_a_snapshot_and_a_document_but_touches_no_task(con, tmp_path):
    store.upsert_trader_strategy(con, {"address": "0xa", "archetype": "scalper",
                                       "confidence": 0.9, "evidence_n": 40})
    _run_with(con, [{"trader": "0xa", "token": "t1", "close": -2.0}])
    before = dict(store.get_task(con, "t"))
    doc = tmp_path / "LEARNED.md"
    f, props, obs = learn.learn(con, write=True, path=doc)
    assert doc.exists() and "UNAPPLIED" in doc.read_text()
    assert store.strategy_snapshots(con)
    assert dict(store.get_task(con, "t")) == before        # RULES.md I8


def test_show_writes_nothing(con, tmp_path):
    doc = tmp_path / "LEARNED.md"
    learn.learn(con, write=False, path=doc)
    assert not doc.exists()
    assert not store.strategy_snapshots(con)


def test_a_database_from_the_previous_schema_still_opens_and_gains_the_new_columns(tmp_path):
    """There are gigabytes of existing data and it must keep opening. New tables arrive free via
    CREATE TABLE IF NOT EXISTS; new *columns* on an existing table do not, which is what the
    PRAGMA table_info block in init_db is for."""
    import sqlite3
    import subprocess

    # The newest revision of schema.sql that predates these tables, rather than HEAD: once the
    # change is committed HEAD *is* the new schema, and a fixture built from it silently stops
    # testing the migration while continuing to pass.
    revs = subprocess.run(["git", "rev-list", "HEAD", "--", "src/polywatch/db/schema.sql"],
                          capture_output=True, text=True, check=True).stdout.split()
    old = None
    for rev in revs:
        text = subprocess.run(["git", "show", f"{rev}:src/polywatch/db/schema.sql"],
                              capture_output=True, text=True, check=True).stdout
        if "trader_strategy" not in text:
            old = text
            break
    assert old is not None, "no revision of schema.sql predates trader_strategy"

    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(old)
    legacy.commit()
    legacy.close()

    con = store.connect(path)
    store.init_db(con)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(trader_scores)")}
    assert {"archetype", "strategy_confidence"} <= cols
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"trader_strategy", "strategy_snapshots", "sessions"} <= tables
    # And the pre-existing rows are untouched by the migration.
    store.upsert_trader_strategy(con, {"address": "0xa", "archetype": "scalper",
                                       "confidence": 0.5, "evidence_n": 30})
    assert store.get_trader_strategy(con, "0xa")["archetype"] == "scalper"
