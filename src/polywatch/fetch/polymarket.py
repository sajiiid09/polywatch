"""Endpoint wrappers. Return raw JSON exactly as Polymarket sent it -- no field access here.

Every quirk encoded below was verified by probing on 2026-09-05 (see scripts/probe_apis.py and
data/raw/probe/). They are the reason this layer exists separately from parsing.
"""

from __future__ import annotations

import hashlib

from ..config import CLOB, DATA_API, GAMMA, LEADERBOARD_PAGE, PRICE_FIDELITY, TRADES_PAGE
from .client import Client


def leaderboard(client: Client, offset: int = 0, limit: int = LEADERBOARD_PAGE) -> list:
    """Top wallets by PnL.

    Quirks: the bare /leaderboard path 404s -- it is /v1/leaderboard. The server caps the page at
    50 rows whatever `limit` says, and ignores `window` and `rankBy` entirely, so PnL-desc is the
    only ranking available. `offset` does page correctly.
    """
    return client.get_json(
        f"{DATA_API}/v1/leaderboard", {"limit": limit, "offset": offset},
        kind="leaderboard", raw_name=f"offset_{offset}",
    )


def trades(client: Client, user: str, offset: int = 0, limit: int = TRADES_PAGE) -> list:
    """A wallet's trades, newest first (verified timestamp-DESC), 1000 per page."""
    return client.get_json(
        f"{DATA_API}/trades", {"user": user, "limit": limit, "offset": offset},
        kind="trades", raw_name=f"{user}_{offset}",
    )


def trades_feed(client: Client, offset: int = 0, limit: int = TRADES_PAGE) -> list:
    """The global trades feed, no user filter. Step 5's sampling frame for control wallets."""
    return client.get_json(
        f"{DATA_API}/trades", {"limit": limit, "offset": offset},
        kind="trades_feed", raw_name=f"offset_{offset}",
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
