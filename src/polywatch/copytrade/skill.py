"""Skill metrics for one wallet, derived from its settled positions.

`screen.py` answers "is this a human?"; this module answers "is this human any good?". They are
deliberately separate: screening is behavioural and cheap enough to run over a thousand wallets,
scoring is evidential and only worth running on the few dozen that survive.

Everything here is computed from data-api's /closed-positions, which already carries realized
PnL, average entry price, size and the settled outcome per market. The alternative -- replaying
every fill and matching it against market resolutions -- costs hundreds of extra requests per
wallet to arrive at the same numbers, and would additionally have to guess maker-vs-taker on
every fill to get fees right.

One caveat, stated rather than hidden: `realized_pnl` is upstream's own figure, and its exact
fee treatment is not documented. Comparing it against cost and settlement on a sampled position
on 2026-09-06 left an unexplained gap consistent with partial sells before resolution. It is
the best available number and it is internally consistent, but it is not one we derived.

All functions here are pure: they take parsed dicts from parse.records.parse_closed_positions
and return numbers. No network, no database.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Sequence

# A settled market prices its winning outcome at 1 and the rest at 0. Anything strictly between
# is not a settlement, and is excluded from Brier rather than rounded -- the same rule
# parse.records._resolution already applies for the analytics side.
SETTLED_HI = 0.999
SETTLED_LO = 0.001


@dataclass
class SkillScore:
    address: str
    n_closed: int
    win_rate: float
    roi: float
    realized_pnl: float
    invested: float
    brier: float | None
    n_brier: int
    avg_entry_price: float
    avg_stake_usd: float
    median_stake_usd: float
    max_drawdown: float
    consistency: float
    last_close_ts: int | None

    def as_row(self) -> dict:
        return asdict(self)


def _settled(pos: dict) -> int | None:
    """1 if the position's outcome came in, 0 if it did not, None if it never settled cleanly."""
    p = pos.get("cur_price")
    if p is None:
        return None
    if p >= SETTLED_HI:
        return 1
    if p <= SETTLED_LO:
        return 0
    return None


def win_rate(closed: Sequence[dict]) -> float:
    """Share of settled positions that made money.

    Measured on realized PnL, not on whether the outcome won. Those differ, and the difference
    is the point: a trader who buys at 0.95 and is right nine times in ten still loses money.
    """
    if not closed:
        return 0.0
    wins = sum(1 for p in closed if (p.get("realized_pnl") or 0.0) > 0)
    return wins / len(closed)


def roi(closed: Sequence[dict]) -> tuple[float, float, float]:
    """Returns (roi, realized_pnl, invested).

    Invested is the sum of entry cost across positions, not peak capital at risk, so this is
    return per dollar deployed rather than return on the account. For copy trading that is the
    more useful of the two -- we deploy per trade, not per portfolio.
    """
    invested = sum(p.get("cost") or 0.0 for p in closed)
    pnl = sum(p.get("realized_pnl") or 0.0 for p in closed)
    return (pnl / invested if invested > 0 else 0.0), pnl, invested


def brier(closed: Sequence[dict]) -> tuple[float | None, int]:
    """Mean squared error of entry price treated as a probability forecast.

    This is the one metric here that measures judgement rather than outcome. A trader can post
    a fine ROI on a handful of lucky longshots; they cannot post a good Brier score without
    being calibrated across many markets. 0.25 is what you get by guessing 0.5 every time, so
    anything at or above that is not evidence of skill.

    Returns (score, n) -- n matters, because a Brier over four markets means nothing.
    """
    errs = []
    for p in closed:
        outcome = _settled(p)
        if outcome is None:
            continue
        forecast = p.get("avg_price")
        if forecast is None:
            continue
        errs.append((forecast - outcome) ** 2)
    if not errs:
        return None, 0
    return sum(errs) / len(errs), len(errs)


def entry_stats(closed: Sequence[dict]) -> tuple[float, float, float]:
    """Returns (cost-weighted average entry price, mean stake, median stake).

    Entry price is weighted by cost rather than counted flat, so one $5 lottery ticket does not
    drag the average of a book full of $500 positions.
    """
    if not closed:
        return 0.0, 0.0, 0.0
    costs = [p.get("cost") or 0.0 for p in closed]
    total = sum(costs)
    if total > 0:
        avg_entry = sum((p.get("avg_price") or 0.0) * c for p, c in zip(closed, costs)) / total
    else:
        avg_entry = statistics.fmean([p.get("avg_price") or 0.0 for p in closed])
    return avg_entry, statistics.fmean(costs), statistics.median(costs)


def max_drawdown(closed: Sequence[dict]) -> float:
    """Worst peak-to-trough fall of the cumulative-PnL curve, as a fraction of capital staked.

    Positions are ordered by settlement time, which is the only timestamp /closed-positions
    gives us. That makes this a drawdown of *realized* PnL: it cannot see paper losses a trader
    sat through and recovered from. It is therefore an optimistic measure, and is used as a
    disqualifier -- a bad number here is damning, a good one is only mildly reassuring.

    The denominator is total invested, not the running peak. Peak-relative drawdown is the
    convention for an account curve that starts at its starting capital, but this curve starts
    at zero, so a $100 fall from a $50 peak comes out as 200% -- a number that is arithmetically
    correct and tells you nothing. Against capital staked it reads as "the worst cumulative
    setback cost half of everything they put in", which is the question actually being asked.
    """
    rows = sorted((p for p in closed if p.get("end_ts")), key=lambda p: p["end_ts"])
    invested = sum(p.get("cost") or 0.0 for p in rows)
    if invested <= 0:
        return 0.0
    equity = 0.0
    peak = 0.0
    worst_usd = 0.0
    for p in rows:
        equity += p.get("realized_pnl") or 0.0
        peak = max(peak, equity)
        worst_usd = max(worst_usd, peak - equity)
    return worst_usd / invested


def consistency(closed: Sequence[dict]) -> float:
    """Share of calendar months that finished in profit.

    A trader who is up on nine months out of ten is a different proposition from one whose
    entire record is a single enormous month, even at identical total PnL. Since the target is
    small wins over time rather than one big score, this is weighted heavily in ranking.
    """
    months: dict[str, float] = {}
    for p in closed:
        ts = p.get("end_ts")
        if not ts:
            continue
        key = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")
        months[key] = months.get(key, 0.0) + (p.get("realized_pnl") or 0.0)
    if not months:
        return 0.0
    return sum(1 for v in months.values() if v > 0) / len(months)


def score(address: str, closed: Sequence[dict]) -> SkillScore:
    """Every metric above, over one wallet's settled positions."""
    r, pnl, invested = roi(closed)
    b, n_b = brier(closed)
    avg_entry, avg_stake, med_stake = entry_stats(closed)
    ends = [p["end_ts"] for p in closed if p.get("end_ts")]
    return SkillScore(
        address=address.lower(),
        n_closed=len(closed),
        win_rate=win_rate(closed),
        roi=r,
        realized_pnl=pnl,
        invested=invested,
        brier=b,
        n_brier=n_b,
        avg_entry_price=avg_entry,
        avg_stake_usd=avg_stake,
        median_stake_usd=med_stake,
        max_drawdown=max_drawdown(closed),
        consistency=consistency(closed),
        last_close_ts=max(ends) if ends else None,
    )
