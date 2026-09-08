"""How many dollars to put behind a copied trade.

Two methods, and the difference between them is what they assume the number means.

`fixed` stakes the same amount every time. It ignores the target's conviction entirely, which
sounds crude and is in fact the safer default at a $100 bankroll: it makes the run's exposure
predictable and its results comparable across traders.

`mirror` scales their stake to our account, so a trade they sized at 2% of their book becomes
2% of ours. It is the more faithful copy and the more fragile one, because the denominator --
their account size -- is not something we can measure. /value reports the mark-to-market of
their *open positions* and excludes idle cash, so it is a floor on their account, and every
fraction derived from it is therefore an overstatement. `MIRROR_MAX_FRACTION` is the guard: a
trader sitting on 90% cash would otherwise make one ordinary trade look like a full-bankroll
conviction bet.

Sizing returns a reason string when it returns nothing, so that "we could not afford it" lands
in the skip histogram beside the gates rather than vanishing.
"""

from __future__ import annotations

from ..config import MIN_ORDER_SHARES_FALLBACK, MIRROR_MAX_FRACTION
from ..db import store


def size_usd(con, run_id: int, task, sig: dict, market,
             trader_account: float | None = None) -> tuple[float, str | None]:
    """(usd, reason). usd is 0 whenever reason is set."""
    want = _intent(task, sig, trader_account)
    if want <= 0:
        return 0.0, "mirror_size_unknown" if task.buy_method == "mirror" else "size_zero"

    # Per-market ceiling, measured across every outcome of the market, not per token: holding
    # YES and NO of the same question is two positions but one question's worth of risk.
    exposure = store.market_exposure(con, run_id, sig["condition_id"])
    room = task.max_market_usd - exposure
    if room <= 0:
        return 0.0, "market_cap_reached"
    want = min(want, room)

    cash = store.run_cash(con, run_id)
    if cash <= 0:
        return 0.0, "no_cash"
    want = min(want, cash)

    # Polymarket rejects orders below the market's minimum share count, so a stake that cannot
    # clear it is not a small trade -- it is not a trade.
    min_shares = _min_shares(market)
    price = sig["price"]
    if want < min_shares * price:
        return 0.0, "below_min_order_size"

    return want, None


def _intent(task, sig: dict, trader_account: float | None) -> float:
    if task.buy_method == "fixed":
        return float(task.fixed_usd or 0.0)

    theirs = sig.get("usdc_size") or (sig.get("size") or 0.0) * (sig.get("price") or 0.0)
    if not theirs or not trader_account or trader_account <= 0:
        return 0.0
    fraction = min(theirs / trader_account, MIRROR_MAX_FRACTION)
    return fraction * task.bankroll


def _min_shares(market) -> float:
    if market is None:
        return MIN_ORDER_SHARES_FALLBACK
    try:
        val = market["order_min_size"]
    except (KeyError, IndexError):
        val = None
    return float(val) if val else MIN_ORDER_SHARES_FALLBACK
