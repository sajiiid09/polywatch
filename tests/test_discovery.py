"""Phase 2b: discovery, ranking and the walk-forward split.

The two look-ahead tests here are the ones that matter. Walk-forward validation is worthless if
the ranking can see the window it is about to be judged on, and the leak would be invisible in
the output -- it would simply make every recommendation look excellent. Everything else in this
file is ordinary coverage; those two are the reason the file exists.
"""

from __future__ import annotations

import pytest

from polywatch.copytrade import discover, rank, replay, select, skill
from polywatch.copytrade.task import Task
from polywatch.db import store

CUTOFF = 1_700_100_000
DAY = 86_400


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init_db(c)
    return c


def _closed(end_ts, pnl, avg_price=0.5, cur_price=1.0, cost=10.0):
    return {"wallet": "0xa", "token_id": "tok", "condition_id": "cond", "shares": cost / avg_price,
            "avg_price": avg_price, "cost": cost, "cur_price": cur_price,
            "realized_pnl": pnl, "outcome": "Yes", "outcome_index": 0, "end_ts": end_ts,
            "title": None, "slug": None}


# --- look-ahead: the ranking must not see past the cutoff -----------------


class _FakeClient:
    """Serves one wallet's closed positions without a network. `discover.fetch_closed` pages
    until it gets a short page, so one short page ends it."""

    def __init__(self, payload):
        self.payload = payload

    def get_json(self, url, params=None, **kw):
        if "closed-positions" in url:
            return [] if params.get("offset") else self.payload
        if "/value" in url:
            return [{"user": "0xa", "value": 500.0}]
        return []


def _raw(end_iso, pnl):
    return {"proxyWallet": "0xa", "asset": "tok", "conditionId": "cond", "totalBought": 20.0,
            "avgPrice": 0.5, "curPrice": 1.0, "realizedPnl": pnl, "outcome": "Yes",
            "outcomeIndex": 0, "endDate": end_iso, "title": "t", "slug": "s"}


def test_the_ranking_never_sees_a_position_that_settled_after_the_cutoff(con):
    """A wallet whose entire profit lands after the cutoff must be scored as though that profit
    does not exist -- because at ranking time it did not. Without this the walk-forward split is
    decorative: the ranking would already know which wallets were about to win."""
    client = _FakeClient([
        _raw("2026-01-10T00:00:00Z", -5.0),    # before cutoff: a loss
        _raw("2026-06-10T00:00:00Z", 500.0),   # after cutoff: the big win
    ])
    cutoff = int(__import__("datetime").datetime(
        2026, 3, 1, tzinfo=__import__("datetime").timezone.utc).timestamp())
    sc, _ = discover.score_trader(con, client, "0xa", until_ts=cutoff)
    assert sc.n_closed == 1, "only the pre-cutoff position may be scored"
    assert sc.realized_pnl == -5.0, "the post-cutoff win must be invisible"


def test_scoring_without_a_cutoff_still_sees_everything(con):
    client = _FakeClient([_raw("2026-01-10T00:00:00Z", -5.0), _raw("2026-06-10T00:00:00Z", 500.0)])
    sc, _ = discover.score_trader(con, client, "0xa")
    assert sc.n_closed == 2


def test_a_position_with_no_settlement_date_is_dropped_when_a_cutoff_applies(con):
    """An undated position cannot be proved to sit on the near side of the cutoff, so counting
    it would be a guess in the direction that flatters the ranking."""
    client = _FakeClient([_raw(None, 100.0)])
    sc, _ = discover.score_trader(con, client, "0xa", until_ts=CUTOFF)
    assert sc.n_closed == 0


# --- look-ahead: the validation must not see before the cutoff ------------


