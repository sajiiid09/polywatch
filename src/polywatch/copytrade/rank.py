"""Turning a wallet's settled record into one comparable number.

`screen.py` asks "is this a human?". `skill.py` asks "is this human any good?" and answers with
six separate metrics. This module answers "which of these humans should we copy?", which needs
the six collapsed into an ordering.

The collapse is a weighted sum with the weights written down in `config.RANK_WEIGHTS`, not a
fitted model. That is a deliberate limitation: with a few hundred candidates and one leaderboard
snapshot, anything fitted would be fitted to noise, and a number nobody can argue with is worse
than a number somebody can. Every weight is a claim about what makes a trader copyable, and a
reader who disagrees can change one line.

Two of the components deserve their reasoning stated rather than assumed.

Calibration dominates at 0.30 because it is the only component that cannot be bought with luck.
A trader can post a fine ROI on a handful of longshots that happened to land; they cannot post a
good Brier score across a hundred markets without being genuinely calibrated. And it is scored
against 0.25 -- the Brier you get by forecasting 0.5 on everything -- so a wallet at or above
that contributes exactly zero here rather than a small positive amount. The wallet already in
the database is the cautionary case: +44% ROI on a Brier of 0.371, which is worse than a coin
flip, and a naive PnL ranking would have put it first.

Everything is normalised across the cohort rather than against absolute targets, because "good
ROI" only means anything relative to the other wallets available on the same day.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..config import BRIER_COINFLIP, RANK_WEIGHTS


@dataclass
class RankedTrader:
    address: str
    rank_score: float
    components: dict
    score: object          # the skill.SkillScore this was computed from
    est_account_usd: float = 0.0
    top_category: str | None = None

    def as_row(self) -> dict:
        s = self.score
        return {
            "address": self.address,
            "n_closed": s.n_closed,
            "win_rate": s.win_rate,
            "roi": s.roi,
            "realized_pnl": s.realized_pnl,
            "brier": s.brier,
            "avg_entry_price": s.avg_entry_price,
            "avg_stake_usd": s.avg_stake_usd,
            "max_drawdown": s.max_drawdown,
            "consistency": s.consistency,
            "est_account_usd": self.est_account_usd,
            "top_category": self.top_category,
            "persona_fit": None,
            "rank_score": self.rank_score,
            # The normalised components alongside the raw metrics, so a ranking can be argued
            # with after the fact: "why did this wallet beat that one" is answerable from the
            # database rather than only by re-running the funnel.
            "metrics_json": json.dumps({**s.as_row(), "components": self.components},
                                       sort_keys=True),
        }


def calibration(brier: float | None, win_rate: float | None = None) -> float:
    """Brier skill, measured against the trader's own base rate rather than against 0.25.

    The first version of this scored `1 - brier/0.25`, and a live sweep of 3,107 wallets showed
    exactly what is wrong with that. The entire top twelve came back with a Brier of 0.000 and
    an average entry price of 0.998: wallets that buy near-certainties at 99.8 cents and collect
    a dollar. Forecasting 0.999 on something that resolves 1 produces a near-perfect Brier, but
    it demonstrates no judgement at all -- the market had already done the forecasting, and the
    trader is collecting a spread. Against a fixed 0.25 reference they scored a perfect 1.0 and
    swept the shortlist.

    The standard correction is a skill score: compare the forecast against the best you could do
    knowing only how often this trader's positions win. A trader who wins 99.9% of the time has
    a reference Brier of p(1-p) ~= 0.001, so predicting 0.999 every time beats nothing and earns
    nothing here. A trader calling coin-flip markets at 60% has a reference of 0.24, and beating
    that is real evidence.

    Falls back to the fixed 0.25 reference when the base rate is unknown, and returns 0 when the
    reference is so small that no forecast could show skill against it.
    """
    if brier is None:
        return 0.0
    reference = BRIER_COINFLIP if win_rate is None else win_rate * (1.0 - win_rate)
    # A base rate this lopsided leaves no room to demonstrate anything: every outcome was a
    # foregone conclusion, so calibration carries no information either way.
    if reference < 0.01:
        return 0.0
    return max(0.0, min(1.0, 1.0 - brier / reference))


def raw_components(score) -> dict:
    """The five components for one wallet, before cohort normalisation.

    Each is oriented so that larger is better -- drawdown and Brier are both inverted here so
    the weighted sum never has to carry a minus sign, which is exactly the kind of detail that
    silently flips a ranking.
    """
    return {
        "calibration": calibration(score.brier, score.win_rate),
        "consistency": score.consistency,
        "roi": score.roi,
        "drawdown": 1.0 - score.max_drawdown,
        "evidence": min(score.n_closed / 100.0, 1.0),
    }


def normalise(values: list[float]) -> list[float]:
    """Min-max a component across the cohort, onto 0..1.

    A cohort where every wallet scores identically -- including a cohort of one -- returns 0.5
    for all of them rather than dividing by zero. Not 1.0: a component that separates nobody
    should not hand everybody full marks and quietly dominate the sum.
    """
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def rank(scores: list, accounts: dict | None = None,
         categories: dict | None = None) -> list[RankedTrader]:
    """Rank a cohort of skill.SkillScore objects, best first.

    Normalisation is per-cohort, so this is only meaningful over wallets scored on the same
    window -- ranking a wallet scored today against one scored last month would compare their
    positions in two different fields.
    """
    if not scores:
        return []
    accounts = accounts or {}
    categories = categories or {}

    raw = [raw_components(s) for s in scores]
    normed = {k: normalise([r[k] for r in raw]) for k in RANK_WEIGHTS}

    out = []
    for i, s in enumerate(scores):
        components = {k: normed[k][i] for k in RANK_WEIGHTS}
        total = sum(RANK_WEIGHTS[k] * components[k] for k in RANK_WEIGHTS)
        out.append(RankedTrader(
            address=s.address, rank_score=total, components=components, score=s,
            est_account_usd=accounts.get(s.address, 0.0),
            top_category=categories.get(s.address),
        ))
    out.sort(key=lambda r: r.rank_score, reverse=True)
    return out


def copyable(score, band: tuple[float, float], stake_usd: float,
             min_shares: float = 5.0) -> tuple[bool, str | None]:
    """Can this trader be followed at all, at our bankroll and settings?

    Ranking a wallet we cannot copy is worse than useless -- it fills the shortlist and pushes
    out wallets we could have followed. The live sweep produced twelve finalists in a row that
    the engine then skipped every single trade of, so this check exists to run *before* the
    expensive validation rather than to explain its emptiness afterwards.

    Two independent reasons a trader is out of reach:

      price   their entries sit outside the style/risk band, so every gate rejects them;
      size    Polymarket's ~5 share minimum means an entry at price p costs at least 5p, and
              below that our stake cannot place the order at all. At a $2 stake nothing above
              $0.40 is reachable -- which is a fact about the bankroll, not about the trader.
    """
    lo, hi = band
    entry = score.avg_entry_price
    if not lo <= entry <= hi:
        return False, f"average entry {entry:.3f} sits outside the {lo:.2f}-{hi:.2f} band"
    if entry * min_shares > stake_usd:
        return False, (f"entry {entry:.3f} needs ${entry * min_shares:.2f} to clear the "
                       f"{min_shares:.0f}-share minimum, above the ${stake_usd:.2f} stake")
    return True, None


def from_stored(rows: list[dict]) -> tuple[list, dict]:
    """Rebuild (skill scores, account values) from persisted `trader_scores` rows.

    `metrics_json` holds the whole SkillScore as written, so a ranking can be recomputed after
    the formula changes without touching the network.
    """
    from .skill import SkillScore
    fields = SkillScore.__dataclass_fields__
    scores, accounts = [], {}
    for row in rows:
        try:
            m = json.loads(row["metrics_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        m.pop("components", None)
        if not all(f in m for f in ("address", "n_closed")):
            continue
        scores.append(SkillScore(**{k: m[k] for k in fields if k in m}))
        accounts[row["address"]] = row.get("est_account_usd") or 0.0
    return scores, accounts


def disqualifiers(r: RankedTrader) -> list[str]:
    """Reasons this wallet should not be copied, regardless of where it ranked.

    Separate from the score on purpose. A weighted sum will happily average a fatal flaw away --
    a wallet with a coin-flip Brier can still finish mid-table on ROI and consistency alone --
    so the fatal flaws are checked as flags rather than as weights.
    """
    out = []
    s = r.score
    if s.brier is None:
        out.append("no settled positions to calibrate against")
    elif s.brier >= BRIER_COINFLIP:
        out.append(f"brier {s.brier:.3f} is no better than guessing 50/50")
    if s.roi <= 0:
        out.append(f"roi {s.roi:+.1%} over the ranking window")
    elif s.roi < 0.02:
        # A sub-2% return per dollar deployed is inside the fee schedule. Copying it converts a
        # thin real edge into a reliable loss.
        out.append(f"roi {s.roi:+.2%} is thinner than the ~4% taker fee round trip")
    if s.avg_entry_price > 0.95:
        out.append(f"average entry {s.avg_entry_price:.3f} -- buying near-certainties, which "
                   "needs size rather than judgement")
    if s.max_drawdown > 0.5:
        out.append(f"drawdown {s.max_drawdown:.0%} of capital staked")
    if s.consistency < 0.5:
        out.append(f"only {s.consistency:.0%} of months finished in profit")
    return out
