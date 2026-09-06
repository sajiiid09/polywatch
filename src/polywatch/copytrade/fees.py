"""Polymarket's taker fee, which is the entire reason paper mode exists.

A $100 account copying trades near $0.79 -- the average entry of the one wallet scored so far
-- pays the fee twice on every round trip, once going in and once coming out. At a ~4% rate
that is real money against a bankroll this small, and it is the first thing a backtest that
ignores fees will get wrong. So the fee is computed per fill from the market's own schedule,
never assumed away.

Shape of the charge: the rate is applied to the *cheaper side* of the price. A share bought at
0.97 is charged on 0.03, one bought at 0.50 is charged on 0.50. That is why
`discover.format_score` prints the average entry price with the note that fees are smallest
near 0 and 1 -- a trader who lives at the extremes is far cheaper to copy than the same trader
at even money.

Every market we ingest carries `fee_rate`, `fee_exponent` and `fee_source`. When `fee_source`
is 'fallback' the rate was guessed from the category rather than read from the market, and the
report says how much of the total fee bill rests on those guesses -- 5,280 of 17,596 ingested
markets are in that state.
"""

from __future__ import annotations

from ..config import FEE_EXPONENT_DEFAULT, FEE_FALLBACK, FEE_FALLBACK_DEFAULT
from ..parse.records import derive_category


def _get(market, key, default=None):
    """Read a column from a sqlite3.Row, a dict, or nothing at all."""
    if market is None:
        return default
    try:
        val = market[key]
    except (KeyError, IndexError):
        return default
    return default if val is None else val


def fee_params(market) -> tuple[float, float, str]:
    """(rate, exponent, source) for one market.

    Prefers the market's own feeSchedule. Falls back to the per-category table only when the
    market never shipped one, and says so in the third element so the caller can report it.
    """
    rate = _get(market, "fee_rate")
    if rate is not None:
        return (float(rate),
                float(_get(market, "fee_exponent", FEE_EXPONENT_DEFAULT)),
                str(_get(market, "fee_source", "market")))

    category = derive_category(_get(market, "fee_type"), _get(market, "category"))
    return (FEE_FALLBACK.get(category, FEE_FALLBACK_DEFAULT), FEE_EXPONENT_DEFAULT, "fallback")


def taker_fee(shares: float, price: float, market=None) -> float:
    """Fee in USDC on one taker fill of `shares` at `price`.

    Charged on min(p, 1-p) so the extremes are cheap, raised to the market's exponent (1.0
    everywhere observed so far, but the schedule carries the field so we honour it).

    Markets with `fees_enabled` explicitly false pay nothing. A market we know nothing about is
    charged at the fallback rate rather than free -- a backtest that under-charges fees is worse
    than useless, because it flatters exactly the high-frequency copying that fees exist to
    punish.
    """
    if shares <= 0:
        return 0.0
    if _get(market, "fees_enabled", 1) in (0, False):
        return 0.0
    price = min(max(price, 0.0), 1.0)
    rate, exponent, _ = fee_params(market)
    return rate * shares * (min(price, 1.0 - price) ** exponent)