def _seed(con, wallet="0xa", condition="cond", token="tok"):
    store.upsert_markets(con, [{
        "condition_id": condition, "gamma_id": "1", "question": "q", "slug": "s",
        "category": None, "closed": 1, "active": 0, "archived": 0, "start_ts": 1,
        "end_ts": CUTOFF + 20 * DAY, "outcomes_json": "[]", "prices_json": "[]",
        "uma_status_json": "[]", "resolved": 1, "winning_index": 0, "neg_risk": 0,
        "fees_enabled": 1, "fee_type": "sports_fees_v3", "fee_rate": 0.0, "fee_exponent": 1.0,
        "fee_taker_only": 1, "fee_rebate_rate": None, "fee_source": "market",
        "tick_size": 0.001, "order_min_size": 5.0, "accepting_orders": 0,
        "enable_order_book": 1}])
    store.upsert_assets(con, [{"token_id": token, "condition_id": condition,
                               "outcome_index": 0, "outcome": "Yes"}])
    store.upsert_wallets(con, [{"address": wallet, "source": "leaderboard", "rank": 1,
                                "username": "u", "vol": 1.0, "pnl": 1.0}])
    # Half the history before the cutoff, half after.
    rows = [{"wallet": wallet, "token_id": token, "condition_id": condition, "side": "BUY",
             "size": 20.0, "price": 0.5, "ts": CUTOFF - (5 - i) * DAY, "outcome": "Yes",
             "outcome_index": 0, "tx_hash": f"0xb{i}"} for i in range(5)]
    rows += [{"wallet": wallet, "token_id": token, "condition_id": condition, "side": "BUY",
              "size": 20.0, "price": 0.5, "ts": CUTOFF + (i + 1) * DAY, "outcome": "Yes",
              "outcome_index": 0, "tx_hash": f"0xa{i}"} for i in range(5)]
    store.insert_trades(con, rows)


def test_the_validation_replay_touches_no_trade_from_before_the_cutoff(con):
    """The other half of the split. If the replay reaches back past the cutoff it is scoring
    the wallet on the very history that selected it."""
    _seed(con)
    task = Task(name="wf", trader="0xa", fixed_usd=2.0, max_market_usd=4.0)
    summary = replay.run(con, task, 0.0, since_ts=CUTOFF + 1)
    early = con.execute("SELECT COUNT(*) FROM signals WHERE run_id=? AND trader_ts<=?",
                        (summary["run_id"], CUTOFF)).fetchone()[0]
    assert early == 0
    assert summary["signals_seen"] == 5, "only the five post-cutoff trades"


def test_the_two_windows_do_not_overlap():
    rank_start, cutoff = select.windows(now=1_800_000_000)
    assert rank_start < cutoff < 1_800_000_000
    assert (1_800_000_000 - cutoff) == 7 * DAY
    assert (cutoff - rank_start) == 53 * DAY, "60 days of history minus the 7 held back"


# --- ranking --------------------------------------------------------------


def _score(address, brier=0.15, consistency=0.8, roi=0.3, dd=0.1, n=100):
    return skill.SkillScore(address=address, n_closed=n, win_rate=0.6, roi=roi,
                            realized_pnl=100.0, invested=300.0, brier=brier, n_brier=n,
                            avg_entry_price=0.5, avg_stake_usd=10.0, median_stake_usd=10.0,
                            max_drawdown=dd, consistency=consistency, last_close_ts=CUTOFF)


def test_a_wallet_better_on_every_component_ranks_first():
    worse = _score("0xworse", brier=0.22, consistency=0.4, roi=0.05, dd=0.4, n=40)
    better = _score("0xbetter", brier=0.10, consistency=0.9, roi=0.5, dd=0.05, n=200)
    assert [r.address for r in rank.rank([worse, better])] == ["0xbetter", "0xworse"]


def test_a_coinflip_brier_scores_zero_calibration_rather_than_merely_little():
    """0.25 is what you get forecasting 0.5 on everything. At or above it the entry prices
    carry no information, whatever the PnL says."""
    assert rank.calibration(0.25) == 0.0
    assert rank.calibration(0.371) == 0.0
    assert rank.calibration(0.125) == pytest.approx(0.5)


def test_a_wallet_with_nothing_to_calibrate_scores_zero_not_average():
    assert rank.calibration(None) == 0.0


def test_normalising_a_cohort_of_one_does_not_divide_by_zero():
    ranked = rank.rank([_score("0xonly")])
    assert len(ranked) == 1 and 0.0 <= ranked[0].rank_score <= 1.0


def test_an_identical_cohort_scores_everyone_the_same_without_full_marks():
    """A component that separates nobody must not hand everybody 1.0 and dominate the sum."""
    assert rank.normalise([3.0, 3.0, 3.0]) == [0.5, 0.5, 0.5]


def test_the_weights_are_a_partition_of_one():
    from polywatch.config import RANK_WEIGHTS
    assert sum(RANK_WEIGHTS.values()) == pytest.approx(1.0)


# --- disqualifiers --------------------------------------------------------


def test_a_coinflip_forecaster_is_flagged_however_well_it_ranked():
    """The failure mode that motivated this: the first wallet ever scored posted +44% ROI on a
    Brier of 0.371. A weighted sum averages that away; a flag does not."""
    r = rank.rank([_score("0xa", brier=0.371, roi=0.44)])[0]
    assert any("50/50" in d for d in rank.disqualifiers(r))


