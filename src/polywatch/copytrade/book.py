"""Order-book arithmetic: what a given amount of money actually buys, and at what price.

Polymarket has no slippage parameter, because it is not an AMM. It is a central limit order
book, and an order that asks for more size than the top level holds eats down into worse
levels. There is no server-side setting that prevents that -- the only defence is to walk the
levels yourself, work out the volume-weighted price the order would really pay, and send a
limit price that refuses anything materially worse.

That is the whole content of this module: `walk` answers "what would this cost", `limit_price`
turns the answer into a price the exchange will accept.

The backtest does not use these. Historical order books are not retrievable -- /book returns
the book now, never the book at 3pm four months ago -- so `replay.py` models the fill from the
target's own fill price instead. This module exists for the live poller, and is written and
tested now so that the live path is not the first place its arithmetic is exercised.
"""

from __future__ import annotations

import math

from ..config import TICK_SIZE_FALLBACK, TICK_SIZES


def walk(levels: list[tuple[float, float]], usd: float) -> tuple[float, float]:
    """Spend `usd` against `levels`, returning (shares, volume-weighted average price).

    `levels` must be best-price-first, which is exactly what `records.parse_book` guarantees
    for both sides. Returns (0.0, 0.0) if the money buys nothing, and stops early -- with a
    partial fill -- when the book runs out of depth. A partial is not an error here: it is the
    honest answer, and the caller decides whether to accept it.
    """
    if usd <= 0:
        return 0.0, 0.0
    remaining = usd
    shares = 0.0
    spent = 0.0
    for price, size in levels:
        if price <= 0 or remaining <= 0:
            break
        level_cost = price * size
        take_cost = min(remaining, level_cost)
        shares += take_cost / price
        spent += take_cost
        remaining -= take_cost
    if shares <= 0:
        return 0.0, 0.0
    return shares, spent / shares


def walk_shares(levels: list[tuple[float, float]], shares: float) -> tuple[float, float]:
    """The same walk in the other unit: sell `shares` into the book, returning (proceeds, vwap).

    Exits are sized in shares, not dollars -- we hold a position and want out of all of it --
    so the sell side needs its own entry point rather than a guess at the dollar equivalent.
    """
    if shares <= 0:
        return 0.0, 0.0
    remaining = shares
    proceeds = 0.0
    filled = 0.0
    for price, size in levels:
        if remaining <= 0:
            break
        take = min(remaining, size)
        proceeds += take * price
        filled += take
        remaining -= take
    if filled <= 0:
        return 0.0, 0.0
    return proceeds, proceeds / filled


def round_to_tick(price: float, tick: float, *, up: bool) -> float:
    """Round to a valid price increment. An off-tick order is rejected outright.

    Direction matters and is not a detail: a buy limit rounded down and a sell limit rounded up
    both move against us, so each is rounded in the direction that keeps the order fillable.
    """
    tick = tick or TICK_SIZE_FALLBACK
    steps = price / tick
    steps = math.ceil(steps - 1e-9) if up else math.floor(steps + 1e-9)
    # Prices live strictly inside (0, 1); an order at 0 or 1 is not a trade.
    return min(max(steps * tick, tick), 1.0 - tick)


def limit_price(vwap: float, slippage: float, tick: float = TICK_SIZE_FALLBACK,
                side: str = "BUY") -> float:
    """The worst price we will accept, given the book's own VWAP.

    A BUY tolerates paying `slippage` more, a SELL tolerates receiving `slippage` less. The
    result is tick-rounded outward so that rounding never quietly tightens the tolerance we
    just chose.
    """
    if side.upper() == "BUY":
        return round_to_tick(vwap * (1.0 + slippage), tick, up=True)
    return round_to_tick(vwap * (1.0 - slippage), tick, up=False)


def nearest_tick_size(value: float | None) -> float:
    """Coerce a market's reported tick to one of the four Polymarket actually uses."""
    if value is None:
        return TICK_SIZE_FALLBACK
    return min(TICK_SIZES, key=lambda t: abs(t - float(value)))
