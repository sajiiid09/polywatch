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

import random
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Sequence

from ..config import FEE_FALLBACK_DEFAULT, MIN_SAMPLE_FOR_LUCK
from . import book as bk

# A settled market prices its winning outcome at 1 and the rest at 0. Anything strictly between
# is not a settlement, and is excluded from Brier rather than rounded -- the same rule
# parse.records._resolution already applies for the analytics side.
SETTLED_HI = 0.999
SETTLED_LO = 0.001


@dataclass
class RoundTrip:
    """One matched buy-then-sell in a single token. The unit a copier actually copies."""
    token_id: str
    condition_id: str
    entry_ts: int
    exit_ts: int
    shares: float
    entry_price: float
    exit_price: float

    @property
    def hold_s(self) -> int:
        return max(0, self.exit_ts - self.entry_ts)


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
    # --- copyability, added in Phase 3. All optional: they need data the score card does not
    # always have (trades for hold times, a fee rate per market, a clock for recency).
    hold_p10_s: int | None = None
    hold_p50_s: int | None = None
    hold_p90_s: int | None = None
    n_round_trips: int = 0
    fee_adjusted_roi: float | None = None
    recent_roi: float | None = None
    luck_p: float | None = None
    top_category: str | None = None
    category_concentration: float | None = None

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


def match_round_trips(trades: Sequence[dict]) -> list[RoundTrip]:
    """Pair a wallet's buys with its later sells, FIFO, per token.

    This is the unit a copier copies: not a position that eventually settled, but a decision to
    get in and a decision to get out, with a measurable gap between them. Everything about
    copyability -- how long we have to be right, whether the price was still moving when we
    arrived -- is a property of that gap.

    FIFO rather than average cost because the question is temporal. Averaging a wallet's entries
    would smear a two-minute flip and a two-week hold in the same token into one number that
    describes neither.

    Sells with no matching buy are dropped rather than guessed at: they belong to a position
    opened before whatever window these trades came from, and inventing an entry price for them
    would put a fabricated number into the very metric this exists to measure.
    """
    lots: dict[str, list[list]] = {}
    out: list[RoundTrip] = []
    for tr in sorted(trades, key=lambda t: t["ts"]):
        token = tr.get("token_id")
        size = tr.get("size") or 0.0
        price = tr.get("price")
        if not token or size <= 0 or price is None:
            continue
        if tr.get("side") == "BUY":
            lots.setdefault(token, []).append([tr["ts"], size, price, tr.get("condition_id", "")])
            continue
        if tr.get("side") != "SELL":
            continue
        remaining = size
        queue = lots.get(token) or []
        while remaining > 1e-9 and queue:
            lot = queue[0]
            take = min(lot[1], remaining)
            out.append(RoundTrip(token, lot[3], lot[0], tr["ts"], take, lot[2], price))
            lot[1] -= take
            remaining -= take
            if lot[1] <= 1e-9:
                queue.pop(0)
    return out