def test_a_clean_record_raises_no_flags():
    r = rank.rank([_score("0xa", brier=0.12, consistency=0.8, roi=0.3, dd=0.1)])[0]
    assert rank.disqualifiers(r) == []


@pytest.mark.parametrize("kw,fragment", [
    ({"roi": -0.2}, "roi"),
    ({"dd": 0.7}, "drawdown"),
    ({"consistency": 0.2}, "months"),
])
def test_each_disqualifier_names_itself(kw, fragment):
    r = rank.rank([_score("0xa", **kw)])[0]
    assert any(fragment in d for d in rank.disqualifiers(r))


# --- sweep ----------------------------------------------------------------


class _SweepClient:
    """Returns the same wallet under every category, plus one unique to each."""

    def __init__(self):
        self.calls = 0

    def get_json(self, url, params=None, **kw):
        self.calls += 1
        if params.get("offset"):
            return []
        cat = params["category"]
        return [{"proxyWallet": "0xshared", "name": "s", "amount": 10.0, "rank": 1},
                {"proxyWallet": f"0x{cat.lower()}", "name": cat, "amount": 5.0, "rank": 2}]


def test_the_sweep_dedupes_a_wallet_that_appears_in_many_categories(con):
    client = _SweepClient()
    found = discover.sweep_leaderboard(con, client, pages=1,
                                       categories=("CRYPTO", "SPORTS"), periods=("DAY",),
                                       orderings=("PNL",))
    assert sorted(found) == ["0xcrypto", "0xshared", "0xsports"]
    assert store.wallets(con) and len(store.wallets(con)) == 3


def test_the_sweep_covers_every_combination(con):
    client = _SweepClient()
    discover.sweep_leaderboard(con, client, pages=1, categories=("CRYPTO", "SPORTS"),
                               periods=("DAY", "WEEK"), orderings=("PNL", "VOL"))
    assert client.calls == 2 * 2 * 2


def test_one_dead_combination_does_not_abort_the_sweep(con):
    from polywatch.fetch.client import FetchError

    class _Flaky(_SweepClient):
        def get_json(self, url, params=None, **kw):
            if params["category"] == "SPORTS":
                raise FetchError("500")
            return super().get_json(url, params, **kw)

    found = discover.sweep_leaderboard(con, _Flaky(), pages=1,
                                       categories=("CRYPTO", "SPORTS"), periods=("DAY",),
                                       orderings=("PNL",))
    assert "0xcrypto" in found


# --- scoping the expensive stage ------------------------------------------


def test_market_ingest_can_be_narrowed_to_one_wallet(con):
    """Walk-forward validates one candidate at a time. Unscoped, the market fetch covers every
    screened wallet -- on a real 3,107-wallet sweep that was 122,277 unfetched markets, about
    twelve thousand requests, to backtest a wallet that traded a few hundred."""
    store.upsert_wallets(con, [
        {"address": "0xa", "source": "leaderboard", "rank": 1, "username": "a",
         "vol": 1.0, "pnl": 1.0},
        {"address": "0xb", "source": "leaderboard", "rank": 2, "username": "b",
         "vol": 1.0, "pnl": 1.0}])
    store.insert_trades(con, [
        {"wallet": "0xa", "token_id": "t1", "condition_id": "mine", "side": "BUY",
         "size": 1.0, "price": 0.5, "ts": 1, "outcome": "Yes", "outcome_index": 0,
         "tx_hash": "0x1"},
        {"wallet": "0xb", "token_id": "t2", "condition_id": "theirs", "side": "BUY",
         "size": 1.0, "price": 0.5, "ts": 2, "outcome": "Yes", "outcome_index": 0,
         "tx_hash": "0x2"}])
    assert store.condition_ids_for_wallets(con, ["0xa"]) == {"mine"}
    assert store.condition_ids_for_wallets(con, ["0xa", "0xb"]) == {"mine", "theirs"}
    assert store.condition_ids_for_wallets(con, []) == set()


def test_wallet_scoping_is_case_insensitive(con):
    store.upsert_wallets(con, [{"address": "0xa", "source": "leaderboard", "rank": 1,
                                "username": "a", "vol": 1.0, "pnl": 1.0}])
    store.insert_trades(con, [
        {"wallet": "0xa", "token_id": "t1", "condition_id": "mine", "side": "BUY",
         "size": 1.0, "price": 0.5, "ts": 1, "outcome": "Yes", "outcome_index": 0,
         "tx_hash": "0x1"}])
    assert store.condition_ids_for_wallets(con, ["0xA"]) == {"mine"}


