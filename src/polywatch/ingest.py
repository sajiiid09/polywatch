"""Ingestion orchestration: leaderboard -> trades -> markets/assets -> price windows.

Resumable throughout. Re-running is cheap and idempotent: trades dedupe on their natural key,
markets upsert, and price windows already recorded in `price_windows` are never refetched.
"""

from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .config import (LEADERBOARD_PAGE, MARKET_BATCH, TRADES_PAGE, WINDOW_MERGE_GAP_S,
                     WINDOW_POST_S, WINDOW_PRE_S)
from .db import store
from .fetch import polymarket as api
from .fetch.client import Client, FetchError, RateLimiter
from .parse import records
from .screen import Thresholds, screen as run_screen

MAX_WINDOW_SPAN_S = 30 * 86400  # split longer spans: one request per ~month of minutes


@dataclass
class Stats:
    wallets: int = 0
    screened: int = 0
    selected: int = 0
    trade_pages: int = 0
    trades_new: int = 0
    markets: int = 0
    assets: int = 0
    price_requests: int = 0
    price_points: int = 0
    errors: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = [
            f"wallets        {self.wallets}",
            f"screened       {self.screened}",
            f"selected       {self.selected}",
            f"trade pages    {self.trade_pages}",
            f"trades added   {self.trades_new}",
            f"markets        {self.markets}",
            f"assets         {self.assets}",
            f"price requests {self.price_requests}",
            f"price points   {self.price_points}",
        ]
        if self.errors:
            lines.append(f"errors         {len(self.errors)}")
            lines += [f"  - {e}" for e in self.errors[:10]]
        return "\n".join(lines)


# --- interval algebra for price windows -----------------------------------


