"""Order-book arithmetic: what a given order would actually cost, and what price to sign.

Polymarket is a central limit order book, not an AMM. There is no slippage parameter to pass
and no pool curve to solve -- the only way to know what $20 buys is to walk the levels. Every
function here is pure: it takes a parsed book from parse.records.parse_book and returns numbers.

Two conventions used throughout:
  * `price` is always the price of the token being traded, 0..1.
  * Buying consumes asks (ascending), selling consumes bids (descending). parse_book has
    already sorted both sides best-first, so both walks are the same loop.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import (FEE_EXPONENT_DEFAULT, FEE_FALLBACK, FEE_FALLBACK_DEFAULT,
                      MIN_ORDER_SHARES_FALLBACK, TICK_SIZE_FALLBACK)


@dataclass
class Walk:
    """The result of consuming a book to a target size."""
    shares: float        # how many shares the book can actually supply
    cost: float          # USDC across those shares (proceeds, on a sell)
    vwap: float          # cost / shares
    worst_price: float   # the last level touched -- the price a limit order must reach
    exhausted: bool      # True when the book ran out before the target was met

    @property
    def filled(self) -> bool:
        return self.shares > 0


def _walk(levels: list[tuple[float, float]], *, shares: float | None = None,
          usd: float | None = None) -> Walk:
    got = 0.0
    spent = 0.0
    worst = 0.0
    for price, size in levels:
        if price <= 0 or size <= 0:
            continue
        if shares is not None:
            take = min(size, shares - got)
        else:
            take = min(size, (usd - spent) / price)
        if take <= 0:
            break
        got += take
        spent += take * price
        worst = price
        if shares is not None and got >= shares - 1e-9:
            return Walk(got, spent, spent / got, worst, False)
        if usd is not None and spent >= usd - 1e-9:
            return Walk(got, spent, spent / got, worst, False)
    if got <= 0:
        return Walk(0.0, 0.0, 0.0, 0.0, True)
    return Walk(got, spent, spent / got, worst, True)


def buy_for_usd(book: dict, usd: float) -> Walk:
    """What `usd` spent at market gets you, walking the asks."""
    return _walk(book.get("asks") or [], usd=usd)


def buy_shares(book: dict, shares: float) -> Walk:
    return _walk(book.get("asks") or [], shares=shares)


def sell_shares(book: dict, shares: float) -> Walk:
    """What selling `shares` at market raises, walking the bids."""
    return _walk(book.get("bids") or [], shares=shares)


def best_bid(book: dict) -> float | None:
    bids = book.get("bids") or []
    return bids[0][0] if bids else None


def best_ask(book: dict) -> float | None:
    asks = book.get("asks") or []
    return asks[0][0] if asks else None


def mid(book: dict) -> float | None:
    b, a = best_bid(book), best_ask(book)
    if b is None or a is None:
        return None
    return (b + a) / 2


def spread(book: dict) -> float | None:
    b, a = best_bid(book), best_ask(book)
    if b is None or a is None:
        return None
    return a - b


def round_to_tick(price: float, tick: float = TICK_SIZE_FALLBACK, *, side: str = "BUY") -> float:
    """Snap a price to the market's tick. An off-tick order is rejected outright.

    Rounding direction is not cosmetic. A BUY limit rounded down can sit below the level it was
    meant to reach and never fill; a SELL limit rounded up does the same. Both round in the
    direction that preserves the intent -- pay one tick more, accept one tick less -- and the
    result is clamped inside (0, 1) because 0 and 1 are not tradeable prices.
    """
    if tick <= 0:
        tick = TICK_SIZE_FALLBACK
    steps = price / tick
    n = int(steps) + (1 if side.upper() == "BUY" and steps % 1 else 0)
    out = round(n * tick, 6)
    return max(tick, min(1.0 - tick, out))


def limit_price(reference: float, slippage: float, tick: float = TICK_SIZE_FALLBACK,
                side: str = "BUY") -> float:
    """The price to actually sign: the touch plus a slippage allowance, on-tick.

    A copy order is a taker order racing whoever else saw the same fill, so it is priced through
    the book rather than at it. `slippage` is fractional: 0.07 accepts paying 7% more per share
    than the reference, and refusing anything worse.

    `reference` is the **touch** -- the best ask when buying, the best bid when selling -- and
    not the VWAP of our own walk. The distinction matters on a thin book. The walk's VWAP already
    includes the impact of our own size, so allowing 7% on top of it permits impact *and* 7%,
    which on a book with two levels can be far worse than the 7% the operator asked for. Priced
    off the touch, the allowance means what it says, and an order too large for the book fills
    partially instead of paying through it -- which is the outcome we would have chosen anyway.
    """
    adj = reference * (1 + slippage) if side.upper() == "BUY" else reference * (1 - slippage)
    return round_to_tick(adj, tick, side=side)


def spread_frac(book: dict) -> float | None:
    """The spread as a fraction of the mid -- what crossing it costs, in the same unit as fees.

    Directly comparable to `round_trip_fee_frac`, and for the same reason: both are costs a round
    trip pays before the trade is right about anything. A 1-cent spread is 2% of the price at
    0.50 and 20% of it at 0.05, which is why this is a fraction rather than an absolute.
    """
    b, a = best_bid(book), best_ask(book)
    if b is None or a is None:
        return None
    m = (a + b) / 2
    return (a - b) / m if m > 0 else None


def fee(shares: float, price: float, rate: float, exponent: float = FEE_EXPONENT_DEFAULT) -> float:
    """Polymarket's taker fee for one fill.

    The fee is proportional to min(price, 1-price), not to notional: it is largest at 0.50 and
    vanishes towards either end. That single fact drives most of the exit ladder -- a 3-cent
    move on a 0.50 contract can be entirely eaten by the round trip, while the same move at
    0.90 is nearly all profit.
    """
    if shares <= 0 or not (0 < price < 1):
        return 0.0
    return rate * ((min(price, 1 - price)) ** exponent) * shares


def fee_rate_for(category: str | None, market_rate: float | None = None) -> float:
    """The market's own rate when it published one, otherwise the category fallback."""
    if market_rate is not None:
        return market_rate
    return FEE_FALLBACK.get((category or "").lower(), FEE_FALLBACK_DEFAULT)