# --- paging deep enough to reach pre-cutoff history -----------------------


class _PagedClient:
    """A prolific wallet: the first pages are all recent (post-cutoff), older rows come later.

    This is the real shape of /closed-positions, which sorts newest first.
    """

    def __init__(self, recent_pages=3):
        self.recent_pages = recent_pages
        self.pages_served = 0

    def get_json(self, url, params=None, **kw):
        if "/value" in url:
            return [{"user": "0xa", "value": 100.0}]
        page = params["offset"] // 50
        self.pages_served = max(self.pages_served, page + 1)
        if page < self.recent_pages:
            return [_raw("2026-06-10T00:00:00Z", 1.0) for _ in range(50)]   # after cutoff
        return [_raw("2026-01-10T00:00:00Z", 1.0) for _ in range(50)]       # before cutoff


def test_paging_continues_past_pages_the_cutoff_will_discard(con):
    """The trap that made active wallets look unscoreable. /closed-positions is newest first,
    so a daily trader's first pages sit entirely inside the validation window and the cutoff
    filter throws them away. Stopping at a flat page cap would score exactly the wallets worth
    copying on nothing at all."""
    import datetime as dt
    cutoff = int(dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc).timestamp())
    client = _PagedClient(recent_pages=3)
    rows = discover.fetch_closed(client, "0xa", until_ts=cutoff, enough=60)
    pre = [p for p in rows if p["end_ts"] <= cutoff]
    assert client.pages_served > 3, "must page past the post-cutoff pages"
    assert len(pre) >= 60, "must keep going until enough pre-cutoff evidence is in hand"


def test_paging_stops_once_it_has_enough_old_positions(con):
    """...and having got enough, it must stop, or every wallet costs twenty requests."""
    import datetime as dt
    cutoff = int(dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc).timestamp())
    client = _PagedClient(recent_pages=0)
    discover.fetch_closed(client, "0xa", until_ts=cutoff, enough=60)
    assert client.pages_served == 2, "60 needed, 50 a page -- two pages is enough"


def test_without_a_cutoff_paging_is_unchanged(con):
    """The plain path still runs to the end of the history, as `polywatch trader` expects."""
    client = _PagedClient(recent_pages=0)
    discover.fetch_closed(client, "0xa", max_pages=4)
    assert client.pages_served == 4


def test_a_ranked_row_supplies_every_column_the_table_demands():
    """Regression. `as_row` omitted metrics_json, and store.upsert_trader_score binds by name,
    so the funnel scored 913 wallets over twenty minutes and then threw on the first insert --
    losing the entire stage. The column list is the contract; check against it, not by eye."""
    ranked = rank.rank([_score("0xa")])[0]
    row = ranked.as_row()
    missing = set(store.TRADER_SCORE_COLUMNS) - set(row) - {"scanned_at"}
    assert not missing, f"as_row is missing {missing}"


def test_a_ranked_row_actually_inserts(con):
    """The same contract, exercised end to end rather than by set arithmetic."""
    ranked = rank.rank([_score("0xa")])[0]
    store.upsert_trader_score(con, ranked.as_row())
    back = store.get_trader_score(con, "0xa")
    assert back is not None and back["rank_score"] == pytest.approx(ranked.rank_score)
    assert "components" in back["metrics_json"]


# --- following an arbitrary address ---------------------------------------


@pytest.mark.parametrize("bad", ["", "bogus", "0x123", "3f3aa7005f8006bfcc367d43a25cde25509fe8fd",
                                 "0xZZZaa7005f8006bfcc367d43a25cde25509fe8fd"])
def test_a_malformed_address_is_rejected_before_it_costs_a_request(bad):
    """A typo'd address and a real one with no history both come back empty, so the shape has
    to be checked here or the user is left wondering why their trader has no trades."""
    with pytest.raises(ValueError, match="not a Polymarket wallet address"):
        select.valid_address(bad)


def test_a_valid_address_is_normalised_to_lowercase():
    mixed = "0x3F3AA7005F8006BFCC367D43A25CDE25509FE8FD"
    assert select.valid_address(mixed) == mixed.lower()
    assert select.valid_address("  " + mixed + "  ") == mixed.lower()


