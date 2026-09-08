"""What a copier would actually have captured, replayed against real price history.

Every other metric in this project measures the trader. This one measures *us copying them*,
which is a different and much less flattering number, and it is the only one that answers the
question the bot has.

The method is deliberately plain. Take the wallet's matched round trips -- a buy and the sell
that closed it. For a copy lag L, look up what the token was worth L seconds after they bought
and L seconds after they sold, charge the taker fee on both legs, and see what a fixed stake
would have made. Do that for several values of L and watch the number decay.

    lag  0s     what they made, charged our fees
    lag 15s     what one poll interval costs us
    lag 60s     what a slow poll or a stalled feed costs us
    lag 900s    where the edge is definitively gone

Three numbers come out of the curve:

  * `copier_roi` at each lag -- return per dollar staked, fees paid.
  * `capture_ratio` -- roi at the reference lag divided by roi at zero. How much of their edge
    survives being copied. A wallet with a superb record and a capture ratio of 0.05 is not a
    candidate; that is the entire point of this module.
  * `edge_half_life` -- the lag at which copier ROI reaches zero, interpolated. A trader whose
    edge is gone in thirty seconds cannot be copied by anything that polls.

Two honest limits, stated rather than buried. Price history is minute-resolution, so a lag of 15
seconds and a lag of 45 seconds often resolve to the same quote -- the curve is coarser than its
x-axis suggests, and small differences between adjacent lags mean little. And a mid-price is not
a fill: this charges fees but not spread, so every number here is an upper bound. `book_gates`
is what keeps the live bot out of the markets where that gap is widest.

Positions are equal-weighted rather than size-weighted, because a copier stakes a fixed amount
per trade regardless of what the trader staked. Weighting by their size would measure their
portfolio, which is not the thing being bought.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..config import (FEE_FALLBACK_DEFAULT, MIN_REPLAY_COVERAGE, REPLAY_LAGS,
                      REPLAY_REFERENCE_LAG, WINDOW_POST_S, WINDOW_PRE_S, WINDOW_MERGE_GAP_S)
from ..db import store
from . import book as bk
from .skill import RoundTrip, match_round_trips


@dataclass
class LagResult:
    """One point on the decay curve."""
    lag_s: int
    n: int                  # round trips that resolved to a price at both ends
    n_missing: int          # round trips with no quote at this lag -- the coverage caveat
    copier_roi: float       # per dollar staked, both taker fees charged
    copier_pnl: float       # at a $1 stake per trip, so it reads as "cents per copy"
    win_rate: float

    def as_row(self) -> dict:
        return asdict(self)


@dataclass
class ReplayResult:
    address: str
    lags: list[LagResult]
    capture_ratio: float | None
    edge_half_life_s: float | None
    n_round_trips: int
    coverage: float = 0.0        # share of round trips priced at both ends, at the reference lag

    def at(self, lag_s: int) -> LagResult | None:
        for r in self.lags:
            if r.lag_s == lag_s:
                return r
        return None

    @property
    def trustworthy(self) -> bool:
        """Is there enough price coverage for this curve to decide anything?

        Price history is fetched in windows around trades we already knew about, so a wallet
        whose exits fall outside those windows produces a curve drawn through a minority of its
        own round trips. Such a curve is not evidence against the wallet, and it is not evidence
        for it either -- so it is reported with its coverage attached and excluded from the hard
        gates in `rank.py`. Backfilling the windows (which `score_trader` does) is the fix; this
        flag is what stops a thin one being mistaken for a verdict in the meantime.
        """
        return self.coverage >= MIN_REPLAY_COVERAGE


def _leg(price_at, token_id: str, ts: int, lag: int) -> float | None:
    """The price a copier would have traded at, `lag` seconds behind the trader."""
    p = price_at(token_id, ts + lag)
    return p if p is not None and 0 < p < 1 else None


def replay_one(trip: RoundTrip, lag: int, price_at, rate: float) -> float | None:
    """What one dollar staked copying this round trip would have returned, or None if unknown.

    Both legs pay the taker fee, which is the whole reason this differs from the trader's own
    result: they may have been a maker on one or both, and in any case their entry was not ours.
    """
    entry = _leg(price_at, trip.token_id, trip.entry_ts, lag)
    exit_ = _leg(price_at, trip.token_id, trip.exit_ts, lag)
    if entry is None or exit_ is None:
        return None
    shares = 1.0 / entry                       # one dollar, at the price we would have paid
    proceeds = shares * exit_
    fees = bk.fee(shares, entry, rate) + bk.fee(shares, exit_, rate)
    return proceeds - 1.0 - fees


def replay_lag(trips: list[RoundTrip], lag: int, price_at, rate_of) -> LagResult:
    results = []
    missing = 0
    for t in trips:
        r = replay_one(t, lag, price_at, rate_of(t))
        if r is None:
            missing += 1
        else:
            results.append(r)
    if not results:
        return LagResult(lag, 0, missing, 0.0, 0.0, 0.0)
    total = sum(results)
    return LagResult(lag, len(results), missing, total / len(results), total,
                     sum(1 for r in results if r > 0) / len(results))


def edge_half_life(lags: list[LagResult]) -> float | None:
    """The lag at which copier ROI first reaches zero, linearly interpolated.

    None when the curve never crosses -- either it was under water from the start (there was
    never an edge to lose) or it is still positive at the longest lag tested, which for a slow
    trader is a genuine result rather than a missing one.
    """
    usable = [r for r in lags if r.n > 0]
    if len(usable) < 2 or usable[0].copier_roi <= 0:
        return None
    prev = usable[0]
    for cur in usable[1:]:
        if cur.copier_roi <= 0:
            span = prev.copier_roi - cur.copier_roi
            if span <= 0:
                return float(cur.lag_s)
            frac = prev.copier_roi / span
            return prev.lag_s + frac * (cur.lag_s - prev.lag_s)
        prev = cur
    return None


def replay(address: str, trades, price_at, rate_of=None,
           lags: tuple[int, ...] = REPLAY_LAGS) -> ReplayResult:
    """The whole decay curve for one wallet.

    `price_at(token_id, ts)` is `store.price_at` in production and a dict lookup in tests, which
    is why it is a parameter: the arithmetic here is worth testing without a gigabyte of prices.
    """
    rate_of = rate_of or (lambda _t: FEE_FALLBACK_DEFAULT)
    trips = match_round_trips(trades)
    rows = [replay_lag(trips, lag, price_at, rate_of) for lag in sorted(lags)]

    zero = next((r for r in rows if r.lag_s == 0 and r.n > 0), None)
    ref = next((r for r in rows if r.lag_s == REPLAY_REFERENCE_LAG and r.n > 0), None)
    capture = None
    if zero and ref and zero.copier_roi > 0:
        # Only meaningful when there was an edge at zero lag to lose. A ratio computed against a
        # negative baseline reads backwards -- getting worse would look like capturing more.
        capture = ref.copier_roi / zero.copier_roi
    priced = ref.n if ref else max((r.n for r in rows), default=0)
    coverage = (priced / len(trips)) if trips else 0.0
    return ReplayResult(address.lower(), rows, capture, edge_half_life(rows), len(trips),
                        coverage)


# --- price coverage -------------------------------------------------------------------------


def needed_windows(trips: list[RoundTrip], lags: tuple[int, ...] = REPLAY_LAGS
                   ) -> dict[str, list[tuple[int, int]]]:
    """The price spans this replay needs, per token, before any of it can be answered.

    Each leg needs a quote at or before `ts + lag`, and `store.price_at` reads backwards, so a
    window has to start before the event rather than at it. The same merge-and-subtract algebra
    that plans ingestion plans this, because it is the same problem: fetch the union of what is
    wanted, minus what is already on disk.
    """
    from ..ingest import merge_intervals
    wanted: dict[str, list[tuple[int, int]]] = {}
    span = max(lags) + WINDOW_POST_S
    for t in trips:
        for ts in (t.entry_ts, t.exit_ts):
            wanted.setdefault(t.token_id, []).append((ts - WINDOW_PRE_S, ts + span))
    return {k: merge_intervals(v, gap=WINDOW_MERGE_GAP_S) for k, v in wanted.items()}


def plan_backfill(con, trips: list[RoundTrip], lags: tuple[int, ...] = REPLAY_LAGS
                  ) -> list[tuple[int, int, str]]:
    """Windows that must still be fetched: what replay needs, minus what has been fetched."""
    from ..ingest import chunk, subtract
    covered = store.existing_windows(con)
    out: list[tuple[int, int, str]] = []
    for token_id, spans in sorted(needed_windows(trips, lags).items()):
        for start_ts, end_ts in chunk(subtract(spans, covered.get(token_id, []))):
            out.append((start_ts, end_ts, token_id))
    return out


def backfill(con, client, trips: list[RoundTrip], lags: tuple[int, ...] = REPLAY_LAGS,
             log=print) -> int:
    """Fetch the missing price windows. Returns points stored.

    Deliberately serial and deliberately resumable: `price_windows` records every span asked
    for, including the empty ones, so a run interrupted halfway costs nothing to repeat.
    """
    from ..fetch import polymarket as api
    from ..fetch.client import FetchError
    from ..parse import records

    todo = plan_backfill(con, trips, lags)
    if not todo:
        return 0
    log(f"    backfilling {len(todo)} price window(s)")
    points = 0
    for start_ts, end_ts, token_id in todo:
        try:
            payload = api.prices_history(client, token_id, start_ts, end_ts)
        except FetchError as e:
            log(f"    ! prices {token_id[:16]}: {e}")
            continue
        pts = records.parse_price_history(payload)
        points += store.insert_prices(con, token_id, pts)
        store.record_window(con, token_id, start_ts, end_ts, len(pts))
    return points


def format_curve(res: ReplayResult) -> str:
    """The decay curve, for a score card."""
    if not res.n_round_trips:
        return "  copyability       no matched round trips to replay"
    lines = [f"  copyability       replayed over {res.n_round_trips} matched round trips",
             "    lag      copier roi    win rate   trips"]
    for r in res.lags:
        if r.n == 0:
            lines.append(f"    {r.lag_s:>4}s          no price coverage")
            continue
        lines.append(f"    {r.lag_s:>4}s      {r.copier_roi:+8.2%}     {r.win_rate:6.1%}  "
                     f"{r.n:>5}" + (f"  ({r.n_missing} unpriced)" if r.n_missing else ""))
    if res.capture_ratio is not None:
        lines.append(f"    capture ratio  {res.capture_ratio:.2f}  "
                     f"(share of their edge surviving {REPLAY_REFERENCE_LAG}s)")
    if res.edge_half_life_s is not None:
        lines.append(f"    edge half-life {res.edge_half_life_s:,.0f}s  "
                     f"(copier roi reaches zero)")
    lines.append(f"    coverage       {res.coverage:.0%} of round trips priced at both ends")
    if not res.trustworthy:
        lines.append("                   -- too thin to judge on. This curve is drawn through a")
        lines.append("                      minority of their trades; it is neither evidence")
        lines.append("                      for the wallet nor against it. Re-run to backfill.")
    if res.capture_ratio is not None and res.capture_ratio > 1.0:
        lines.append("                   -- capture above 1.0 means a copier did better than")
        lines.append("                      the trader, which is minute-resolution noise rather")
        lines.append("                      than an edge. Read it as 'no measurable decay'.")
    return "\n".join(lines)
