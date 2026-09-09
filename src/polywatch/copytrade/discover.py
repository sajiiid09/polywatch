"""Trader discovery: find wallets worth copying, and score the ones that survive.

Four stages, ordered by what they cost, so that the expensive question is only ever asked about
wallets that have survived the cheap ones.

  1. **Sweep** (`sweep.py`) -- the leaderboard across every category, window and ordering. Wide
     and cheap. Several hundred distinct wallets, biased toward smaller ones: ranking by PnL
     sorts by bankroll as much as by ability, and a wallet turning $2k into $3k is both more
     impressive and more copyable at a $100 bankroll than one turning $2M into $2.1M. Volume is
     a filter, never a ranking.
  2. **Screen** (`screen.py`) -- behavioural, from a recon page of each wallet's trades. Drops
     the market makers, whose edge is latency and is therefore gone by the time a copier sees
     the fill.
  3. **Score** (`skill.py`) -- evidential, from settled positions. Win rate, ROI, Brier,
     drawdown, consistency, hold times, fee-adjusted return, recent form, and whether the record
     is distinguishable from its own variance.
  4. **Replay** (`replay.py`) -- the only stage that measures *us copying them* rather than
     them. Expensive, because it needs price history around every fill, and narrow for exactly
     that reason.

Stage 4 is what makes the other three worth running. A wallet can pass every behavioural screen,
post a fine Brier score and a real ROI, and still be uncopyable -- because the price it bought at
was gone fifteen seconds later. Nothing before replay can see that, and it is the single most
common reason a promising candidate is not one.

Nothing here picks a trader. It produces a ranked shortlist and stops.
"""

from __future__ import annotations

import json

import time

from ..config import CLOSED_POSITIONS_PAGE, REPLAY_LAGS, TRADES_PAGE
from ..db import store
from ..fetch import polymarket as api
from ..fetch.client import Client, FetchError
from ..parse import records
from .. import screen
from . import book as bk
from . import rank
from . import replay as replay_mod
from . import skill
from . import strategy as strategy_mod

# Trade pages pulled per candidate for hold times and replay. Four pages is 4000 fills, which
# covers months for a human and days for something that should not have survived screening.
TRADE_PAGES_FOR_SCORING = 4

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


def fetch_trades(client: Client, address: str, pages: int = TRADE_PAGES_FOR_SCORING,
                 since_ts: int | None = None) -> list[dict]:
    """Recent fills for one wallet, for round-trip matching and replay.

    Paged rather than single-page: one page is the most recent thousand fills, and for an active
    wallet that can be three days -- which would make its holding-period distribution a fact
    about our page size rather than about the wallet.
    """
    out: list[dict] = []
    for page in range(pages):
        payload = api.trades(client, address, offset=page * TRADES_PAGE, limit=TRADES_PAGE)
        rows = records.parse_trades(payload)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < TRADES_PAGE:
            break
        if since_ts is not None and min(r["ts"] for r in rows) < since_ts:
            break
    return [r for r in out if since_ts is None or r["ts"] >= since_ts]


def _rate_lookup(con):
    """(condition_id -> fee rate) resolved once, so replay does not query per round trip."""
    table = store.fee_rate_by_condition(con)

    def rate_of(item) -> float:
        cid = item.condition_id if hasattr(item, "condition_id") else item.get("condition_id")
        rate, fee_type = table.get(cid, (None, None))
        return bk.fee_rate_for(records.derive_category(fee_type), rate)

    return rate_of


def _category_lookup(con):
    table = store.fee_rate_by_condition(con)

    def category_of(pos) -> str | None:
        _rate, fee_type = table.get(pos.get("condition_id"), (None, None))
        return records.derive_category(fee_type)

    return category_of


