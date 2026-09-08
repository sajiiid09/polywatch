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

from ..config import (CLOSED_POSITIONS_PAGE, LEADERBOARD_CATEGORIES,
                      LEADERBOARD_ORDERINGS, LEADERBOARD_PAGE, LEADERBOARD_PERIODS,
                      RANK_SAMPLE_TARGET, SWEEP_PAGES_PER_COMBO)
from ..db import store
from ..fetch import polymarket as api
from ..fetch.client import Client, FetchError
from ..parse import records
from . import skill

# Enough history to say something, cheap enough to run over thirty wallets. Twenty pages is
# 1000 settled positions, which is far more than any candidate that passed screening will have.
MAX_CLOSED_PAGES = 20


def sweep_leaderboard(con, client: Client, pages: int = SWEEP_PAGES_PER_COMBO,
                     categories=LEADERBOARD_CATEGORIES, periods=LEADERBOARD_PERIODS,
                     orderings=LEADERBOARD_ORDERINGS, on_progress=None) -> list[str]:
    """Stage 1: assemble the candidate pool from every corner of the leaderboard.

    The default OVERALL / ALL / PNL call that `ingest.ingest_wallets` makes returns the same
    handful of whales every time -- wallets whose rank is a function of bankroll rather than
    ability, and whose size makes them uncopyable at $100. Sweeping the other combinations is
    the only way to reach the smaller, still-active wallets that are worth following.

    The DAY and WEEK periods matter most here: they are what surface a wallet that is trading
    well *now*, as opposed to one coasting on a record set in March.

    Deduped by address, so a wallet appearing in six categories costs one row. The rank and
    volume kept are whichever the last combination reported -- neither is used for ranking
    (see the module docstring), only for the screen's volume cap.
    """
    seen: dict[str, dict] = {}
    combos = [(c, p, o) for c in categories for p in periods for o in orderings]
    for i, (category, period, ordering) in enumerate(combos, 1):
        for page in range(pages):
            try:
                payload = api.leaderboard(client, offset=page * LEADERBOARD_PAGE,
                                          limit=LEADERBOARD_PAGE, category=category,
                                          time_period=period, order_by=ordering)
            except FetchError:
                # One dead combination must not abort a sweep of eighty-eight of them.
                break
            rows = records.parse_leaderboard(payload)
            for row in rows:
                seen.setdefault(row["address"], row)
            if len(rows) < LEADERBOARD_PAGE:
                break
        if on_progress:
            on_progress(i, len(combos), len(seen))

    store.upsert_wallets(con, list(seen.values()))
    return list(seen)


def fetch_closed(client: Client, address: str, max_pages: int = MAX_CLOSED_PAGES,
                 until_ts: int | None = None, enough: int | None = None) -> list[dict]:
    """Every settled position for a wallet, paged out.

    The endpoint caps at 50 rows per page whatever `limit` says, so a short page means the end
    of the history rather than an error.

    `until_ts` and `enough` together stop the paging early, and the pair exists because of a
    trap in how this endpoint orders its results. Rows come back newest first, so for a wallet
    that trades daily the first several pages sit entirely inside the recent window that
    walk-forward ranking is required to ignore. Filter those out and a flat page cap leaves the
    most active wallets -- exactly the ones worth copying -- scored on a handful of positions or
    none at all. Paging therefore continues until `enough` positions older than `until_ts` are
    in hand, and only then stops.
    """
    out: list[dict] = []
    for page in range(max_pages):
        payload = api.closed_positions(client, address, offset=page * CLOSED_POSITIONS_PAGE)
        rows = records.parse_closed_positions(payload)
        out.extend(rows)
        if len(rows) < CLOSED_POSITIONS_PAGE:
            break
        if until_ts is not None and enough:
            have = sum(1 for p in out if p.get("end_ts") and p["end_ts"] <= until_ts)
            if have >= enough:
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


def score_trader(con, client: Client, address: str, max_pages: int = MAX_CLOSED_PAGES,
                 until_ts: int | None = None) -> tuple[skill.SkillScore, float]:
    """Fetch, score and persist one wallet. Returns (score, estimated account value).

    `until_ts` restricts scoring to positions that had already settled by that moment. It is
    what makes walk-forward ranking honest: without it the ranking sees the same outcomes the
    validation window is about to be judged on, and any result it produces is circular.
    Positions with no settlement time are dropped when a cutoff is given, because a position we
    cannot date cannot be proved to belong on the near side of it.
    """
    address = address.lower()
    closed = fetch_closed(client, address, max_pages, until_ts=until_ts,
                          enough=RANK_SAMPLE_TARGET)
    if until_ts is not None:
        closed = [p for p in closed if p.get("end_ts") and p["end_ts"] <= until_ts]
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
