"""What kind of trader a wallet is.

`skill.py` measures whether a wallet is good and `replay.py` measures whether it is copyable.
Neither asks what it is *doing*. That gap shows up the moment a paper run ends: PnL attributes to
an address and stops there, and "0xab.. made $4" is not a finding. "Favourite-grinders made $4
across 31 positions while longshot-hunters lost $9 across 40" is, and it is the kind of statement
that survives the wallet going quiet.

The labels are rule-based over rows already in the database. That is a deliberate choice over
clustering or asking a model:

  * it is deterministic -- a wallet classifies the same way twice, so an archetype's track record
    accumulates across sessions instead of drifting with the labeller;
  * it is testable without a network, like every other pure module here;
  * it adds no dependency, which `RULES.md` I5 cares about for reasons that have nothing to do
    with taxonomy.

The cost is that it can only find shapes someone thought of. That is recorded rather than
hidden: `classify` returns every archetype's score, not only the winner, and refuses to label at
all when the top two are close.

Why the labels matter to the arithmetic rather than being decoration: `STRATEGY.md` §3 shows a
round trip costing 10% of stake at 0.50 and 0.5% at 0.95. `longshot-hunter` and
`favourite-grinder` sit at opposite ends of that curve, so an archetype is among other things a
prediction about fee drag made before a single trade is copied.

Pure: parsed rows in, numbers out. The clock and the price lookup are injected, exactly as
`replay.replay` injects `price_at`, so tests pass a dict.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Callable, Sequence

from . import skill
from .rank import _clamp, _ramp
from ..config import (ARCHETYPE_MARGIN_SCALE, MIN_ARCHETYPE_CONFIDENCE, MIN_ARCHETYPE_TRADES,
                      PRE_DRIFT_S)

# Coarse on purpose. `AGENTS.md` makes the same argument about skip reasons: these become the
# grouping key of every learned finding, so a proliferation of near-synonyms destroys the signal
# that grouping exists to produce.
ARCHETYPES = (
    "scalper",            # gone before a copier could act, however good
    "market-maker",       # edge is latency; screen.py already rejects these
    "momentum-chaser",    # buys into a move already going their way
    "fade-the-move",      # buys against one
    "longshot-hunter",    # cheap entries, many small losers, a few large winners
    "favourite-grinder",  # expensive entries, high hit rate, thin margins
    "resolution-holder",  # exits by settlement, so there is no exit to follow
    "event-specialist",   # concentrated in one category and none of the above
)
UNCLASSIFIED = "unclassified"


@dataclass
class StrategyProfile:
    address: str
    archetype: str
    confidence: float
    evidence_n: int                     # matched round trips the temporal features rest on
    scores: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        """The `trader_strategy` row: the deciding features flattened, the rest as JSON."""
        f = self.features
        return {
            "address": self.address,
            "archetype": self.archetype,
            "confidence": round(self.confidence, 4),
            "evidence_n": self.evidence_n,
            "entry_p50": f.get("entry_p50"),
            "hold_p50_s": f.get("hold_p50_s"),
            "pre_drift_p50": f.get("pre_drift_p50"),
            "resolution_frac": f.get("resolution_frac"),
            "cut_ratio": f.get("cut_ratio"),
            "sell_frac": f.get("sell_frac"),
            "cat_hhi": f.get("cat_hhi"),
            "top_category": f.get("top_category"),
            "scores_json": json.dumps(self.scores, sort_keys=True),
            "features_json": json.dumps(self.features, sort_keys=True),
        }

    def as_dict(self) -> dict:
        return asdict(self)


# --- features -------------------------------------------------------------------------------


def pre_drift(trades: Sequence[dict], price_at: Callable | None,
              window_s: int = PRE_DRIFT_S) -> float | None:
    """Median price move in the `window_s` before each buy, in price units.

    This is the momentum-versus-fade axis and the only feature that needs the price table. A
    positive median says they buy after the price has already moved their way; a negative one
    says they buy into weakness. It is computed over *buys*, not over matched round trips, so a
    wallet that never sells still has one -- which is the wallet this most needs to describe.

    A quote outside (0,1) is treated as absent rather than clamped, the same guard `replay._leg`
    uses: a price of exactly 0 or 1 in this table means settled, not cheap.
    """
    if price_at is None:
        return None
    drifts = []
    for tr in trades:
        if tr.get("side") != "BUY":
            continue
        token, price, ts = tr.get("token_id"), tr.get("price"), tr.get("ts")
        if not token or price is None or ts is None:
            continue
        before = price_at(token, int(ts) - window_s)
        if before is None or not 0 < before < 1:
            continue
        drifts.append(float(price) - float(before))
    if not drifts:
        return None
    return round(skill._pct(drifts, 50), 5)


def _token_shapes(trades: Sequence[dict]) -> tuple[float | None, float | None]:
    """(share of bought tokens never sold, share scaled into with more than one buy).

    The first separates a flipper from someone whose exit is the market resolving -- there is no
    exit to follow, so `follow_exit`, which `STRATEGY.md` §4 calls the edge being copied, never
    fires for them. The second says whether a single signal is the whole position or the first
    slice of one, which decides whether copying one fill copies their idea.
    """
    buys: dict[str, int] = {}
    first_sell: dict[str, int] = {}
    for tr in sorted(trades, key=lambda t: t.get("ts") or 0):
        token = tr.get("token_id")
        if not token:
            continue
        if tr.get("side") == "BUY":
            if token not in first_sell:
                buys[token] = buys.get(token, 0) + 1
        elif tr.get("side") == "SELL":
            first_sell.setdefault(token, 1)
    if not buys:
        return None, None
    sold = sum(1 for t in buys if t in first_sell)
    scaled = sum(1 for n in buys.values() if n > 1)
    return round(1 - sold / len(buys), 4), round(scaled / len(buys), 4)


def cut_ratio(trips: Sequence[skill.RoundTrip]) -> float | None:
    """Mean percentage gain on winning round trips over mean percentage loss on losing ones.

    Above 1 is the longshot shape: a lot of small losers paid for by a few large winners, which
    is exactly the distribution `STRATEGY.md` §4 measured (median +15%, p90 +194%) and exactly the
    one a fixed take-profit destroys. Below 1 is the grinder shape. Percentages are off the entry
    price, so a 0.10 -> 0.20 flip and a 0.80 -> 0.88 flip are compared on equal terms.
    """
    wins, losses = [], []
    for t in trips:
        if t.entry_price <= 0:
            continue
        r = (t.exit_price - t.entry_price) / t.entry_price
        (wins if r > 0 else losses).append(abs(r))
    if not wins or not losses:
        return None
    mean_loss = sum(losses) / len(losses)
    if mean_loss <= 1e-9:
        return None
    return round((sum(wins) / len(wins)) / mean_loss, 4)


def features(trades: Sequence[dict], closed: Sequence[dict] | None = None, *,
             price_at: Callable | None = None, category_of: Callable | None = None,
             cadence: dict | None = None) -> dict:
    """The feature vector. Every entry is either a number or None.

    None means "not measurable from what was given", never a default. `classify` scores a missing
    feature at 0.5, which neither credits nor penalises the wallet -- the rule `rank.components`
    already follows, and the reason a wallet with no price history does not read as a fader.

    `cadence` is the subset of `screen.Profile` that describes machine-like timing
    (`trades_per_day`, `median_gap_s`, `burst_frac`). It is passed in rather than recomputed
    because `screen.profile_wallet` already produces it during the funnel's recon pass.
    """
    trips = skill.match_round_trips(trades)
    holds = skill.hold_times(trips)
    entries = [t.entry_price for t in trips if t.entry_price > 0]
    n = len(trades)
    sells = sum(1 for t in trades if t.get("side") == "SELL")
    resolution_frac, scale_in_frac = _token_shapes(trades)
    top_cat, hhi = skill.category_mix(closed or [], category_of)
    cadence = cadence or {}

    feat = {
        "n_trades": n,
        "n_round_trips": len(trips),
        "sell_frac": round(sells / n, 4) if n else None,
        "entry_p10": round(skill._pct(entries, 10), 4) if entries else None,
        "entry_p50": round(skill._pct(entries, 50), 4) if entries else None,
        "entry_p90": round(skill._pct(entries, 90), 4) if entries else None,
        "hold_p10_s": holds.get("p10"),
        "hold_p50_s": holds.get("p50"),
        "hold_p90_s": holds.get("p90"),
        "pre_drift_p50": pre_drift(trades, price_at),
        "resolution_frac": resolution_frac,
        "scale_in_frac": scale_in_frac,
        "cut_ratio": cut_ratio(trips),
        "trip_win_rate": (round(sum(1 for t in trips if t.exit_price > t.entry_price) / len(trips),
                                4) if trips else None),
        "cat_hhi": round(hhi, 4) if hhi is not None else None,
        "top_category": top_cat,
        "trades_per_day": cadence.get("trades_per_day"),
        "median_gap_s": cadence.get("median_gap_s"),
        "burst_frac": cadence.get("burst_frac"),
    }
    return feat


# --- classification -------------------------------------------------------------------------


def _mean(parts: Sequence[float]) -> float:
    return _clamp(sum(parts) / len(parts)) if parts else 0.5


def _gated(f: dict, required: Sequence[str], parts: Sequence[float]) -> float:
    """A score, or a flat 0.5 when the feature that defines the archetype is missing.

    Without this, an archetype inherits a high score from whichever components it happens to
    share with another. `momentum-chaser` and `fade-the-move` are the case that forced it: they
    are mirror images that differ only in the sign of `pre_drift_p50`, so a wallet with no price
    history scored both identically and highly off their two shared terms, tying at the top and
    burying whatever the wallet actually was. Claiming a wallet chases momentum when the drift
    was never measured is not a weak claim, it is a different claim -- so it is not made.
    """
    if any(f.get(k) is None for k in required):
        return 0.5
    return _mean(parts)


def _scores(f: dict) -> dict[str, float]:
    """One score per archetype, each in [0,1] with 0.5 meaning 'nothing to say'.

    Every threshold pair below is a `_ramp(x, bad, good)` reused from `rank.py`, so the whole
    vector stays on one scale. Where a ramp reads backwards (`_ramp(hold, 300, 60)`) it is
    deliberate: shorter is more scalper-like.
    """
    s = {
        # Under two minutes is `rank.HOLD_FLOOR_S`, the point at which a trade is over before a
        # poller could have joined it. This archetype is a disqualification, not a description.
        "scalper": _gated(f, ("hold_p50_s",),
                          [_ramp(f.get("hold_p50_s"), 300, 60),
                           _ramp(f.get("hold_p10_s"), 120, 20)]),
        # The signature screen.py already rejects: sub-minute spacing, bursts, machine cadence.
        "market-maker": _gated(f, ("burst_frac", "median_gap_s"),
                               [_ramp(f.get("burst_frac"), 0.30, 0.70),
                                _ramp(f.get("median_gap_s"), 120, 20),
                                _ramp(f.get("trades_per_day"), 40, 200)]),
        "momentum-chaser": _gated(f, ("pre_drift_p50",),
                                  [_ramp(f.get("pre_drift_p50"), 0.0, 0.05),
                                   _ramp(f.get("hold_p50_s"), 7200, 600),
                                   _ramp(f.get("sell_frac"), 0.20, 0.45)]),
        "fade-the-move": _gated(f, ("pre_drift_p50",),
                                [_ramp(f.get("pre_drift_p50"), 0.0, -0.05),
                                 _ramp(f.get("hold_p50_s"), 7200, 600),
                                 _ramp(f.get("sell_frac"), 0.20, 0.45)]),
        "longshot-hunter": _gated(f, ("entry_p50",),
                                  [_ramp(f.get("entry_p50"), 0.40, 0.12),
                                   _ramp(f.get("cut_ratio"), 1.0, 3.0)]),
        "favourite-grinder": _gated(f, ("entry_p50",),
                                    [_ramp(f.get("entry_p50"), 0.55, 0.85),
                                     _ramp(f.get("trip_win_rate"), 0.50, 0.85),
                                     _ramp(f.get("cut_ratio"), 1.0, 0.4)]),
        "resolution-holder": _gated(f, ("resolution_frac",),
                                    [_ramp(f.get("resolution_frac"), 0.25, 0.75),
                                     _ramp(f.get("sell_frac"), 0.45, 0.10)]),
    }
    # Computed last and deliberately: category concentration is a fact about *where* a wallet
    # trades, not about how, so it should only win when nothing describes the how. The residual
    # term makes that explicit rather than leaving it to threshold luck.
    s["event-specialist"] = _gated(f, ("cat_hhi",),
                                   [_ramp(f.get("cat_hhi"), 0.35, 0.85),
                                    1.0 - max(s.values())])
    return {k: round(v, 4) for k, v in s.items()}


def classify(f: dict) -> tuple[str, float, dict[str, float]]:
    """(archetype, confidence, every score).

    Confidence is the margin over the runner-up, scaled by `ARCHETYPE_MARGIN_SCALE` and then
    shrunk by how much evidence there is -- the same shrink `rank.brier_score` applies, and for
    the same reason: a decisive-looking margin over eleven trades is not decisive.

    A wallet under the trade floor, or whose top two archetypes are too close, comes back
    `unclassified` rather than being given the winner. An honest refusal to label is worth more
    here than a coin flip wearing a verdict's clothes, because everything downstream groups by
    this string.
    """
    scores = _scores(f)
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    best, runner_up = ordered[0], ordered[1]
    n_trades = f.get("n_trades") or 0
    if n_trades < MIN_ARCHETYPE_TRADES:
        return UNCLASSIFIED, 0.0, scores
    shrink = min(1.0, n_trades / MIN_ARCHETYPE_TRADES)
    confidence = _clamp((best[1] - runner_up[1]) / ARCHETYPE_MARGIN_SCALE) * shrink
    if confidence < MIN_ARCHETYPE_CONFIDENCE:
        return UNCLASSIFIED, round(confidence, 4), scores
    return best[0], round(confidence, 4), scores


def profile(address: str, trades: Sequence[dict], closed: Sequence[dict] | None = None, *,
            price_at: Callable | None = None, category_of: Callable | None = None,
            cadence: dict | None = None) -> StrategyProfile:
    f = features(trades, closed, price_at=price_at, category_of=category_of, cadence=cadence)
    archetype, confidence, scores = classify(f)
    return StrategyProfile(address=address.lower(), archetype=archetype, confidence=confidence,
                           evidence_n=f["n_round_trips"], scores=scores, features=f)


# --- presentation ---------------------------------------------------------------------------


def describe(archetype: str) -> str:
    """One line on what the label means for copying, not for admiring."""
    return {
        "scalper": "flips inside a poll interval -- over before a copier could join",
        "market-maker": "edge is latency, not judgement; gone by the time the fill is visible",
        "momentum-chaser": "buys into a move already underway; a late copy buys the move, not it",
        "fade-the-move": "buys weakness; a late copy gets a better price, not a worse one",
        "longshot-hunter": "cheap entries, small fees, a few large winners paying for many losers",
        "favourite-grinder": "expensive entries, high hit rate, thin margins the fee eats first",
        "resolution-holder": "exits by settlement, so there is no exit to follow out",
        "event-specialist": "concentrated in one category; worth copying there and nowhere else",
        UNCLASSIFIED: "no shape stood out far enough from the next one to name",
    }.get(archetype, "")


def format_profile(p: StrategyProfile) -> str:
    f = p.features
    lines = [f"strategy   {p.archetype}  (confidence {p.confidence:.2f})",
             f"           {describe(p.archetype)}",
             f"  evidence {f.get('n_trades', 0)} trades, {p.evidence_n} matched round trips"]

    def row(label: str, val, fmt: str = "{:.3f}") -> None:
        if val is not None:
            lines.append(f"  {label:<16}{fmt.format(val)}")

    row("entry p50", f.get("entry_p50"))
    row("hold p50", f.get("hold_p50_s"), "{:.0f}s")
    row("pre-entry drift", f.get("pre_drift_p50"), "{:+.4f}")
    row("held to settle", f.get("resolution_frac"))
    row("cut ratio", f.get("cut_ratio"))
    row("category hhi", f.get("cat_hhi"))
    runners = sorted(p.scores.items(), key=lambda kv: -kv[1])[:3]
    lines.append("  scores          " + ", ".join(f"{k} {v:.2f}" for k, v in runners))
    return "\n".join(lines)