def score_trader(con, client: Client, address: str, max_pages: int = MAX_CLOSED_PAGES,
                 *, with_replay: bool = True, trade_pages: int = TRADE_PAGES_FOR_SCORING,
                 lags: tuple[int, ...] = REPLAY_LAGS, log=print
                 ) -> tuple[skill.SkillScore, float, replay_mod.ReplayResult | None]:
    """Fetch, score, replay and persist one wallet.

    Returns (score, estimated account value, replay result or None). Replay is optional because
    it is by far the most expensive part -- it needs minute-resolution price history around every
    fill -- and a fast pass over a hundred candidates is a reasonable thing to want. What it
    costs in accuracy is stated where it is used: without it, copyability is inferred from
    holding period rather than measured.
    """
    address = address.lower()
    closed = fetch_closed(client, address, max_pages)
    est_account = account_value(client, address)

    trades = fetch_trades(client, address, trade_pages)
    if trades:
        store.insert_trades(con, trades)

    sc = skill.score(address, closed, trades=trades, now=int(time.time()),
                     rate_of=_rate_lookup(con), category_of=_category_lookup(con))

    res = None
    if with_replay and trades:
        trips = skill.match_round_trips(trades)
        replay_mod.backfill(con, client, trips, lags, log=log)
        res = replay_mod.replay(address, trades,
                                lambda tok, ts: store.price_at(con, tok, ts),
                                _rate_lookup(con), lags)
        store.upsert_trader_replay(con, address, [r.as_row() for r in res.lags])

    # What kind of trader this is, from the rows just fetched and the price windows replay just
    # backfilled -- so the label costs no extra requests. `screen.profile_wallet` supplies the
    # cadence features rather than this module recomputing timing statistics that already exist.
    cadence = None
    if trades:
        cadence = screen.profile_wallet(
            {"address": address, "username": None, "vol": None, "pnl": None},
            trades, int(time.time())).as_row()
    prof = strategy_mod.profile(address, trades, closed,
                                price_at=lambda tok, ts: store.price_at(con, tok, ts),
                                category_of=_category_lookup(con), cadence=cadence)
    store.upsert_trader_strategy(con, prof.as_row())

    ranked = rank.rank(sc, res)
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
        "top_category": sc.top_category,
        "persona_fit": None,
        "rank_score": ranked.rank_score,
        "capture_ratio": res.capture_ratio if res else None,
        "edge_half_life_s": res.edge_half_life_s if res else None,
        "hold_p50_s": sc.hold_p50_s,
        "fee_adjusted_roi": sc.fee_adjusted_roi,
        "recent_roi": sc.recent_roi,
        "luck_p": sc.luck_p,
        "excluded": ranked.excluded,
        "archetype": prof.archetype,
        "strategy_confidence": prof.confidence,
        "metrics_json": json.dumps({**sc.as_row(), "components": ranked.components,
                                    "strategy": prof.features}),
    })
    return sc, est_account, res


def run(con, client: Client, *, limit: int = 20, pages: int = 1, with_replay: bool = True,
        categories=None, thresholds=None, max_candidates: int = 60, log=print) -> list:
    """The whole funnel: sweep, screen, score, replay, rank. Returns the shortlist.

    `max_candidates` bounds stage 3, which is where the cost is. Screening happily passes two
    hundred wallets and scoring each one is thousands of requests; the cap takes the best-ranked
    survivors by leaderboard position, which is a weak signal but the only one available before
    anything has been scored.
    """
    from ..screen import Thresholds, screen as run_screen
    from . import sweep as sweep_mod

    log("stage 1/4  sweeping the leaderboard")
    kw = {"categories": categories} if categories else {}
    pool = sweep_mod.sweep(client, pages=pages, log=log, **kw)
    sweep_mod.persist(con, pool)
    log(f"  {len(pool)} distinct wallets")

    log("stage 2/4  recon and behavioural screening")
    from ..ingest import Stats, recon_trades
    stats = Stats()
    recon_trades(con, client, sorted(pool), 0, stats)
    results = run_screen(con, thresholds or Thresholds())
    passed = [p_ for p_, ok, _ in results if ok]
    log(f"  {len(passed)}/{len(results)} wallets passed screening")
    if not passed:
        log("  nothing survived screening -- loosen the thresholds and re-run")
        return []

    # Best leaderboard rank first. A weak ordering, and deliberately the only one used here:
    # anything stronger would be a judgement made before the evidence was gathered.
    passed.sort(key=lambda p_: (p_.vol or 0.0))
    candidates = [p_.address for p_ in passed[:max_candidates]]

    log(f"stage 3/4  scoring {len(candidates)} candidates"
        + ("  (with lag replay)" if with_replay else "  (no replay)"))
    for i, addr in enumerate(candidates, 1):
        try:
            sc, _est, res = score_trader(con, client, addr, with_replay=with_replay, log=log)
        except FetchError as e:
            log(f"  [{i}/{len(candidates)}] {addr[:12]} failed: {e}")
            continue
        capture = "-" if not res or res.capture_ratio is None else f"{res.capture_ratio:.2f}"
        log(f"  [{i}/{len(candidates)}] {addr[:12]}  {sc.n_closed:>4} settled  "
            f"roi {sc.roi:+7.1%}  capture {capture}")

    log("stage 4/4  ranking")
    return store.top_trader_scores(con, limit=limit)


def format_shortlist(rows) -> str:
    """The ranked shortlist. Ordered by rank_score, which is a judgement -- so the columns that
    fed it are shown beside it rather than behind it."""
    if not rows:
        return ("  no candidates ranked.\n"
                "  Either nothing survived screening, or every survivor failed a hard gate --\n"
                "  most often a capture ratio saying their edge does not outlive being copied.\n"
                "  `--include-excluded` shows those and why.")
    head = (f"  {'wallet':14} {'rank':>5} {'settled':>7} {'roi':>8} {'fee roi':>8} "
            f"{'capture':>7} {'half-life':>9} {'hold':>7} {'luck p':>7}  "
            f"{'archetype':18} category")
    out = [head, "  " + "-" * (len(head) - 2)]
    for r in rows:
        half = "-" if r["edge_half_life_s"] is None else f"{r['edge_half_life_s']:,.0f}s"
        cap = "-" if r["capture_ratio"] is None else f"{r['capture_ratio']:.2f}"
        hold = "-" if r["hold_p50_s"] is None else f"{r['hold_p50_s'] / 60:.0f}m"
        luck = "-" if r["luck_p"] is None else f"{r['luck_p']:.3f}"
        froi = "-" if r["fee_adjusted_roi"] is None else f"{r['fee_adjusted_roi']:+.1%}"
        out.append(f"  {r['address'][:14]:14} {(r['rank_score'] or 0):5.2f} "
                   f"{r['n_closed']:7} {(r['roi'] or 0):+8.1%} {froi:>8} {cap:>7} {half:>9} "
                   f"{hold:>7} {luck:>7}  {(r['archetype'] or '-'):18} "
                   f"{r['top_category'] or ''}"
                   + (f"   EXCLUDED: {r['excluded']}" if r["excluded"] else ""))
    return "\n".join(out)


