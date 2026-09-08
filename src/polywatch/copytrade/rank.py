"""Turn a pile of metrics into one number, and be explicit about what that number believes.

Ranking is where the judgement is, so it is kept in one small file with its weights in
`config.py` rather than spread across the code that computes the inputs. Everything here is pure:
scores in, score out.

The shape of the opinion:

  * **Copyability dominates.** `capture_ratio` -- how much of a wallet's edge survives one poll
    interval -- carries more weight than any measure of how good the wallet is, because a great
    trader we cannot follow is worth exactly zero and a mediocre one we can follow is worth
    something. A wallet below `MIN_CAPTURE_RATIO` is not ranked at all; it is excluded.
  * **Consistency over magnitude.** A wallet up in nine months out of ten beats one whose entire
    record is a single enormous month, at equal total PnL. The target is small wins repeated,
    not a lottery ticket.
  * **Evidence over outcome.** A good Brier score is hard to fake and a good ROI is not. A record
    that cannot be distinguished from luck is discounted however large it is.
  * **Every component is bounded and directional.** Each sub-score maps to [0, 1] with 0.5 as
    "unremarkable", so a missing metric can default to 0.5 and neither reward nor punish the
    wallet for data we did not have. Nothing here can be gamed by making one number enormous.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import (MIN_CAPTURE_RATIO, MIN_SAMPLE_FOR_LUCK, RANK_WEIGHTS)

# Hold times a copier can and cannot work with, in seconds. Under the floor the trade is over
# before a 15-second poll could have seen it; over the ceiling it is not a flip and the quick-flip
# machinery is the wrong tool, though the wallet may be perfectly good.
HOLD_FLOOR_S = 120
HOLD_IDEAL_S = 1200
HOLD_CEILING_S = 6 * 3600


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _ramp(x: float | None, bad: float, good: float, default: float = 0.5) -> float:
    """Linear score from `bad` (0) to `good` (1). Works in either direction."""
    if x is None:
        return default
    if good == bad:
        return default
    return _clamp((x - bad) / (good - bad))


def hold_score(p50_s: int | None, p10_s: int | None = None) -> float:
    """Is this wallet's holding period one a poller can actually work with?

    Peaks in the twenty-minute region: long enough that a fifteen-second copy is still early,
    short enough that the exit ladder and the session clock mean something. The tenth percentile
    is a separate penalty, because a wallet with a fine median and a fat tail of forty-second
    scalps will have those scalps skipped as stale -- which shows up as a low fill rate rather
    than as a loss, and is easy to miss.
    """
    if p50_s is None:
        return 0.5
    if p50_s <= HOLD_FLOOR_S:
        base = _ramp(p50_s, 0, HOLD_FLOOR_S) * 0.3
    elif p50_s <= HOLD_IDEAL_S:
        base = 0.3 + 0.7 * _ramp(p50_s, HOLD_FLOOR_S, HOLD_IDEAL_S)
    else:
        base = 1.0 - 0.5 * _ramp(p50_s, HOLD_IDEAL_S, HOLD_CEILING_S)
    if p10_s is not None and p10_s < HOLD_FLOOR_S:
        base *= 0.75          # a fat tail of uncopyable scalps, whatever the median says
    return _clamp(base)


def brier_score(brier: float | None, n: int) -> float:
    """0.25 is what guessing 0.5 every time earns, so it is the zero of this scale.

    Discounted toward neutral when the sample is thin: a Brier over four markets is noise wearing
    the clothes of a measurement.
    """
    if brier is None or n <= 0:
        return 0.5
    raw = _ramp(brier, 0.25, 0.10)                 # lower is better
    confidence = _clamp(n / MIN_SAMPLE_FOR_LUCK)
    return 0.5 + (raw - 0.5) * confidence


def luck_score(p: float | None) -> float:
    """A record indistinguishable from its own variance is discounted, not rejected.

    Rejected would be too strong: a real edge over thirty trades can sit at p = 0.2 and still be
    real. But it should not outrank a record that is unambiguous.
    """
    if p is None:
        return 0.5
    return _ramp(p, 0.50, 0.02)                    # lower p is better


@dataclass
class Ranked:
    address: str
    rank_score: float
    persona_fit: float | None
    components: dict
    excluded: str | None = None                    # why this wallet is not a candidate at all


def _usable(replay) -> bool:
    """Does this replay have enough price coverage to be allowed an opinion?"""
    return replay is not None and getattr(replay, "trustworthy", True)


def components(sc, replay=None) -> dict:
    """Every sub-score, kept separately so a ranking can be explained rather than trusted."""
    usable = _usable(replay)
    capture = replay.capture_ratio if usable else None
    half_life = replay.edge_half_life_s if usable else None
    return {
        # Copyability: the two numbers only replay can produce.
        "capture": _ramp(capture, 0.0, 0.60),
        "persistence": _ramp(half_life, 60.0, 900.0),
        "hold": hold_score(sc.hold_p50_s, sc.hold_p10_s),
        # Quality of the record itself.
        "brier": brier_score(sc.brier, sc.n_brier),
        "consistency": _clamp(sc.consistency),
        "luck": luck_score(sc.luck_p),
        # Return, after fees and weighted toward recent form. Capped well below what an
        # exceptional wallet posts, so an outlier cannot buy its way past the other components.
        "fee_roi": _ramp(sc.fee_adjusted_roi, 0.0, 0.25),
        "recent": _ramp(sc.recent_roi, -0.05, 0.20),
        # Drawdown is a disqualifier rather than a virtue: it is measured on realized PnL and so
        # cannot see paper losses they sat through. A good number here is only mildly reassuring.
        "drawdown": 1.0 - _ramp(sc.max_drawdown, 0.0, 0.60),
    }


def rank(sc, replay=None, weights: dict | None = None) -> Ranked:
    """One wallet's rank score, with the reason it was excluded when it was.

    Exclusions are hard gates rather than heavy penalties, because they are statements about
    whether the wallet is a candidate at all -- not about how good a candidate it is.
    """
    w = weights or RANK_WEIGHTS
    comp = components(sc, replay)

    # A replay drawn through a minority of a wallet's round trips does not get to exclude it.
    # Thin coverage is a fact about which price windows we happen to have fetched, not about the
    # trader, and acting on it would quietly reject wallets for being unfamiliar.
    verdict = replay if _usable(replay) else None

    excluded = None
    if sc.n_closed < MIN_SAMPLE_FOR_LUCK:
        excluded = f"only {sc.n_closed} settled positions"
    elif verdict is not None and verdict.capture_ratio is not None \
            and verdict.capture_ratio < MIN_CAPTURE_RATIO:
        excluded = (f"capture ratio {verdict.capture_ratio:.2f} -- their edge does not survive "
                    f"being copied")
    elif verdict is not None and verdict.edge_half_life_s is not None \
            and verdict.edge_half_life_s < 60:
        excluded = f"edge half-life {verdict.edge_half_life_s:.0f}s -- gone before we could act"

    total = sum(comp[k] * w.get(k, 0.0) for k in comp)
    denom = sum(w.get(k, 0.0) for k in comp) or 1.0
    return Ranked(sc.address, _clamp(total / denom), None, comp, excluded)


def persona_fit(sc, task_like) -> float:
    """How well a wallet matches the shape of task someone wants to run.

    Separate from `rank_score` on purpose: rank says whether a wallet is any good, fit says
    whether it is good *for this*. A superb slow-thesis trader ranks well and fits a quick-flip
    task badly, and collapsing the two would hide that.
    """
    score = 1.0
    want_hold = getattr(task_like, "hold", None)
    if want_hold == "quick_flips":
        score *= hold_score(sc.hold_p50_s, sc.hold_p10_s)
    elif want_hold == "hours" and sc.hold_p50_s is not None:
        score *= _clamp(_ramp(sc.hold_p50_s, HOLD_FLOOR_S, HOLD_CEILING_S))

    want_cat = getattr(task_like, "category", None)
    if want_cat:
        if sc.top_category == want_cat:
            score *= 0.7 + 0.3 * _clamp(sc.category_concentration or 0.0)
        else:
            score *= 0.5
    if getattr(task_like, "risk", None) == "conservative":
        score *= 1.0 - 0.5 * _ramp(sc.max_drawdown, 0.0, 0.60)
    return _clamp(score)