def test_an_unknown_wallet_is_registered_so_later_stages_can_find_it(con, monkeypatch):
    """Handing the bot a cold address must work. The wallet row is what the market-scoping and
    screening stages look it up by."""
    calls = {}
    monkeypatch.setattr(select.ingest, "ingest_trades",
                        lambda *a, **k: calls.setdefault("trades", True))
    monkeypatch.setattr(select.ingest, "ingest_markets",
                        lambda *a, **k: calls.setdefault("markets", True))
    addr = "0x3f3aa7005f8006bfcc367d43a25cde25509fe8fd"
    select.prepare_wallet(con, object(), addr, since_ts=0)
    row = con.execute("SELECT * FROM wallets WHERE address=?", (addr,)).fetchone()
    assert row is not None and row["source"] == "manual"
    assert calls == {"trades": True, "markets": True}


def test_preparing_a_known_wallet_does_not_duplicate_it(con, monkeypatch):
    monkeypatch.setattr(select.ingest, "ingest_trades", lambda *a, **k: None)
    monkeypatch.setattr(select.ingest, "ingest_markets", lambda *a, **k: None)
    addr = "0x3f3aa7005f8006bfcc367d43a25cde25509fe8fd"
    store.upsert_wallets(con, [{"address": addr, "source": "leaderboard", "rank": 3,
                                "username": "known", "vol": 1.0, "pnl": 1.0}])
    select.prepare_wallet(con, object(), addr, since_ts=0)
    rows = con.execute("SELECT * FROM wallets WHERE address=?", (addr,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["username"] == "known", "an existing wallet must not be overwritten"


# --- the near-certainty trap ----------------------------------------------


def test_buying_near_certainties_earns_no_calibration_credit():
    """The live failure this was written for. A sweep of 3,107 wallets returned a top twelve
    with a Brier of 0.000 and an average entry of 0.998 -- wallets buying near-certainties at
    99.8c and collecting a dollar. Against a fixed 0.25 reference they scored a perfect 1.0.
    Forecasting 0.999 on a foregone conclusion is not judgement, and must not read as skill."""
    assert rank.calibration(0.0009, win_rate=0.999) == 0.0
    assert rank.calibration(0.001, win_rate=0.995) == 0.0


def test_a_genuine_forecaster_still_scores():
    """The correction must not throw out the traders it exists to find."""
    assert rank.calibration(0.18, win_rate=0.60) == pytest.approx(0.25)
    assert rank.calibration(0.12, win_rate=0.55) > 0.4


def test_calibration_against_an_unknown_base_rate_falls_back_to_the_coinflip():
    assert rank.calibration(0.125, None) == pytest.approx(0.5)


def test_a_near_certainty_trader_loses_to_a_forecaster_after_the_fix():
    scalper = _score("0xscalp", brier=0.001, consistency=1.0, roi=0.002, dd=0.0, n=240)
    scalper.win_rate = 0.999
    scalper.avg_entry_price = 0.998
    forecaster = _score("0xfore", brier=0.18, consistency=0.7, roi=0.35, dd=0.15, n=150)
    forecaster.win_rate = 0.60
    forecaster.avg_entry_price = 0.45
    assert [r.address for r in rank.rank([scalper, forecaster])][0] == "0xfore"


def _copyable_score(entry, win_rate=0.6):
    s = _score("0xa")
    s.avg_entry_price = entry
    s.win_rate = win_rate
    return s


def test_a_trader_above_the_price_band_is_not_copyable():
    ok, why = rank.copyable(_copyable_score(0.998), (0.02, 0.98), stake_usd=2.0)
    assert not ok and "outside" in why


def test_a_trader_the_stake_cannot_reach_is_not_copyable():
    """Five shares at 0.50 costs $2.50 -- more than a $2 stake can place, whatever the trader's
    quality. A fact about the bankroll, not about them."""
    ok, why = rank.copyable(_copyable_score(0.50), (0.02, 0.98), stake_usd=2.0)
    assert not ok and "5-share minimum" in why


def test_a_reachable_trader_passes():
    ok, why = rank.copyable(_copyable_score(0.30), (0.02, 0.98), stake_usd=2.0)
    assert ok and why is None


def test_a_bigger_stake_reaches_further():
    assert rank.copyable(_copyable_score(0.50), (0.02, 0.98), stake_usd=10.0)[0]


def test_a_thin_edge_is_flagged_as_inside_the_fee_schedule():
    r = rank.rank([_score("0xa", roi=0.002)])[0]
    assert any("thinner than" in d for d in rank.disqualifiers(r))


def test_a_near_certainty_entry_is_flagged():
    s = _score("0xa")
    s.avg_entry_price = 0.99
    r = rank.rank([s])[0]
    assert any("near-certainties" in d for d in rank.disqualifiers(r))