def format_score(sc: skill.SkillScore, est_account: float, username: str | None = None,
                 strategy_row=None) -> str:
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

    # Everything above describes the trader. Everything below describes copying them, which is
    # the only question the bot has.
    if sc.n_round_trips:
        lines += [
            "",
            f"  round trips       {sc.n_round_trips} matched from recent fills",
            f"  holding period    p10 {_dur(sc.hold_p10_s)}   median {_dur(sc.hold_p50_s)}   "
            f"p90 {_dur(sc.hold_p90_s)}",
        ]
        if sc.hold_p10_s is not None and sc.hold_p10_s < 120:
            lines.append("                    a tenth of their trades are over inside two "
                         "minutes -- those are not copyable at a 15s poll")
    if sc.fee_adjusted_roi is not None:
        lines.append(f"  roi after fees    {sc.fee_adjusted_roi:+.1%}   "
                     f"(both taker fees charged; still ignores our slippage)")
    if sc.recent_roi is not None:
        lines.append(f"  recent form       {sc.recent_roi:+.1%}   "
                     f"(30-day half-life, so this is what they are doing now)")
    if sc.luck_p is not None:
        verdict = ("distinguishable from their own variance" if sc.luck_p < 0.05
                   else "NOT distinguishable from their own variance")
        lines.append(f"  luck test         p={sc.luck_p:.3f}   {verdict}")
    elif sc.n_closed:
        lines.append(f"  luck test         not run -- {sc.n_closed} settled positions is too "
                     f"few to say anything")
    if strategy_row is not None:
        # Placed with the copying half rather than the trader half: an archetype is a statement
        # about what copying this wallet would be like, not a compliment about their record.
        lines += ["", f"  strategy          {strategy_row['archetype']}  "
                      f"(confidence {(strategy_row['confidence'] or 0):.2f})",
                  f"                    {strategy_mod.describe(strategy_row['archetype'])}"]
        drift = strategy_row["pre_drift_p50"]
        if drift is not None:
            moved = "into a move already underway" if drift > 0 else "against the recent move"
            lines.append(f"  pre-entry drift   {drift:+.4f} in the 5 min before their buys "
                         f"-- they buy {moved}")
        if strategy_row["resolution_frac"] is not None and strategy_row["resolution_frac"] > 0.5:
            lines.append("                    most of their buys are never sold: their exit is "
                         "settlement, so follow_exit will not fire")
    if sc.top_category:
        lines.append(f"  earns most in     {sc.top_category}"
                     + (f"   (concentration {sc.category_concentration:.2f})"
                        if sc.category_concentration is not None else ""))
    return "\n".join(lines)


def _dur(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def backfill_strategies(con, *, limit: int | None = None, min_trades: int = 20,
                        log=print) -> int:
    """Classify every wallet whose trades are already stored. No network.

    Discovery classifies as it scores, but a database that predates the classifier holds a
    million fills and no labels, and refetching them to derive something already derivable would
    be absurd. So this reads `trades` and nothing else.

    What it cannot do is compute category concentration: that comes from settled positions with
    realized PnL, which are not stored. `cat_hhi` therefore comes back None and
    `event-specialist` scores neutral -- reported as missing rather than defaulted, which is why
    a backfilled label can differ from one produced by a full `polywatch trader` pass.
    """
    addresses = [r[0] for r in con.execute(
        "SELECT wallet, COUNT(*) c FROM trades GROUP BY wallet HAVING c >= ? "
        "ORDER BY c DESC" + (" LIMIT ?" if limit else ""),
        (min_trades, limit) if limit else (min_trades,))]
    price_at = lambda tok, ts: store.price_at(con, tok, ts)          # noqa: E731
    now = int(time.time())
    done = 0
    for i, address in enumerate(addresses, 1):
        rows = [dict(r) for r in store.trades_for(con, address)]
        if not rows:
            continue
        cadence = screen.profile_wallet(
            {"address": address, "username": None, "vol": None, "pnl": None},
            rows, now).as_row()
        prof = strategy_mod.profile(address, rows, None, price_at=price_at, cadence=cadence)
        store.upsert_trader_strategy(con, prof.as_row())
        if store.get_trader_score(con, address) is not None:
            store.set_trader_archetype(con, address, prof.archetype, prof.confidence)
        done += 1
        if i % 50 == 0:
            log(f"  {i}/{len(addresses)} classified")
    return done