def _pct(xs: Sequence[float], p: float) -> float:
    """Nearest-rank percentile, so every reported value is one that actually happened."""
    ordered = sorted(xs)
    idx = min(len(ordered) - 1, max(0, int(round(p / 100 * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def hold_times(trips: Sequence[RoundTrip]) -> dict:
    """How long this wallet's round trips last, in seconds.

    The gate on everything else. A copier fifteen seconds late can still join a twenty-minute
    flip; it cannot join a forty-second scalp at all, however good the scalper is. The tenth
    percentile matters more than the median here -- it says what share of their trades are over
    before we could have acted.
    """
    if not trips:
        return {"n": 0}
    hs = [t.hold_s for t in trips]
    return {"n": len(hs), "p10": int(_pct(hs, 10)), "p50": int(_pct(hs, 50)),
            "p90": int(_pct(hs, 90))}


def fee_adjusted_roi(closed: Sequence[dict], rate_of=None) -> float:
    """Their ROI with both taker fees charged to it -- the first correction toward ours.

    `roi()` above is the trader's own return at their own entry price, and Polymarket's fee
    schedule means that number can be positive while a copier's is not: a round trip costs 10% of
    stake at even odds. This charges each position the round trip its entry price implies.

    It is still optimistic -- it does not charge our slippage, and it assumes we got their
    price -- so read it as an upper bound on what a copy would have returned, and use replay for
    the number that is not.
    """
    invested = sum(p.get("cost") or 0.0 for p in closed)
    if invested <= 0:
        return 0.0
    net = 0.0
    for p in closed:
        cost = p.get("cost") or 0.0
        rate = rate_of(p) if rate_of else FEE_FALLBACK_DEFAULT
        net += (p.get("realized_pnl") or 0.0) - cost * bk.round_trip_fee_frac(
            p.get("avg_price") or 0.0, rate)
    return net / invested


def recency_weighted_roi(closed: Sequence[dict], now: int, half_life_days: float = 30.0
                         ) -> float | None:
    """ROI with older positions discounted by an exponential half-life.

    A wallet that was excellent in March and is bleeding now scores identically to one improving
    at the same lifetime total, and only one of them is worth copying tomorrow. Thirty days is
    chosen to be roughly the horizon over which a prediction-market edge can plausibly persist;
    it is a knob, not a law.
    """
    num = den = 0.0
    for p in closed:
        ts = p.get("end_ts")
        cost = p.get("cost") or 0.0
        if not ts or cost <= 0:
            continue
        age_days = max(0.0, (now - ts) / 86400)
        w = 0.5 ** (age_days / half_life_days)
        num += w * (p.get("realized_pnl") or 0.0)
        den += w * cost
    return (num / den) if den > 0 else None


def luck_pvalue(closed: Sequence[dict], iterations: int = 2000, seed: int = 0) -> float | None:
    """How often a record this good arises from resampling this wallet's own trades.

    A bootstrap: draw `len(closed)` positions with replacement from their own results and total
    the PnL, `iterations` times. The p-value is the share of those totals that came out at or
    below zero -- that is, how fragile the profit is to which trades happened to land.

    It answers "is this distinguishable from luck", which is the question the project is named
    for and which win rate and ROI cannot answer at all: eleven winning coin flips and a real
    edge look identical in both. It is deliberately not a test against a market benchmark; it
    only says whether *this* wallet's own dispersion swamps its own mean.

    None below a floor of settled positions, because a p-value over four trades is theatre.
    """
    pnls = [p.get("realized_pnl") or 0.0 for p in closed]
    if len(pnls) < MIN_SAMPLE_FOR_LUCK or all(x == 0 for x in pnls):
        return None
    rng = random.Random(seed)          # seeded: the same wallet must score the same twice
    n = len(pnls)
    losses = 0
    for _ in range(iterations):
        if sum(pnls[rng.randrange(n)] for _ in range(n)) <= 0:
            losses += 1
    return losses / iterations


def category_mix(closed: Sequence[dict], category_of=None) -> tuple[str | None, float | None]:
    """Where their PnL comes from: (top category, concentration).

    Concentration is a Herfindahl index over positive PnL by category -- 1.0 means every dollar
    they made came from one category, near 0 means it is spread thin. Neither end is good or bad
    on its own. A concentrated wallet is worth copying *in that category*, which is what
    `persona_fit` is for; a diffuse one is either genuinely broad or is being carried by variance.
    """
    if category_of is None:
        return None, None
    by_cat: dict[str, float] = {}
    for p in closed:
        pnl = p.get("realized_pnl") or 0.0
        if pnl <= 0:
            continue
        cat = category_of(p) or "unknown"
        by_cat[cat] = by_cat.get(cat, 0.0) + pnl
    total = sum(by_cat.values())
    if total <= 0:
        return None, None
    top = max(by_cat, key=lambda k: by_cat[k])
    hhi = sum((v / total) ** 2 for v in by_cat.values())
    return top, hhi


def score(address: str, closed: Sequence[dict], *, trades: Sequence[dict] | None = None,
          now: int | None = None, rate_of=None, category_of=None) -> SkillScore:
    """Every metric above, over one wallet's settled positions.

    The copyability metrics need more than settled positions -- hold times need the fills, the
    fee adjustment needs a rate per market, recency needs a clock -- so each is computed only
    when it was given what it needs and left as None otherwise. A missing metric is reported as
    missing rather than defaulted to something flattering.
    """
    r, pnl, invested = roi(closed)
    b, n_b = brier(closed)
    avg_entry, avg_stake, med_stake = entry_stats(closed)
    ends = [p["end_ts"] for p in closed if p.get("end_ts")]
    holds = hold_times(match_round_trips(trades)) if trades else {"n": 0}
    top_cat, hhi = category_mix(closed, category_of)
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
        hold_p10_s=holds.get("p10"),
        hold_p50_s=holds.get("p50"),
        hold_p90_s=holds.get("p90"),
        n_round_trips=holds.get("n", 0),
        fee_adjusted_roi=fee_adjusted_roi(closed, rate_of) if closed else None,
        recent_roi=recency_weighted_roi(closed, now) if (closed and now) else None,
        luck_p=luck_pvalue(closed),
        top_category=top_cat,
        category_concentration=hhi,
    )
