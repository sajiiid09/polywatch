"""Wallet screening: separate plausible humans from bots inside the leaderboard's top N.

The leaderboard ranks on PnL alone, so its top slots fill with high-frequency market-making bots
whose edge is latency, not judgement. Copying those is impossible in practice -- by the time a
copier sees the fill, the edge is gone -- and their trades also swamp any Brier estimate with
thousands of near-coinflip scalps.

So before spending thousands of requests on full history, each candidate gets a cheap recon pass
(one page of recent trades) and a profile. The thresholds below are defaults, not truths: every
one is a CLI flag, and `polywatch screen` prints the full profile table so they can be moved
with the data in view.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


@dataclass
class Thresholds:
    max_trades_per_day: float = 60.0   # above this is a machine, not a person with opinions
    min_median_gap_s: float = 60.0     # sub-minute median spacing is automation
    max_burst_frac: float = 0.50       # share of trades within 60s of the previous one
    max_recency_days: float = 7.0      # must still be trading; a cold wallet can't be copied
    min_active_days: int = 5           # a real record spans days, not one lucky session
    min_active_day_ratio: float = 0.25  # ...and is spread across its span, not one burst
    min_distinct_markets: int = 10     # diversification, not one market ground repeatedly
    max_vol_percentile: float = 80.0   # drop the biggest-volume wallets in the candidate set


@dataclass
class Profile:
    address: str
    username: str | None
    vol: float | None
    pnl: float | None
    n_trades: int
    span_days: float
    trades_per_day: float
    last_trade_age_days: float
    active_days: int
    active_day_ratio: float
    median_gap_s: float
    burst_frac: float
    distinct_markets: int
    buy_frac: float
    median_size: float

    def as_row(self) -> dict:
        return asdict(self)


def profile_wallet(row: sqlite3.Row, trades: list[sqlite3.Row], now: int) -> Profile:
    """Behavioural fingerprint from a wallet's recent trades."""
    ts = sorted(t["ts"] for t in trades)
    n = len(ts)
    if n == 0:
        return Profile(row["address"], row["username"], row["vol"], row["pnl"], 0, 0.0, 0.0,
                       float("inf"), 0, 0.0, 0.0, 0.0, 0, 0.0, 0.0)

    span_s = max(ts[-1] - ts[0], 1)
    span_days = span_s / 86400
    gaps = [b - a for a, b in zip(ts, ts[1:])] or [span_s]
    days = {datetime.fromtimestamp(t, timezone.utc).date() for t in ts}
    sizes = [t["size"] for t in trades]

    return Profile(
        address=row["address"],
        username=row["username"],
        vol=row["vol"],
        pnl=row["pnl"],
        n_trades=n,
        span_days=round(span_days, 2),
        trades_per_day=round(n / max(span_days, 1 / 24), 2),
        last_trade_age_days=round((now - ts[-1]) / 86400, 2),
        active_days=len(days),
        # Distinct active days over calendar days spanned: 1.0 means traded every single day,
        # near 0 means one burst inside a long dormant stretch.
        # Capped at 1.0: a page covering less than a day can otherwise report a ratio above 1,
        # which would read as "more consistent than every day".
        active_day_ratio=round(min(1.0, len(days) / max(span_days, 1.0)), 3),
        median_gap_s=round(statistics.median(gaps), 1),
        burst_frac=round(sum(1 for g in gaps if g < 60) / len(gaps), 3),
        distinct_markets=len({t["condition_id"] for t in trades}),
        buy_frac=round(sum(1 for t in trades if t["side"] == "BUY") / n, 3),
        median_size=round(statistics.median(sizes), 2),
    )


def verdict(p: Profile, th: Thresholds, vol_cap: float | None) -> tuple[bool, list[str]]:
    """Returns (passes, reasons it failed). Reasons are kept so rejections are auditable."""
    fails = []
    if p.n_trades == 0:
        return False, ["no trades in window"]
    if p.trades_per_day > th.max_trades_per_day:
        fails.append(f"trades/day {p.trades_per_day} > {th.max_trades_per_day}")
    if p.median_gap_s < th.min_median_gap_s:
        fails.append(f"median gap {p.median_gap_s}s < {th.min_median_gap_s}s")
    if p.burst_frac > th.max_burst_frac:
        fails.append(f"burst frac {p.burst_frac} > {th.max_burst_frac}")
    if p.last_trade_age_days > th.max_recency_days:
        fails.append(f"stale {p.last_trade_age_days}d > {th.max_recency_days}d")
    if p.active_days < th.min_active_days:
        fails.append(f"active days {p.active_days} < {th.min_active_days}")
    if p.active_day_ratio < th.min_active_day_ratio:
        fails.append(f"active-day ratio {p.active_day_ratio} < {th.min_active_day_ratio}")
    if p.distinct_markets < th.min_distinct_markets:
        fails.append(f"markets {p.distinct_markets} < {th.min_distinct_markets}")
    if vol_cap is not None and (p.vol or 0) > vol_cap:
        fails.append(f"volume {p.vol:,.0f} > cap {vol_cap:,.0f}")
    return (not fails), fails


def vol_cap_from(profiles: list[Profile], percentile: float) -> float | None:
    """Volume ceiling as a percentile of the candidate set rather than an absolute number --
    'big' only means anything relative to the cohort being screened."""
    vols = sorted(p.vol for p in profiles if p.vol is not None)
    if not vols or percentile >= 100:
        return None
    idx = min(len(vols) - 1, int(len(vols) * percentile / 100))
    return vols[idx]


def screen(con: sqlite3.Connection, th: Thresholds, now: int | None = None
           ) -> list[tuple[Profile, bool, list[str]]]:
    """Profile every known wallet from the trades already in the DB, then apply thresholds."""
    import time as _t
    now = now or int(_t.time())
    out = []
    profiles = []
    for row in con.execute("SELECT * FROM wallets ORDER BY rank"):
        trades = con.execute(
            "SELECT ts, side, size, condition_id FROM trades WHERE wallet=?", (row["address"],)
        ).fetchall()
        profiles.append((row, profile_wallet(row, trades, now)))

    cap = vol_cap_from([p for _, p in profiles], th.max_vol_percentile)
    for _, p in profiles:
        ok, fails = verdict(p, th, cap)
        out.append((p, ok, fails))
    return out
