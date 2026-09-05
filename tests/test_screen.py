"""Screening tests: the bot/human separation the wallet selection depends on."""

import sqlite3

from polywatch.screen import Profile, Thresholds, profile_wallet, verdict, vol_cap_from

DAY = 86400


def _row(address="0xabc", username="u", vol=100_000.0, pnl=1000.0):
    return {"address": address, "username": username, "vol": vol, "pnl": pnl}


def _trades(times, condition_prefix="m", side="BUY", size=10.0):
    return [
        sqlite3.Row  # not used; plain dicts satisfy the same key access
        and {"ts": t, "side": side, "size": size, "condition_id": f"{condition_prefix}{i}"}
        for i, t in enumerate(times)
    ]


def test_bot_cadence_is_visible_in_the_profile():
    now = 1_000_000
    # 500 trades, one every 5 seconds: a machine.
    times = [now - 2500 + 5 * i for i in range(500)]
    p = profile_wallet(_row(), _trades(times), now)
    assert p.median_gap_s == 5
    assert p.burst_frac == 1.0
    assert p.trades_per_day > 1000


def test_human_cadence_passes_defaults():
    now = 1_000_000
    # 60 trades spread two per day over 30 days.
    times = [now - 30 * DAY + d * DAY + h * 3600 for d in range(30) for h in (1, 9)]
    p = profile_wallet(_row(), _trades(times), now)
    ok, fails = verdict(p, Thresholds(), vol_cap=None)
    assert ok, fails
    assert p.active_days == 30 and p.active_day_ratio <= 1.0


def test_active_day_ratio_never_exceeds_one():
    now = 1_000_000
    p = profile_wallet(_row(), _trades([now - 3600, now - 60]), now)
    assert p.active_day_ratio <= 1.0


def test_stale_wallet_rejected_even_if_history_looks_good():
    now = 1_000_000
    times = [now - 200 * DAY + d * DAY for d in range(60)]
    p = profile_wallet(_row(), _trades(times), now)
    ok, fails = verdict(p, Thresholds(), vol_cap=None)
    assert not ok and any("stale" in f for f in fails)


def test_one_burst_record_rejected_by_active_day_ratio():
    now = 1_000_000
    # 40 trades inside one day, then nothing for 60 days: a single session, not a track record.
    times = [now - 60 * DAY + 600 * i for i in range(40)]
    p = profile_wallet(_row(), _trades(times), now)
    ok, fails = verdict(p, Thresholds(), vol_cap=None)
    assert not ok


def test_volume_cap_is_relative_to_the_cohort():
    profiles = [Profile("a", None, v, 0, 10, 10, 1, 1, 10, 1, 300, 0.1, 20, 0.5, 10)
                for v in (10, 20, 30, 40, 100)]
    cap = vol_cap_from(profiles, 80.0)
    assert cap == 100  # index 4 of the sorted vols
    assert vol_cap_from(profiles, 100.0) is None


def test_empty_wallet_is_rejected_not_crashed():
    p = profile_wallet(_row(), [], 1_000_000)
    ok, fails = verdict(p, Thresholds(), vol_cap=None)
    assert not ok and fails == ["no trades in window"]
