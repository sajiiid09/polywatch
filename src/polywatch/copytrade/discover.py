"""Trader discovery: find wallets worth copying, and score the ones that survive.

Two stages, because the two questions have very different costs.

Stage 1 is cheap and wide. It sweeps the leaderboard across categories, time windows and both
orderings to assemble a candidate pool of several hundred wallets, then runs the existing
behavioural screen over one page of each wallet's trades. That screen already encodes the thing
that makes most of the leaderboard uncopyable: a market maker's edge is latency, and by the time
a copier sees the fill it is gone.

Stage 2 is expensive and narrow. Only the survivors get their settled positions pulled and
scored for actual skill.

The deliberate bias in stage 1 is toward *smaller* wallets. Ranking by PnL sorts by bankroll as
much as by ability, and a wallet turning $2k into $3k is both more impressive and more copyable
at a $100 bankroll than one turning $2M into $2.1M. Volume is used as a filter, never as a
ranking.

Nothing here picks a trader. It prints a ranked shortlist and stops.
"""

from __future__ import annotations

import json

from ..config import CLOSED_POSITIONS_PAGE
from ..db import store
from ..fetch import polymarket as api
from ..fetch.client import Client, FetchError
from ..parse import records
from . import skill

# Enough history to say something, cheap enough to run over thirty wallets. Twenty pages is
# 1000 settled positions, which is far more than any candidate that passed screening will have.
MAX_CLOSED_PAGES = 20


def fetch_closed(client: Client, address: str, max_pages: int = MAX_CLOSED_PAGES) -> list[dict]:
    """Every settled position for a wallet, paged out.

    The endpoint caps at 50 rows per page whatever `limit` says, so a short page means the end
    of the history rather than an error.
    """
    out: list[dict] = []
    for page in range(max_pages):
        payload = api.closed_positions(client, address, offset=page * CLOSED_POSITIONS_PAGE)
        rows = records.parse_closed_positions(payload)
        out.extend(rows)
        if len(rows) < CLOSED_POSITIONS_PAGE:
            break
    return out


def account_value(client: Client, address: str) -> float:
    """Mark-to-market value of the wallet's open positions.

    A floor on their account, not the account: it excludes idle cash. Treat it as such --
    dividing a trade's size by this number overstates how much of their book they just risked.
    """
    try:
        payload = api.portfolio_value(client, address)
    except FetchError:
        return 0.0
    if isinstance(payload, list) and payload:
        return float(payload[0].get("value") or 0.0)
    return 0.0


def score_trader(con, client: Client, address: str,
                 max_pages: int = MAX_CLOSED_PAGES) -> tuple[skill.SkillScore, float]:
    """Fetch, score and persist one wallet. Returns (score, estimated account value)."""
    address = address.lower()
    closed = fetch_closed(client, address, max_pages)
    sc = skill.score(address, closed)
    est_account = account_value(client, address)

    store.upsert_trader_score(con, {
        "address": sc.address,
        "n_closed": sc.n_closed,
        "win_rate": sc.win_rate,
        "roi": sc.roi,
        "realized_pnl": sc.realized_pnl,
        "brier": sc.brier,
        "avg_entry_price": sc.avg_entry_price,
        "avg_stake_usd": sc.avg_stake_usd,
        "max_drawdown": sc.max_drawdown,
        "consistency": sc.consistency,
        "est_account_usd": est_account,
        "top_category": None,
        "persona_fit": None,
        "rank_score": None,
        "metrics_json": json.dumps(sc.as_row()),
    })
    return sc, est_account


def format_score(sc: skill.SkillScore, est_account: float, username: str | None = None) -> str:
    """A one-wallet score card, for `polywatch trader`."""
    brier = "n/a" if sc.brier is None else f"{sc.brier:.4f} over {sc.n_brier} settled"
    calib = ""
    if sc.brier is not None:
        # 0.25 is the score for forecasting 0.5 on everything. Above it, the entry prices carry
        # no information, whatever the PnL says.
        calib = "  (better than always guessing 50/50)" if sc.brier < 0.25 else \
                "  (WORSE than always guessing 50/50)"
    lines = [
        f"  wallet            {sc.address}" + (f"  ({username})" if username else ""),
        f"  settled positions {sc.n_closed}",
        f"  win rate          {sc.win_rate:.1%}",
        f"  realized pnl      ${sc.realized_pnl:,.2f} on ${sc.invested:,.2f} staked",
        f"  roi               {sc.roi:+.1%}",
        f"  brier             {brier}{calib}",
        f"  avg entry price   ${sc.avg_entry_price:.3f}   (fees are smallest near 0 and 1)",
        f"  stake per market  ${sc.median_stake_usd:,.2f} median, ${sc.avg_stake_usd:,.2f} mean",
        f"  max drawdown      {sc.max_drawdown:.1%} of capital staked",
        f"  consistency       {sc.consistency:.1%} of months in profit",
        f"  est. account      ${est_account:,.2f}   (open positions only, excludes cash)",
    ]
    return "\n".join(lines)
