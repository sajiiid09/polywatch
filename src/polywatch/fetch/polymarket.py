"""Endpoint wrappers. Return raw JSON exactly as Polymarket sent it -- no field access here.

Every quirk encoded below was verified by probing on 2026-09-05 (see scripts/probe_apis.py and
data/raw/probe/). They are the reason this layer exists separately from parsing.
"""

from __future__ import annotations

import hashlib

from ..config import (ACTIVITY_PAGE, CLOB, CLOSED_POSITIONS_PAGE, DATA_API, GAMMA,
                      LEADERBOARD_PAGE, POSITIONS_PAGE, PRICE_FIDELITY, TRADES_PAGE)
from .client import Client


def leaderboard(client: Client, offset: int = 0, limit: int = LEADERBOARD_PAGE,
                category: str = "OVERALL", time_period: str = "ALL",
                order_by: str = "PNL") -> list:
    """A slice of the leaderboard.

    Quirks: the bare /leaderboard path 404s -- it is /v1/leaderboard. The server caps the page at
    50 rows whatever `limit` says. `offset` pages correctly, up to 1000.

    The filters are `category`, `timePeriod` and `orderBy`. An earlier probe concluded the
    leaderboard was PnL-desc-only; that was wrong -- it had guessed the names `window` and
    `rankBy`, which the server silently drops. Re-probed 2026-09-06 with the correct names and
    all three change the result set. This matters a great deal: PNL/ALL/OVERALL returns the same
    handful of whales every time, and sweeping the other combinations is the only way to reach
    the smaller, still-active wallets that are actually copyable.

    category    OVERALL POLITICS SPORTS ESPORTS CRYPTO CULTURE MENTIONS WEATHER
                ECONOMICS TECH FINANCE
    time_period DAY WEEK MONTH ALL
    order_by    PNL VOL
    """
    return client.get_json(
        f"{DATA_API}/v1/leaderboard",
        {"limit": limit, "offset": offset, "category": category,
         "timePeriod": time_period, "orderBy": order_by},
        kind="leaderboard", raw_name=f"{category}_{time_period}_{order_by}_{offset}",
    )


def trades(client: Client, user: str, offset: int = 0, limit: int = TRADES_PAGE) -> list:
    """A wallet's trades, newest first (verified timestamp-DESC), 1000 per page."""
    return client.get_json(
        f"{DATA_API}/trades", {"user": user, "limit": limit, "offset": offset},
        kind="trades", raw_name=f"{user}_{offset}",
    )


def markets_by_condition(client: Client, condition_ids: list[str], closed: bool | None = None) -> list:
    """Market metadata for a batch of condition ids.

    Two quirks that matter: repeated `condition_ids` params batch correctly, and gamma returns
    ONLY open markets unless `closed=true` is passed. Since scoring runs on resolved markets, the
    caller must sweep both values -- see ingest.fetch_markets.
    """
    params: dict = {"condition_ids": condition_ids, "limit": len(condition_ids)}
    if closed is not None:
        params["closed"] = "true" if closed else "false"
    key = hashlib.sha1(("|".join(sorted(condition_ids)) + str(closed)).encode()).hexdigest()[:16]
    return client.get_json(f"{GAMMA}/markets", params, kind="markets", raw_name=key)


