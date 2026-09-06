"""The funnel: from the whole leaderboard down to one wallet worth copying.

Four stages, ordered by cost per wallet, so the expensive evidence is only ever gathered for
wallets that already survived the cheap evidence.

    1. sweep      88 leaderboard combinations   ~176 requests   -> several hundred candidates
    2. screen     one page of trades each       1 req/wallet    -> humans, still trading
    3. rank       settled positions, pre-cutoff ~4 req/wallet   -> a shortlist with a score
    4. validate   replay the unseen window      ~70 req/wallet  -> a recommendation

Stage 4 is what separates this from a leaderboard with extra arithmetic. Stages 1-3 rank
wallets on history up to a cutoff; stage 4 then replays the days *after* that cutoff through
the same paper engine `polywatch backtest` uses. The ranking never saw those trades, so the
result answers the question that actually matters -- does this selection rule pick wallets that
go on to perform -- rather than the much easier question of whether past winners won.

What the split cannot remove is survivorship: every candidate here is a wallet that reached a
leaderboard, so this measures the ranking rule among visible wallets, not among all traders.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .. import ingest
from ..config import (BACKTEST_SLIPPAGE_SWEEP, DEFAULT_BANKROLL_USD, DEFAULT_STAKE_FRACTION,
                      FINALIST_COUNT, MAX_RECENCY_DAYS_ACTIVE, MIN_SETTLED_FOR_RANK,
                      RANK_SAMPLE_TARGET, RANK_WINDOW_DAYS, RANK_WORKERS, VALIDATION_DAYS)
from ..db import store
from ..fetch.client import Client, FetchError
from ..screen import Thresholds
from . import discover, rank, replay, skill
from .task import Task


@dataclass
class Funnel:
    """Counts at every stage, so a disappointing shortlist can be explained rather than guessed
    at. `swept -> screened -> ranked -> validated` is the first thing to read when the answer
    is "nobody qualifies"."""
    swept: int = 0
    screened: int = 0
    passed_screen: int = 0
    ranked: int = 0
    too_little_history: int = 0
    uncopyable: int = 0
    validated: int = 0
    errors: list = field(default_factory=list)


@dataclass
class Finalist:
    ranked: rank.RankedTrader
    username: str | None
    out_of_sample: dict          # slippage -> final account value on the unseen window
    signals: int
    closed: int
    disqualifiers: list


def windows(now: int | None = None) -> tuple[int, int]:
    """(rank_start, cutoff). Wallets are scored on [rank_start, cutoff] and validated after."""
    now = now or int(time.time())
    return now - RANK_WINDOW_DAYS * 86400, now - VALIDATION_DAYS * 86400


def run(con, client: Client, bankroll: float = DEFAULT_BANKROLL_USD,
        finalists: int = FINALIST_COUNT, now: int | None = None,
        reuse_scan: bool = False, reuse_scores: bool = False,
        log=print) -> tuple[Funnel, list[Finalist]]:
    """The whole funnel. Returns (counts, finalists ordered best-first on unseen data).

    `reuse_scan` skips stages 1 and 2's fetching and screens whatever is already in the
    database. Only sound when the existing recon is fresh: the screen's recency test is
    measured against the newest trade on file, so stale recon would silently rate a wallet that
    stopped trading last week as active today.

    `reuse_scores` goes further and skips stage 3 as well, re-ranking the metrics already
    stored. That is the path to take after changing a weight or the calibration formula: the
    settled positions have not moved, only the opinion about how to read them.
    """
    now = now or int(time.time())
    rank_start, cutoff = windows(now)
    funnel = Funnel()

    if reuse_scores:
        log("re-ranking stored scores; no wallet is refetched")
        scores, accounts = rank.from_stored(store.stored_scores(con))
        funnel.swept = funnel.screened = funnel.passed_screen = len(scores)
        return _rank_and_validate(con, client, scores, accounts, bankroll, finalists,
                                  cutoff, funnel, log)

    # --- 1. sweep ------------------------------------------------------------------------
    if reuse_scan:
        addresses = [w["address"] for w in store.wallets(con)]
        funnel.swept = len(addresses)
        log(f"reusing {funnel.swept} wallets already scanned")
    else:
        log("sweeping leaderboard across categories, windows and orderings...")
        addresses = discover.sweep_leaderboard(
            con, client,
            on_progress=lambda i, n, found: log(f"  [{i}/{n}] {found} unique wallets")
            if i % 20 == 0 else None)
        funnel.swept = len(addresses)
        log(f"  {funnel.swept} unique candidate wallets")

    # --- 2. screen -----------------------------------------------------------------------
    log("screening for humans still trading...")
    stats = ingest.Stats()
    if not reuse_scan:
        ingest.recon_trades(con, client, addresses, rank_start, stats)
    # 48 hours rather than the usual seven days: a wallet last seen six days ago may simply
    # have stopped, and copying silence produces neither trades nor information.
    thresholds = Thresholds(max_recency_days=MAX_RECENCY_DAYS_ACTIVE)
    survivors = ingest.apply_screen(con, thresholds, stats)
    funnel.screened, funnel.passed_screen = stats.screened, len(survivors)

    # --- 3. rank on pre-cutoff evidence only ----------------------------------------------
    log(f"scoring {len(survivors)} wallets on settled positions before "
        f"{time.strftime('%Y-%m-%d', time.gmtime(cutoff))}...")
    scores, accounts = _score_cohort(con, client, survivors, cutoff, funnel, log)

    return _rank_and_validate(con, client, scores, accounts, bankroll, finalists, cutoff,
                              funnel, log)


def _rank_and_validate(con, client, scores, accounts, bankroll, finalists, cutoff, funnel, log):
    """Stages 3b and 4: rank the cohort, drop what cannot be copied, validate the rest."""
    ranked = rank.rank(scores, accounts)
    funnel.ranked = len(ranked)
    for r in ranked:
        store.upsert_trader_score(con, r.as_row())

    # Drop wallets this bankroll cannot follow before spending validation on them. A live sweep
    # once produced twelve finalists in a row whose every trade the engine then skipped -- they
    # bought near-certainties at 0.998, above both the price band and what a $2 stake can reach
    # through the five-share minimum. Ranking them was worse than useless: they displaced
    # wallets that could actually have been copied.
    probe = Task(name="probe", trader="0x" + "0" * 40, bankroll=bankroll)
    stake = bankroll * DEFAULT_STAKE_FRACTION
    copyable, rejected = [], []
    for r in ranked:
        ok, why = rank.copyable(r.score, probe.price_band(), stake)
        (copyable if ok else rejected).append((r, why))
    funnel.uncopyable = len(rejected)
    if rejected:
        log(f"  {len(rejected)} ranked wallets are out of reach at ${stake:,.2f} a trade "
            f"(e.g. {rejected[0][1]})")

    # --- 4. validate the shortlist on the unseen window ------------------------------------
    shortlist = [r for r, _ in copyable][:finalists]
    log(f"walk-forward validating the top {len(shortlist)} on the last "
        f"{VALIDATION_DAYS} days...")
    out = []
    for i, r in enumerate(shortlist, 1):
        log(f"  [{i}/{len(shortlist)}] {r.address}")
        try:
            _prepare(con, client, r.address, cutoff)
            results = _validate(con, r.address, bankroll, cutoff)
        except (FetchError, ValueError) as e:
            funnel.errors.append(f"validate {r.address}: {e}")
            continue
        row = con.execute("SELECT username FROM wallets WHERE address=?", (r.address,)).fetchone()
        out.append(Finalist(
            ranked=r, username=row["username"] if row else None,
            out_of_sample={s: v["final_value_at_cost"] for s, v in results.items()},
            signals=max(v["signals_seen"] for v in results.values()),
            closed=max(v["positions_closed"] for v in results.values()),
            disqualifiers=rank.disqualifiers(r),
        ))
    funnel.validated = len(out)

    # Ordered by the pessimistic slippage. A candidate that still makes money once execution
    # friction is assumed to be bad is the only kind worth putting real money behind.
    worst = max(BACKTEST_SLIPPAGE_SWEEP)
    out.sort(key=lambda f: f.out_of_sample.get(worst, 0.0), reverse=True)
    return funnel, out


def _score_cohort(con, client: Client, addresses: list[str], cutoff: int,
                  funnel: Funnel, log) -> tuple[list, dict]:
    """Stage 3, fanned out across threads.

    Fetching is the whole cost here -- several hundred wallets at a dozen requests each -- and
    it is all waiting on the network. The workers share the parent's rate limiter, so this
    raises throughput without raising the request rate Polymarket sees. Measured serially the
    stage ran at roughly six seconds a wallet, which over 904 survivors was an hour and a half.

    Scoring and persistence stay on this thread: the worker clients hold no database connection
    (`ingest._worker_client`), because SQLite connections belong to the thread that opened them.
    """
    worker = ingest._worker_client(client)

    def fetch(address: str):
        try:
            closed = discover.fetch_closed(worker, address, until_ts=cutoff,
                                           enough=RANK_SAMPLE_TARGET)
            return address, closed, discover.account_value(worker, address), None
        except FetchError as e:
            return address, None, 0.0, f"score {address}: {e}"

    scores, accounts = [], {}
    done = 0
    for address, closed, est, err in ingest._parallel(fetch, addresses, RANK_WORKERS):
        done += 1
        if err:
            funnel.errors.append(err)
            continue
        # The cutoff filter lives here rather than in fetch_closed so that the fetcher stays a
        # fetcher; an undated position cannot be shown to predate the cutoff, so it is dropped.
        pre = [p for p in closed if p.get("end_ts") and p["end_ts"] <= cutoff]
        sc = skill.score(address, pre)
        if sc.n_closed < MIN_SETTLED_FOR_RANK:
            funnel.too_little_history += 1
        else:
            scores.append(sc)
            accounts[address] = est
        if done % 50 == 0:
            log(f"  [{done}/{len(addresses)}] {len(scores)} with enough history")

    worker.drain_logs(con)
    return scores, accounts


def valid_address(address: str) -> str:
    """Normalise and sanity-check a wallet address before it costs a request.

    A typo'd address is indistinguishable from a real one with no history -- both come back
    empty -- so the shape is checked here rather than leaving the user to wonder why their
    trader has no trades.
    """
    address = (address or "").strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", address):
        raise ValueError(f"{address!r} is not a Polymarket wallet address "
                         "(expected 0x followed by 40 hex characters)")
    return address


def prepare_wallet(con, client: Client, address: str, since_ts: int,
                   log=lambda *_: None) -> int:
    """Make any Polymarket address backtestable, whether or not it has ever been seen.

    This is what lets a wallet be handed to the bot cold: give it an address, and it fetches the
    trade history and the market metadata needed to settle those trades, registering the wallet
    on the way so later stages can find it. Returns the number of trades on file afterwards.

    Everything it needs is public and unauthenticated -- the same GET-only client the analytics
    side uses. Following someone requires nothing from them and nothing from us but their
    address.
    """
    address = valid_address(address)
    known = {w["address"] for w in store.wallets(con)}
    if address not in known:
        # `source` marks where a wallet came from; one handed to us directly is neither a
        # leaderboard entry nor a sampled control.
        store.upsert_wallets(con, [{"address": address, "source": "manual", "rank": None,
                                    "username": None, "vol": None, "pnl": None}])

    stats = ingest.Stats()
    log(f"  fetching trades for {address}...")
    ingest.ingest_trades(con, client, [address], since_ts, max_pages=25, stats=stats)
    log("  fetching market metadata...")
    ingest.ingest_markets(con, client, stats, refresh_open=False, wallets=[address])
    n = len(store.trader_trades(con, address, since_ts))
    log(f"  {n} trades on file since {time.strftime('%Y-%m-%d', time.gmtime(since_ts))}")
    return n


def _prepare(con, client: Client, address: str, cutoff: int) -> None:
    """Fetch what the replay needs for one wallet: full trades, and market metadata.

    Deliberately no price ingest. Hold-to-resolution settlement reads `markets.winning_index`,
    so a candidate costs roughly seventy requests to make backtestable rather than the
    thousands the analytics side spends on per-trade price windows.

    Scoped to this one wallet. The unscoped form pulls markets for every screened wallet, which
    on a 3,107-wallet sweep is over a hundred thousand markets and roughly twelve thousand
    requests -- paid to validate a candidate that traded a few hundred of them.
    """
    stats = ingest.Stats()
    ingest.ingest_trades(con, client, [address], cutoff, max_pages=25, stats=stats)
    ingest.ingest_markets(con, client, stats, refresh_open=False, wallets=[address])


def _validate(con, address: str, bankroll: float, cutoff: int) -> dict:
    """Replay the unseen window at each slippage. Returns {slippage: summary}."""
    stake = bankroll * DEFAULT_STAKE_FRACTION
    results = {}
    for slip in BACKTEST_SLIPPAGE_SWEEP:
        task = Task(
            name=f"wf-{address[:10]}-s{int(slip * 1000):03d}", trader=address,
            bankroll=bankroll, fixed_usd=stake, max_market_usd=stake * 2,
            max_concurrent=5, slippage=slip,
        )
        # since_ts is the cutoff: the ranking above never saw a single one of these trades.
        results[slip] = replay.run(con, task, slip, since_ts=cutoff + 1)
    return results


def format_report(funnel: Funnel, finalists: list, bankroll: float, cutoff: int) -> str:
    """The recommendation, and everything needed to disbelieve it."""
    worst = max(BACKTEST_SLIPPAGE_SWEEP)
    cut = time.strftime("%Y-%m-%d", time.gmtime(cutoff))
    lines = [
        "  funnel",
        f"    {funnel.swept:>6}  wallets swept from the leaderboard",
        f"    {funnel.passed_screen:>6}  passed the behavioural screen (human, traded within "
        f"{MAX_RECENCY_DAYS_ACTIVE * 24:.0f}h)",
        f"    {funnel.ranked:>6}  had {MIN_SETTLED_FOR_RANK}+ settled positions before {cut}",
        f"    {funnel.uncopyable:>6}  of those were out of reach at this bankroll",
        f"    {funnel.validated:>6}  survived walk-forward validation",
    ]
    if funnel.too_little_history:
        lines.append(f"    {funnel.too_little_history:>6}  dropped for too little history")
    if funnel.errors:
        lines.append(f"    {len(funnel.errors):>6}  errors (first: {funnel.errors[0][:60]})")

    if not finalists:
        lines += ["", "  No candidate cleared every stage. Nothing to recommend -- the honest",
                  "  outcome when the evidence is not there, not a reason to lower the bar."]
        return "\n".join(lines)

    lines += ["", f"  finalists, ranked on ${bankroll:,.0f} over the UNSEEN {VALIDATION_DAYS} "
                  f"days after {cut}", ""]
    hdr = (f"    {'trader':18}{'rank':>6}{'brier':>7}{'roi':>8}{'cons':>6}"
           + "".join(f"{s:>8.0%}" for s in BACKTEST_SLIPPAGE_SWEEP)
           + f"{'trades':>8}")
    lines += [hdr, "    " + "-" * (len(hdr) - 4)]
    for f in finalists:
        s = f.ranked.score
        brier = "n/a" if s.brier is None else f"{s.brier:.3f}"
        name = (f.username or f.ranked.address[:16])[:17]
        lines.append(
            f"    {name:18}{f.ranked.rank_score:>6.2f}{brier:>7}{s.roi:>8.1%}"
            f"{s.consistency:>6.0%}"
            + "".join(f"{f.out_of_sample.get(sl, 0):>8,.0f}" for sl in BACKTEST_SLIPPAGE_SWEEP)
            + f"{f.closed:>8}")

    best = finalists[0]
    lines += ["", "  " + "=" * 68, f"  recommendation: {best.username or best.ranked.address}",
              f"  {best.ranked.address}", ""]
    survived = best.out_of_sample.get(worst, 0.0)
    lines += [
        f"    on the {VALIDATION_DAYS} days its ranking never saw, ${bankroll:,.0f} became "
        f"${survived:,.2f}",
        f"    at the pessimistic {worst:.0%} slippage, over {best.closed} settled positions.",
    ]
    if best.disqualifiers:
        lines += ["", "    but it carries warnings, and they are the reason to hesitate:"]
        lines += [f"      - {d}" for d in best.disqualifiers]
    if survived <= bankroll:
        lines += ["", "    NOTE: this did not make money out-of-sample at the pessimistic",
                  "    slippage. It is the best of the candidates, not a profitable one."]
    if best.closed < 10:
        lines += ["", f"    NOTE: only {best.closed} positions settled in the validation window.",
                  "    That is too small a sample to distinguish skill from luck."]
    lines += ["", "    reproduce:",
              f"      polywatch backtest {best.ranked.address}"]
    return "\n".join(lines)