def merge_intervals(spans: list[tuple[int, int]], gap: int = 0) -> list[tuple[int, int]]:
    """Union of spans, merging any pair separated by <= `gap`.

    Merging is a cost decision: two windows 10 minutes apart would produce two HTTP requests whose
    payloads overlap anyway, so one wider request is strictly cheaper.
    """
    if not spans:
        return []
    spans = sorted(spans)
    out = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def subtract(spans: list[tuple[int, int]], covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """spans minus already-fetched coverage. This is what makes resume free."""
    covered = merge_intervals(covered)
    out: list[tuple[int, int]] = []
    for s, e in spans:
        cur = [(s, e)]
        for cs, ce in covered:
            nxt = []
            for a, b in cur:
                if ce <= a or cs >= b:
                    nxt.append((a, b))
                    continue
                if cs > a:
                    nxt.append((a, min(cs, b)))
                if ce < b:
                    nxt.append((max(ce, a), b))
            cur = nxt
        out.extend([(a, b) for a, b in cur if b > a])
    return out


def chunk(spans: list[tuple[int, int]], max_span: int = MAX_WINDOW_SPAN_S) -> list[tuple[int, int]]:
    out = []
    for s, e in spans:
        while e - s > max_span:
            out.append((s, s + max_span))
            s += max_span
        out.append((s, e))
    return out


# --- parallel fetching ---------------------------------------------------


def _worker_client(client: Client) -> Client:
    """A Client for worker threads: shares the parent's rate limiter so the cap stays global,
    but buffers its log rows instead of writing to SQLite from a thread that doesn't own it."""
    return Client(con=None, raw_dir=client.raw_dir, dump_raw=client.dump_raw,
                  limiter=client.limiter, buffer_logs=True)


def _parallel(fn, tasks, workers: int):
    """Run fn over tasks, yielding results as they land. Serial when workers <= 1."""
    if workers <= 1:
        for t in tasks:
            yield fn(t)
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        yield from pool.map(fn, tasks)


# --- stages ---------------------------------------------------------------


def ingest_wallets(con: sqlite3.Connection, client: Client, limit: int, stats: Stats) -> list[str]:
    """Top `limit` wallets by PnL. The server caps each page at 50, so this pages by offset."""
    rows: list[dict] = []
    offset = 0
    while len(rows) < limit:
        payload = api.leaderboard(client, offset=offset, limit=LEADERBOARD_PAGE)
        parsed = records.parse_leaderboard(payload)
        if not parsed:
            break
        rows.extend(parsed)
        offset += len(parsed)
        if len(parsed) < LEADERBOARD_PAGE:
            break
    rows = rows[:limit]
    stats.wallets = store.upsert_wallets(con, rows)
    return [r["address"] for r in rows]


def recon_trades(con: sqlite3.Connection, client: Client, wallets: list[str], since_ts: int,
                 stats: Stats, workers: int = 4) -> None:
    """One page of recent trades per wallet -- enough to fingerprint behaviour before committing
    to full history. 50 requests here can save tens of thousands downstream."""
    worker = _worker_client(client)

    def fetch(wallet: str):
        try:
            return wallet, api.trades(worker, wallet, offset=0, limit=TRADES_PAGE), None
        except FetchError as e:
            return wallet, None, f"recon {wallet}: {e}"

    for wallet, payload, err in _parallel(fetch, wallets, workers):
        stats.trade_pages += 1
        if err:
            stats.errors.append(err)
            continue
        rows = [t for t in records.parse_trades(payload) if t["ts"] >= since_ts]
        stats.trades_new += store.insert_trades(con, rows)
    worker.drain_logs(con)


def apply_screen(con: sqlite3.Connection, th: Thresholds, stats: Stats) -> list[str]:
    """Score every candidate against the thresholds and persist the verdict with its reasons."""
    results = run_screen(con, th)
    selected = []
    for profile, ok, fails in results:
        store.set_screen(con, profile.address, ok, "; ".join(fails), json.dumps(profile.as_row()))
        if ok:
            selected.append(profile.address)
    stats.screened = len(results)
    stats.selected = len(selected)
    print(f"  {len(selected)}/{len(results)} wallets passed screening")
    for profile, ok, fails in results:
        if not ok:
            print(f"    reject {profile.address[:12]} {profile.username or '':18} {fails[0]}")
    return selected


def ingest_trades(con: sqlite3.Connection, client: Client, wallets: list[str], since_ts: int,
                  max_pages: int, stats: Stats) -> None:
    """Full trade history per wallet, back to `since_ts`.

    The feed is timestamp-DESC, so paging stops as soon as a page's newest trade predates the
    cutoff -- no need to walk a wallet's entire 2020 history to collect its 2026 one.
    """
    for i, wallet in enumerate(wallets, 1):
        offset = 0
        oldest = None
        added = 0
        for _ in range(max_pages):
            try:
                payload = api.trades(client, wallet, offset=offset, limit=TRADES_PAGE)
            except FetchError as e:
                stats.errors.append(f"trades {wallet}: {e}")
                break
            stats.trade_pages += 1
            parsed = records.parse_trades(payload)
            if not parsed:
                break
            keep = [t for t in parsed if t["ts"] >= since_ts]
            added += store.insert_trades(con, keep)
            oldest = min([t["ts"] for t in parsed] + ([oldest] if oldest else []))
            page_newest = max(t["ts"] for t in parsed)
            if page_newest < since_ts or len(parsed) < TRADES_PAGE:
                break
            offset += len(parsed)
        stats.trades_new += added
        if oldest:
            store.set_wallet_progress(con, wallet, oldest)
        print(f"  [{i}/{len(wallets)}] {wallet} +{added} trades")


def ingest_markets(con: sqlite3.Connection, client: Client, stats: Stats,
                   refresh_open: bool = True, workers: int = 4) -> None:
    """Market metadata for every condition id seen in trades.

    Gamma hides closed markets unless closed=true is passed, so each batch is swept twice: once
    for settled markets, once for live ones. Unresolved markets are re-fetched on later runs
    because a market that was open last week may have resolved since.
    """
    wanted = store.condition_ids_in_trades(con, selected_only=True)
    known = store.known_condition_ids(con)
    todo = sorted(wanted - known)
    if refresh_open:
        unresolved = {r[0] for r in con.execute("SELECT condition_id FROM markets WHERE resolved=0")}
        todo = sorted(set(todo) | (unresolved & wanted))

    batches = [todo[i:i + MARKET_BATCH] for i in range(0, len(todo), MARKET_BATCH)]
    worker = _worker_client(client)

    def fetch_batch(batch: list[str]):
        found: dict[str, dict] = {}
        errs: list[str] = []
        for closed in (True, False):
            missing = [c for c in batch if c not in found]
            if not missing:
                break
            try:
                payload = api.markets_by_condition(worker, missing, closed=closed)
            except FetchError as e:
                errs.append(f"markets batch: {e}")
                continue
            for rec in payload:
                cid = rec.get("conditionId")
                if cid:
                    found[cid] = rec
        return batch, found, errs

    done = 0
    for batch, found, errs in _parallel(fetch_batch, batches, workers):
        stats.errors.extend(errs)
        market_rows, asset_rows = [], []
        for rec in found.values():
            market, assets = records.parse_market(rec)
            market_rows.append(market)
            asset_rows.extend(assets)
        stats.markets += store.upsert_markets(con, market_rows)
        stats.assets += store.upsert_assets(con, asset_rows)
        for cid in batch:
            if cid not in found:
                stats.errors.append(f"market not found in gamma: {cid}")
        done += len(batch)
        if done % (MARKET_BATCH * 10) == 0 or done >= len(todo):
            print(f"  markets {done}/{len(todo)}")
    worker.drain_logs(con)


def ingest_prices(con: sqlite3.Connection, client: Client, stats: Stats,
                  resolved_only: bool = True, workers: int = 4) -> None:
    """Minute-resolution price windows around every trade.

    Full history for every asset is not viable (a year of minutes is ~525k rows per asset), so
    this fetches only [t-PRE, t+POST] around each trade -- POST covers the whole Step 4 lag sweep
    -- merges overlapping windows per asset, and subtracts what is already on disk.
    """
    by_token = store.trade_times_by_token(con, selected_only=True)
    if resolved_only:
        eligible = {
            r[0] for r in con.execute(
                """SELECT a.token_id FROM assets a
                   JOIN markets m ON m.condition_id = a.condition_id
                   WHERE m.resolved = 1"""
            )
        }
        by_token = {k: v for k, v in by_token.items() if k in eligible}

    covered = store.existing_windows(con)

    # Plan every request up front: merge each asset's per-trade windows, subtract what is already
    # on disk, split anything longer than a month. Planning before fetching keeps the parallel
    # stage a flat list of independent jobs.
    tasks: list[tuple[str, int, int]] = []
    for token_id, times in sorted(by_token.items()):
        spans = merge_intervals(
            [(t - WINDOW_PRE_S, t + WINDOW_POST_S) for t in times], gap=WINDOW_MERGE_GAP_S
        )
        for start_ts, end_ts in chunk(subtract(spans, covered.get(token_id, []))):
            tasks.append((token_id, start_ts, end_ts))

    print(f"  {len(by_token)} assets -> {len(tasks)} price requests")
    worker = _worker_client(client)

    def fetch_window(task):
        token_id, start_ts, end_ts = task
        try:
            payload = api.prices_history(worker, token_id, start_ts, end_ts)
        except FetchError as e:
            return task, None, f"prices {token_id[:16]}: {e}"
        return task, records.parse_price_history(payload), None

    # Only this thread touches SQLite; workers hand back parsed points.
    done = 0
    for (token_id, start_ts, end_ts), points, err in _parallel(fetch_window, tasks, workers):
        done += 1
        if err:
            stats.errors.append(err)
            continue
        stats.price_requests += 1
        stats.price_points += store.insert_prices(con, token_id, points)
        # Record the window even when it comes back empty: "we asked and there were no quotes"
        # is a fact worth keeping, and stops the next run asking again.
        store.record_window(con, token_id, start_ts, end_ts, len(points))
        if done % 250 == 0 or done == len(tasks):
            worker.drain_logs(con)
            print(f"  prices {done}/{len(tasks)} requests")
    worker.drain_logs(con)


def run(db_path, wallet_limit: int, since_ts: int, max_pages: int, rps: float,
        resolved_only: bool = True, workers: int = 4,
        thresholds: Thresholds | None = None, screen_only: bool = False) -> Stats:
    con = store.connect(db_path)
    store.init_db(con)
    client = Client(con=con, rps=rps)
    stats = Stats()
    t0 = time.time()

    print("stage 1/6  leaderboard")
    candidates = ingest_wallets(con, client, wallet_limit, stats)
    print(f"  {len(candidates)} candidates")

    print("stage 2/6  recon (one trade page per candidate)")
    recon_trades(con, client, candidates, since_ts, stats, workers=workers)

    print("stage 3/6  screening")
    wallets = apply_screen(con, thresholds or Thresholds(), stats)
    if not wallets:
        print("  nothing survived screening -- loosen thresholds and re-run")
        return stats

    if screen_only:
        print("  --screen-only: stopping before deep ingest")
        return stats

    print("stage 4/6  full trade history for selected wallets")
    ingest_trades(con, client, wallets, since_ts, max_pages, stats)

    print("stage 5/6  markets + assets")
    ingest_markets(con, client, stats, workers=workers)

    print("stage 6/6  price windows")
    ingest_prices(con, client, stats, resolved_only=resolved_only, workers=workers)

    print(f"\ndone in {time.time() - t0:.0f}s")
    return stats