def prices_history(client: Client, token_id: str, start_ts: int, end_ts: int,
                   fidelity: int = PRICE_FIDELITY) -> dict:
    """Minute-resolution price history for one CLOB token over an explicit window.

    Uses startTs/endTs rather than `interval`, because `interval=1m` rejects any fidelity under
    10 while the explicit-window form accepts fidelity=1. That difference is what makes bounded
    per-trade windows possible at minute resolution.
    """
    return client.get_json(
        f"{CLOB}/prices-history",
        {"market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity},
        kind="prices", raw_name=f"{token_id[:24]}_{start_ts}_{end_ts}",
    )


# --- Endpoints added for copy trading -----------------------------------------------------
# Verified live and unauthenticated on 2026-09-06. None of these need an API key.


def activity(client: Client, user: str, limit: int = ACTIVITY_PAGE, offset: int = 0,
             kinds: str = "TRADE", start_ts: int | None = None,
             dump_raw: bool | None = None) -> list:
    """A wallet's activity feed, newest first. The copy-trading poller's eye, and its backstop.

    This used to say it was "the sole way to watch someone else's fills", on the grounds that the
    CLOB market websocket carries no wallet address and the user websocket reports only your own
    account. Both remain true, and both are facts about Polymarket's API rather than about the
    world: the fills settle on Polygon, where the maker is an indexed topic. See
    copytrade/tradestream.py and ADR-0008. This endpoint is now the slower of two routes -- it
    carries a `max-age=15` cache and was measured 13-21s behind -- and is kept because it is the
    reconciliation source, it carries the non-TRADE event kinds, and it is what the run falls
    back to when the chain feed cannot connect.

    Richer than /trades: it also carries `usdcSize`, the market `title`/`slug`, and the
    non-TRADE event kinds (SPLIT, MERGE, REDEEM, ...) which tell us when a position left the
    book by some route other than a sell.
    """
    params: dict = {"user": user, "limit": limit, "offset": offset,
                    "type": kinds, "sortDirection": "DESC"}
    if start_ts is not None:
        params["start"] = start_ts
    return client.get_json(f"{DATA_API}/activity", params, kind="activity",
                           raw_name=f"{user}_{offset}", dump_raw=dump_raw)


def positions(client: Client, user: str, limit: int = POSITIONS_PAGE, offset: int = 0,
              size_threshold: float = 1.0) -> list:
    """A wallet's open positions, with avgPrice, cashPnl, percentPnl and entryFeesUsdc."""
    return client.get_json(
        f"{DATA_API}/positions",
        {"user": user, "limit": limit, "offset": offset, "sizeThreshold": size_threshold},
        kind="positions", raw_name=f"{user}_{offset}",
    )


def closed_positions(client: Client, user: str, limit: int = CLOSED_POSITIONS_PAGE,
                     offset: int = 0, sort_by: str = "TIMESTAMP",
                     sort_direction: str = "DESC") -> list:
    """A wallet's settled positions -- the track record, straight from the source.

    Each row carries realizedPnl, avgPrice, totalBought, outcome and curPrice (1 or 0), which is
    everything needed for win rate, ROI and a Brier score. Computing those from raw trades plus
    market resolutions would cost hundreds of extra requests per wallet and get the same answer.

    Caps at 50 rows per page whatever `limit` says, like the leaderboard; `offset` pages
    correctly.

    `sort_by` defaults to TIMESTAMP and this is not a cosmetic choice. The server's own default
    is REALIZEDPNL descending, so reading the first few pages hands you nothing but the wallet's
    best trades: probed 2026-09-06 on a real wallet, the first 200 rows were 100% winners with a
    monotonically falling PnL, and scoring them produced a 100% win rate and a 0% drawdown for a
    trader who is not remotely that good. Sorted by time the same wallet's most recent 50 rows
    are 18/50 losers. Any sampled prefix of a PnL-sorted list is a biased sample; a prefix of a
    time-sorted list is just recent form.

    Valid sort_by values, per the server's own error message: REALIZEDPNL, AVGPRICE, PRICE,
    TITLE, TIMESTAMP.
    """
    return client.get_json(
        f"{DATA_API}/closed-positions",
        {"user": user, "limit": limit, "offset": offset,
         "sortBy": sort_by, "sortDirection": sort_direction},
        kind="closed_positions", raw_name=f"{user}_{offset}",
    )


def portfolio_value(client: Client, user: str) -> list:
    """Current mark-to-market value of a wallet's open positions, as [{user, value}].

    Note this is positions only -- it does not include idle cash, so it is a floor on the
    trader's account size, not the size itself. Good enough to scale MIRROR sizing by.
    """
    return client.get_json(f"{DATA_API}/value", {"user": user},
                           kind="value", raw_name=user)


def book(client: Client, token_id: str, dump_raw: bool | None = None) -> dict:
    """Full order book for one CLOB token: {bids: [{price, size}], asks: [...]}.

    Polymarket has no slippage parameter -- it is a limit order book, not an AMM. Bounding
    slippage means walking these levels ourselves to a volume-weighted worst price. See
    copytrade/book.py.
    """
    return client.get_json(f"{CLOB}/book", {"token_id": token_id},
                           kind="book", raw_name=token_id[:24], dump_raw=dump_raw)


# --- probed and verified, not currently called ---------------------------------------------
# No caller today. Kept rather than deleted because each docstring records something that was
# established by probing the live API on a date -- the page caps, the parameter names the
# server silently drops, where the real category taxonomy lives -- and re-deriving that costs
# far more than four functions cost to carry. Anything here that gains a caller moves back up.

def trades_feed(client: Client, offset: int = 0, limit: int = TRADES_PAGE) -> list:
    """The global trades feed, no user filter. Step 5's sampling frame for control wallets."""
    return client.get_json(
        f"{DATA_API}/trades", {"limit": limit, "offset": offset},
        kind="trades_feed", raw_name=f"offset_{offset}",
    )


def traded_count(client: Client, user: str) -> dict:
    """Lifetime trade count for a wallet, as {user, traded}."""
    return client.get_json(f"{DATA_API}/traded", {"user": user},
                           kind="traded", raw_name=user)


def tick_size(client: Client, token_id: str) -> dict:
    """Minimum price increment for one token, as {minimum_tick_size: 0.001}.

    One of 0.1 / 0.01 / 0.001 / 0.0001, per market. An order priced off-tick is rejected.
    """
    return client.get_json(f"{CLOB}/tick-size", {"token_id": token_id},
                           kind="tick_size", raw_name=token_id[:24])


def market_tags(client: Client, market_id: str) -> list:
    """Gamma's tags for one market -- the real category taxonomy.

    `markets.category` is NULL for every market we have ever ingested, so this is where the
    category actually lives. It costs one request per market though, so prefer deriving the
    category from the `feeType` string we already store (`sports_fees_v3` -> sports) and fall
    back to this only when feeType is missing.
    """
    return client.get_json(f"{GAMMA}/markets/{market_id}/tags", None,
                           kind="market_tags", raw_name=str(market_id))
