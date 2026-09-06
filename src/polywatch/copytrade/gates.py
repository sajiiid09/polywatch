"""The filters between seeing a trade and copying it.

Every gate returns its own name when it rejects, and that name is written to `signals.reason`,
which `store.skip_reasons` aggregates into a histogram. That histogram is the primary output of
a run: "we saw 400 trades and copied 12" is only useful alongside "and here is what stopped the
other 388". A skip with no reason is a bug, not a result.

Order matters. The cheapest and most categorical checks run first, so the reason a signal was
dropped is the most fundamental one that applied rather than whichever happened to be tested
first.

The gates run on entries. Exits are not gated -- once we hold a position, getting out is not a
decision the risk settings get a vote on.
"""

from __future__ import annotations

from ..db import store


def check(con, run_id: int, task, sig: dict, market, extra=()) -> tuple[bool, str | None]:
    """(ok, reason). `sig` is a parse_activity-shaped dict; `market` a markets row or None.

    `extra` carries the gates that only one driver can ask. The replay adds
    `market_is_settleable`; the live poller will add `market_accepting_orders`. Neither belongs
    in the shared list, because each is meaningless -- and actively wrong -- for the other.
    """
    for gate in (_is_a_trade, _is_a_buy, _price_is_sane, _market_is_known, _price_in_band,
                 _room_for_another_position, *extra):
        reason = gate(con, run_id, task, sig, market)
        if reason:
            return False, reason
    return True, None


def _is_a_trade(con, run_id, task, sig, market):
    # REDEEM, MERGE, SPLIT and friends move a position without being a trade we can mirror.
    if (sig.get("kind") or "TRADE").upper() != "TRADE":
        return f"not_a_trade:{(sig.get('kind') or '').lower()}"
    return None


def _is_a_buy(con, run_id, task, sig, market):
    if (sig.get("side") or "").upper() != "BUY":
        return "not_an_entry"
    return None


def _price_is_sane(con, run_id, task, sig, market):
    price = sig.get("price")
    if price is None or not 0.0 < price < 1.0:
        return "bad_price"
    return None


def _market_is_known(con, run_id, task, sig, market):
    # Copying into a market we have no record of means sizing and fees would both be guesses.
    if market is None:
        return "market_unknown"
    return None


def replay_can_settle(last_trade: dict):
    """Replay-only gate: refuse trades whose settlement this database cannot place in time.

    Not a rule about trading -- a rule about evidence, and it has to be an entry gate rather
    than a bookkeeping detail. A position we cannot date has to be held to the end of the run,
    and holding it occupies a `max_concurrent` slot for months. Ten such positions and the
    backtest stops trading entirely: measured on the wallet used to develop this, 12.9% of
    entries were undatable and they silently blocked 7,682 of the remaining 7,759 signals.

    Two things must be true. The market resolved and we know which outcome won; and the
    estimated close -- the later of gamma's scheduled `end_ts` and the last fill anyone in our
    data made in that market -- falls after the entry. The second is what keeps a position from
    settling at the instant it opens, which is look-ahead and reliably produces nonsense.
    """
    def gate(con, run_id, task, sig, market):
        info = store.settlement(con, sig["token_id"])
        if info is None:
            return "token_unknown"
        if not info["resolved"] or info["won"] is None:
            return "market_never_resolved_in_data"
        if not info["end_ts"]:
            return "market_has_no_end_date"
        est = max(int(info["end_ts"]), last_trade.get(sig["condition_id"], 0))
        if est <= sig["ts"]:
            return "settlement_undatable"
        return None
    return gate


def market_accepting_orders(con, run_id, task, sig, market):
    """Live-only: the exchange must currently be taking orders on this market.

    Kept out of the shared list on purpose. `accepting_orders` records whether a market takes
    orders *now*, so over resolved history it is false for every row -- as a shared gate it
    would reject 100% of a backtest's signals and produce a run that never trades.

    Its natural-looking cousin, "reject any fill at or after `markets.end_ts`", is worse than
    useless and was tried first: gamma's `endDate` is the market's *scheduled* end, not its
    resolution, and in-play markets keep trading straight through it. On the wallet used to
    develop this, 51.5% of buys landed after `end_ts` -- so that gate silently threw away half
    the trader's record as "market already ended" while the market was demonstrably still
    trading. The target having filled an order is itself the proof the book was open.
    """
    if market is not None and market["accepting_orders"] in (0, False):
        return "market_not_accepting_orders"
    return None


def _price_in_band(con, run_id, task, sig, market):
    """Style and risk expressed as an entry-price band -- see task.STYLE_BANDS."""
    lo, hi = task.price_band()
    price = sig["price"]
    if price < lo:
        return f"price_below_{task.style}_band"
    if price > hi:
        return f"price_above_{task.style}_band"
    return None


def _room_for_another_position(con, run_id, task, sig, market):
    """max_concurrent, counted on distinct open positions.

    Adding to a position we already hold does not consume a new slot, so an existing position
    in this token skips the check entirely -- otherwise a full book would block a trader from
    scaling into a market we are already committed to, which is the one case where following
    them costs no extra diversification.
    """
    if store.open_position_for(con, run_id, sig["token_id"]) is not None:
        return None
    if len(store.open_positions(con, run_id)) >= task.max_concurrent:
        return "max_concurrent_positions"
    return None