def meets_min_size(shares: float, min_shares: float = MIN_ORDER_SHARES_FALLBACK) -> bool:
    return shares >= min_shares - 1e-9


def net_exit_value(book: dict, shares: float, rate: float,
                   exponent: float = FEE_EXPONENT_DEFAULT) -> tuple[float, float, float]:
    """What closing a position right now would actually net.

    Returns (proceeds, fee, net). This -- not the mid price, and not the last trade -- is what
    every exit rung is evaluated against, because it is the only number that includes both the
    depth of the bid side and the fee on the way out.
    """
    w = sell_shares(book, shares)
    f = fee(w.shares, w.vwap, rate, exponent) if w.filled else 0.0
    return w.cost, f, w.cost - f


def round_trip_fee_frac(price: float, rate: float,
                        exponent: float = FEE_EXPONENT_DEFAULT) -> float:
    """Both taker fees on a round trip, as a fraction of the stake.

    This is the number that decides whether a copy can make money at all, and it is not
    intuitive: the fee is proportional to min(p, 1-p) but the shares a fixed stake buys are
    proportional to 1/p, so the cost of a round trip runs from 10% of stake at even odds down
    to under 1% near the extremes. A 5% take-profit at 0.50 is arithmetically incapable of
    clearing it -- which is a property of the market, not of the trader being copied.

    Both legs are priced at the entry, so this is the fee on a flat round trip. A winning exit
    that moves toward 0.50 pays slightly more and one that moves away pays slightly less; the
    error is second-order next to the decision it informs.
    """
    if not (0 < price < 1) or rate <= 0:
        return 0.0
    return 2 * rate * (min(price, 1 - price) ** exponent) / price


def min_viable_target(price: float, rate: float, margin: float = 0.0,
                      exponent: float = FEE_EXPONENT_DEFAULT) -> float:
    """The smallest take-profit that clears both fees, plus whatever margin is wanted."""
    return round_trip_fee_frac(price, rate, exponent) + margin
