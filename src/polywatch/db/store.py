"""SQLite access. Every SQL statement in the project lives here.

Kept in one module on purpose: a Postgres port later is a rewrite of this file, not a hunt
through the codebase. Callers pass and receive plain dicts/tuples, never cursors.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable, Sequence

from ..config import DB_PATH

SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    # WAL: readers (scoring) never block the single writer (ingestion).
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    # ~64MB page cache. The hot query is random access over `prices`; caching its upper B-tree
    # levels is what keeps 75M lookups off the disk.
    con.execute("PRAGMA cache_size=-65536")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA.read_text())
    # Additive migration for databases created before screening existed.
    have = {r["name"] for r in con.execute("PRAGMA table_info(wallets)")}
    for col, decl in (("selected", "INTEGER"), ("screen_reason", "TEXT"), ("profile_json", "TEXT")):
        if col not in have:
            con.execute(f"ALTER TABLE wallets ADD COLUMN {col} {decl}")
    # ...and for databases created before the trading side needed gamma's market id and the
    # per-market execution constraints. CREATE TABLE IF NOT EXISTS will not add columns to a
    # table that already exists, so these have to be spelled out.
    have = {r["name"] for r in con.execute("PRAGMA table_info(markets)")}
    for col, decl in (("gamma_id", "TEXT"), ("tick_size", "REAL"), ("order_min_size", "REAL"),
                      ("accepting_orders", "INTEGER"), ("enable_order_book", "INTEGER")):
        if col not in have:
            con.execute(f"ALTER TABLE markets ADD COLUMN {col} {decl}")
    con.commit()


def set_screen(con: sqlite3.Connection, address: str, selected: bool, reason: str,
               profile_json: str) -> None:
    con.execute(
        "UPDATE wallets SET selected=?, screen_reason=?, profile_json=? WHERE address=?",
        (1 if selected else 0, reason, profile_json, address),
    )
    con.commit()


def selected_wallets(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT address FROM wallets WHERE selected=1 ORDER BY rank")]


# --- writes ---------------------------------------------------------------


def upsert_wallets(con: sqlite3.Connection, rows: Iterable[dict]) -> int:
    now = int(time.time())
    payload = [
        (r["address"], r.get("source", "leaderboard"), r.get("rank"), r.get("username"),
         r.get("vol"), r.get("pnl"), now)
        for r in rows
    ]
    con.executemany(
        """INSERT INTO wallets (address, source, rank, username, vol, pnl, fetched_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(address) DO UPDATE SET
             rank=excluded.rank, username=excluded.username, vol=excluded.vol,
             pnl=excluded.pnl, fetched_at=excluded.fetched_at""",
        payload,
    )
    con.commit()
    return len(payload)


def set_wallet_progress(con: sqlite3.Connection, address: str, oldest_ts: int) -> None:
    con.execute("UPDATE wallets SET trades_from_ts=? WHERE address=?", (oldest_ts, address))
    con.commit()


def insert_trades(con: sqlite3.Connection, rows: Sequence[dict]) -> int:
    """Insert trades, ignoring ones already present. Returns rows actually added."""
    if not rows:
        return 0
    before = con.total_changes
    con.executemany(
        """INSERT OR IGNORE INTO trades
           (wallet, token_id, condition_id, side, size, price, ts, outcome, outcome_index, tx_hash)
           VALUES (:wallet,:token_id,:condition_id,:side,:size,:price,:ts,:outcome,
                   :outcome_index,:tx_hash)""",
        rows,
    )
    con.commit()
    return con.total_changes - before


def upsert_markets(con: sqlite3.Connection, rows: Sequence[dict]) -> int:
    if not rows:
        return 0
    now = int(time.time())
    for r in rows:
        r.setdefault("fetched_at", now)
    con.executemany(
        """INSERT INTO markets (condition_id, gamma_id, question, slug, category, closed,
                active, archived, start_ts, end_ts, outcomes_json, prices_json, uma_status_json,
                resolved, winning_index, neg_risk, fees_enabled, fee_type, fee_rate, fee_exponent,
                fee_taker_only, fee_rebate_rate, fee_source, tick_size, order_min_size,
                accepting_orders, enable_order_book, fetched_at)
           VALUES (:condition_id,:gamma_id,:question,:slug,:category,:closed,:active,:archived,
                :start_ts,:end_ts,:outcomes_json,:prices_json,:uma_status_json,:resolved,
                :winning_index,:neg_risk,:fees_enabled,:fee_type,:fee_rate,:fee_exponent,
                :fee_taker_only,:fee_rebate_rate,:fee_source,:tick_size,:order_min_size,
                :accepting_orders,:enable_order_book,:fetched_at)
           ON CONFLICT(condition_id) DO UPDATE SET
                gamma_id=excluded.gamma_id,
                question=excluded.question, slug=excluded.slug, category=excluded.category,
                closed=excluded.closed, active=excluded.active, archived=excluded.archived,
                start_ts=excluded.start_ts, end_ts=excluded.end_ts,
                outcomes_json=excluded.outcomes_json, prices_json=excluded.prices_json,
                uma_status_json=excluded.uma_status_json, resolved=excluded.resolved,
                winning_index=excluded.winning_index, neg_risk=excluded.neg_risk,
                fees_enabled=excluded.fees_enabled, fee_type=excluded.fee_type,
                fee_rate=excluded.fee_rate, fee_exponent=excluded.fee_exponent,
                fee_taker_only=excluded.fee_taker_only, fee_rebate_rate=excluded.fee_rebate_rate,
                fee_source=excluded.fee_source, tick_size=excluded.tick_size,
                order_min_size=excluded.order_min_size,
                accepting_orders=excluded.accepting_orders,
                enable_order_book=excluded.enable_order_book,
                fetched_at=excluded.fetched_at""",
        rows,
    )
    con.commit()
    return len(rows)


def upsert_assets(con: sqlite3.Connection, rows: Sequence[dict]) -> int:
    if not rows:
        return 0
    con.executemany(
        """INSERT INTO assets (token_id, condition_id, outcome_index, outcome)
           VALUES (:token_id,:condition_id,:outcome_index,:outcome)
           ON CONFLICT(token_id) DO UPDATE SET
             condition_id=excluded.condition_id, outcome_index=excluded.outcome_index,
             outcome=excluded.outcome""",
        rows,
    )
    con.commit()
    return len(rows)


def insert_prices(con: sqlite3.Connection, token_id: str, points: Sequence[tuple[int, float]]) -> int:
    if not points:
        return 0
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO prices (token_id, ts, price) VALUES (?,?,?)",
        [(token_id, t, p) for t, p in points],
    )
    con.commit()
    return con.total_changes - before


def record_window(con: sqlite3.Connection, token_id: str, start_ts: int, end_ts: int,
                  points: int) -> None:
    con.execute(
        """INSERT OR REPLACE INTO price_windows (token_id, start_ts, end_ts, points, fetched_at)
           VALUES (?,?,?,?,?)""",
        (token_id, start_ts, end_ts, points, int(time.time())),
    )
    con.commit()


def log_fetch(con: sqlite3.Connection, **kw) -> None:
    con.execute(
        """INSERT INTO ingest_log (kind, url, status, bytes, records, attempts, error, raw_path, ts)
           VALUES (:kind,:url,:status,:bytes,:records,:attempts,:error,:raw_path,:ts)""",
        {
            "kind": kw.get("kind", "?"), "url": kw.get("url", ""), "status": kw.get("status"),
            "bytes": kw.get("bytes"), "records": kw.get("records"), "attempts": kw.get("attempts"),
            "error": kw.get("error"), "raw_path": kw.get("raw_path"), "ts": int(time.time()),
        },
    )
    con.commit()


# --- reads ----------------------------------------------------------------


def price_at(con: sqlite3.Connection, token_id: str, ts: int) -> float | None:
    """THE hot query: last known price at or before `ts`. One seek on the WITHOUT ROWID PK."""
    row = con.execute(
        "SELECT price FROM prices WHERE token_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (token_id, ts),
    ).fetchone()
    return row["price"] if row else None


def wallets(con: sqlite3.Connection, source: str | None = None) -> list[sqlite3.Row]:
    if source:
        return con.execute("SELECT * FROM wallets WHERE source=? ORDER BY rank", (source,)).fetchall()
    return con.execute("SELECT * FROM wallets ORDER BY rank").fetchall()


def known_condition_ids(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT condition_id FROM markets")}


def condition_ids_in_trades(con: sqlite3.Connection, selected_only: bool = False) -> set[str]:
    """Condition ids seen in trades. `selected_only` keeps rejected wallets' recon trades from
    dragging thousands of markets into the expensive stages."""
    if selected_only:
        sql = """SELECT DISTINCT t.condition_id FROM trades t
                 JOIN wallets w ON w.address = t.wallet WHERE w.selected = 1"""
    else:
        sql = "SELECT DISTINCT condition_id FROM trades"
    return {r[0] for r in con.execute(sql)}


def condition_ids_for_wallets(con: sqlite3.Connection, wallets: Sequence[str]) -> set[str]:
    """Condition ids traded by specific wallets.

    The narrow counterpart to `condition_ids_in_trades(selected_only=True)`. Walk-forward
    validation only needs market metadata for the one candidate being validated, and the wide
    version would pull every market touched by every screened wallet -- measured on a real
    sweep, 122,277 unfetched markets and roughly twelve thousand requests, to backtest one
    wallet that traded a few hundred of them.
    """
    if not wallets:
        return set()
    marks = ",".join("?" * len(wallets))
    return {r[0] for r in con.execute(
        f"SELECT DISTINCT condition_id FROM trades WHERE wallet IN ({marks})",
        [w.lower() for w in wallets])}


def trade_times_by_token(con: sqlite3.Connection, selected_only: bool = False
                         ) -> dict[str, list[int]]:
    if selected_only:
        sql = """SELECT t.token_id, t.ts FROM trades t JOIN wallets w ON w.address = t.wallet
                 WHERE w.selected = 1 ORDER BY t.token_id, t.ts"""
    else:
        sql = "SELECT token_id, ts FROM trades ORDER BY token_id, ts"
    out: dict[str, list[int]] = {}
    for token_id, ts in con.execute(sql):
        out.setdefault(token_id, []).append(ts)
    return out


def existing_windows(con: sqlite3.Connection) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    for token_id, s, e in con.execute(
        "SELECT token_id, start_ts, end_ts FROM price_windows ORDER BY token_id, start_ts"
    ):
        out.setdefault(token_id, []).append((s, e))
    return out


def counts(con: sqlite3.Connection) -> dict[str, int]:
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "wallets": q("SELECT COUNT(*) FROM wallets"),
        "wallets_selected": q("SELECT COUNT(*) FROM wallets WHERE selected=1"),
        "wallets_rejected": q("SELECT COUNT(*) FROM wallets WHERE selected=0"),
        "trades": q("SELECT COUNT(*) FROM trades"),
        "markets": q("SELECT COUNT(*) FROM markets"),
        "markets_resolved": q("SELECT COUNT(*) FROM markets WHERE resolved=1"),
        "assets": q("SELECT COUNT(*) FROM assets"),
        "prices": q("SELECT COUNT(*) FROM prices"),
        "price_windows": q("SELECT COUNT(*) FROM price_windows"),
        "fetches": q("SELECT COUNT(*) FROM ingest_log"),
        "fetch_errors": q("SELECT COUNT(*) FROM ingest_log WHERE error IS NOT NULL"),
    }


# --- copy trading ---------------------------------------------------------
# The invariant these functions exist to protect: a finished run must be explicable from this
# database alone. Every observed action of the target lands in `signals` whether or not we
# copied it, and a skip always carries the reason it was skipped.

TASK_COLUMNS = ("name", "trader", "mode", "bankroll", "buy_method", "fixed_usd",
                "max_market_usd", "max_concurrent", "slippage", "sl_kind", "sl_value",
                "tp_kind", "tp_value", "behavior", "risk", "category", "style", "hold",
                "activity", "created_at", "updated_at", "config_json")


def upsert_task(con: sqlite3.Connection, row: dict) -> None:
    now = int(time.time())
    row.setdefault("created_at", now)
    row["updated_at"] = now
    cols = ",".join(TASK_COLUMNS)
    binds = ",".join(f":{c}" for c in TASK_COLUMNS)
    sets = ",".join(f"{c}=excluded.{c}" for c in TASK_COLUMNS if c not in ("name", "created_at"))
    con.execute(f"INSERT INTO tasks ({cols}) VALUES ({binds}) "
                f"ON CONFLICT(name) DO UPDATE SET {sets}", row)
    con.commit()


def get_task(con: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM tasks WHERE name=?", (name,)).fetchone()


def list_tasks(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()


def delete_task(con: sqlite3.Connection, name: str) -> int:
    n = con.execute("DELETE FROM tasks WHERE name=?", (name,)).rowcount
    con.commit()
    return n


def start_run(con: sqlite3.Connection, task: str, mode: str, bankroll: float,
              started_at: int | None = None) -> int:
    """`started_at` is explicit so a historical replay can stamp a run in the past. A backtest
    whose run rows all claim to have started today is unsortable against a live one."""
    cur = con.execute(
        "INSERT INTO task_runs (task, mode, started_at, start_bankroll) VALUES (?,?,?,?)",
        (task, mode, int(time.time()) if started_at is None else started_at, bankroll),
    )
    con.commit()
    return int(cur.lastrowid)


def finish_run(con: sqlite3.Connection, run_id: int, end_bankroll: float, reason: str,
               stopped_at: int | None = None) -> None:
    con.execute(
        "UPDATE task_runs SET stopped_at=?, end_bankroll=?, stop_reason=? WHERE id=?",
        (int(time.time()) if stopped_at is None else stopped_at, end_bankroll, reason, run_id),
    )
    con.commit()


def running_runs(con: sqlite3.Connection, task: str | None = None) -> list[sqlite3.Row]:
    """Runs that were never closed out. A leftover row here means a previous run was killed
    rather than stopped, and its open positions are stale -- the engine must not silently
    inherit them."""
    sql = "SELECT * FROM task_runs WHERE stopped_at IS NULL"
    args: tuple = ()
    if task is not None:
        sql += " AND task=?"
        args = (task,)
    return con.execute(sql + " ORDER BY started_at", args).fetchall()


def last_run(con: sqlite3.Connection, task: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM task_runs WHERE task=? ORDER BY started_at DESC LIMIT 1", (task,)
    ).fetchone()


def insert_signal(con: sqlite3.Connection, row: dict) -> int | None:
    """Record one observed action of the target. Returns the new id, or None if we had already
    seen it.

    Dedupe is a UNIQUE constraint rather than an in-memory set on purpose: consecutive polls
    always overlap, and a set would be lost on restart -- which is precisely when copying a
    stale trade would be most expensive.
    """
    row.setdefault("seen_ts", int(time.time()))
    cur = con.execute(
        """INSERT OR IGNORE INTO signals
               (run_id, trader, kind, tx_hash, token_id, condition_id, side, size, price,
                usdc_size, trader_ts, seen_ts, action, reason)
           VALUES (:run_id,:trader,:kind,:tx_hash,:token_id,:condition_id,:side,:size,:price,
                :usdc_size,:trader_ts,:seen_ts,:action,:reason)""",
        row,
    )
    con.commit()
    return int(cur.lastrowid) if cur.rowcount else None


def signal_seen(con: sqlite3.Connection, run_id: int, tx_hash: str, token_id: str,
                side: str | None, size: float | None) -> bool:
    return con.execute(
        """SELECT 1 FROM signals
           WHERE run_id=? AND tx_hash=? AND token_id=? AND side IS ? AND size IS ?""",
        (run_id, tx_hash, token_id, side, size),
    ).fetchone() is not None


def signals(con: sqlite3.Connection, run_id: int, action: str | None = None
            ) -> list[sqlite3.Row]:
    sql = "SELECT * FROM signals WHERE run_id=?"
    args: tuple = (run_id,)
    if action is not None:
        sql += " AND action=?"
        args += (action,)
    return con.execute(sql + " ORDER BY seen_ts, id", args).fetchall()


def skip_reasons(con: sqlite3.Connection, run_id: int) -> list[tuple[str, int]]:
    """Why we passed on trades, most common first. The main output of a paper run."""
    return [(r[0], r[1]) for r in con.execute(
        """SELECT reason, COUNT(*) FROM signals
           WHERE run_id=? AND action='skipped' GROUP BY reason ORDER BY COUNT(*) DESC""",
        (run_id,),
    )]


def insert_order(con: sqlite3.Connection, row: dict) -> int:
    row.setdefault("ts", int(time.time()))
    cur = con.execute(
        """INSERT INTO orders
               (run_id, signal_id, mode, token_id, condition_id, side, intent_usd, limit_price,
                book_vwap, filled_shares, avg_price, fee, status, reason, ts)
           VALUES (:run_id,:signal_id,:mode,:token_id,:condition_id,:side,:intent_usd,
                :limit_price,:book_vwap,:filled_shares,:avg_price,:fee,:status,:reason,:ts)""",
        row,
    )
    con.commit()
    return int(cur.lastrowid)


def orders(con: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM orders WHERE run_id=? ORDER BY ts, id",
                       (run_id,)).fetchall()


def open_position(con: sqlite3.Connection, row: dict) -> int:
    row.setdefault("opened_ts", int(time.time()))
    cur = con.execute(
        """INSERT INTO positions
               (run_id, token_id, condition_id, shares, avg_price, cost_usd, fees_paid,
                opened_ts, open)
           VALUES (:run_id,:token_id,:condition_id,:shares,:avg_price,:cost_usd,:fees_paid,
                :opened_ts,1)""",
        row,
    )
    con.commit()
    return int(cur.lastrowid)


def add_to_position(con: sqlite3.Connection, position_id: int, shares: float, cost_usd: float,
                    fee: float) -> None:
    """Fold a second fill into an open position, recomputing the weighted average price.

    avg_price has to move with the new cost or the stop-loss would keep measuring against the
    first entry only, which is how a position quietly ends up with no working stop.
    """
    con.execute(
        """UPDATE positions
           SET shares = shares + ?,
               cost_usd = cost_usd + ?,
               fees_paid = fees_paid + ?,
               avg_price = (cost_usd + ?) / NULLIF(shares + ?, 0)
           WHERE id=?""",
        (shares, cost_usd, fee, cost_usd, shares, position_id),
    )
    con.commit()


def close_position(con: sqlite3.Connection, position_id: int, proceeds_usd: float,
                   exit_fee: float, reason: str, closed_ts: int | None = None) -> float:
    """Settle a position and return its realized PnL.

    PnL is proceeds minus entry cost minus every fee on both sides. Fees are subtracted here
    rather than folded into cost_usd so that `report` can show what the taker fee actually
    cost over a run -- the number this whole exercise exists to measure.

    `closed_ts` defaults to now for a live run, but a replay must pass the historical settlement
    time. Without it every backtested position would appear to have closed the moment the
    backtest ran, and holding periods -- the thing a copy trader most needs to see -- would all
    read as zero.
    """
    row = con.execute("SELECT cost_usd, fees_paid FROM positions WHERE id=?",
                      (position_id,)).fetchone()
    if row is None:
        raise KeyError(f"no position {position_id}")
    pnl = proceeds_usd - row["cost_usd"] - row["fees_paid"] - exit_fee
    con.execute(
        """UPDATE positions
           SET open=0, closed_ts=?, close_reason=?, realized_pnl=?, fees_paid=fees_paid+?
           WHERE id=?""",
        (int(time.time()) if closed_ts is None else closed_ts, reason, pnl, exit_fee,
         position_id),
    )
    con.commit()
    return pnl


def open_positions(con: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM positions WHERE run_id=? AND open=1 ORDER BY opened_ts", (run_id,)
    ).fetchall()


def open_position_for(con: sqlite3.Connection, run_id: int, token_id: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM positions WHERE run_id=? AND token_id=? AND open=1", (run_id, token_id)
    ).fetchone()


def market_exposure(con: sqlite3.Connection, run_id: int, condition_id: str) -> float:
    """Open cost across every outcome of one market -- what the per-market cap is measured on.

    Keyed on condition_id, not token_id: holding YES and NO of the same market is two positions
    but one market's worth of risk.
    """
    row = con.execute(
        """SELECT COALESCE(SUM(cost_usd + fees_paid), 0) FROM positions
           WHERE run_id=? AND condition_id=? AND open=1""",
        (run_id, condition_id),
    ).fetchone()
    return float(row[0])


def run_cash(con: sqlite3.Connection, run_id: int) -> float:
    """Uninvested cash: the starting bankroll, minus what open positions tie up, plus what
    closed ones returned."""
    start = con.execute("SELECT start_bankroll FROM task_runs WHERE id=?",
                        (run_id,)).fetchone()
    if start is None:
        raise KeyError(f"no run {run_id}")
    tied = con.execute(
        """SELECT COALESCE(SUM(cost_usd + fees_paid), 0) FROM positions
           WHERE run_id=? AND open=1""", (run_id,)).fetchone()[0]
    # Only the PnL, not cost + PnL. `tied` counts open positions only, so a closed position's
    # cost was never subtracted here to begin with -- adding it back would credit the account
    # with money it never spent.
    realized = con.execute(
        """SELECT COALESCE(SUM(realized_pnl), 0) FROM positions
           WHERE run_id=? AND open=0""", (run_id,)).fetchone()[0]
    return float(start[0]) - float(tied) + float(realized)


def run_summary(con: sqlite3.Connection, run_id: int) -> dict:
    q = lambda sql: con.execute(sql, (run_id,)).fetchone()[0]  # noqa: E731
    return {
        "signals_seen": q("SELECT COUNT(*) FROM signals WHERE run_id=?"),
        "copied": q("SELECT COUNT(*) FROM signals WHERE run_id=? AND action='copied'"),
        "skipped": q("SELECT COUNT(*) FROM signals WHERE run_id=? AND action='skipped'"),
        "orders": q("SELECT COUNT(*) FROM orders WHERE run_id=?"),
        "positions_open": q("SELECT COUNT(*) FROM positions WHERE run_id=? AND open=1"),
        "positions_closed": q("SELECT COUNT(*) FROM positions WHERE run_id=? AND open=0"),
        "fees_paid": q("SELECT COALESCE(SUM(fees_paid),0) FROM positions WHERE run_id=?"),
        "realized_pnl": q(
            "SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE run_id=? AND open=0"),
        "cash": run_cash(con, run_id),
    }


TRADER_SCORE_COLUMNS = ("address", "scanned_at", "n_closed", "win_rate", "roi", "realized_pnl",
                        "brier", "avg_entry_price", "avg_stake_usd", "max_drawdown",
                        "consistency", "est_account_usd", "top_category", "persona_fit",
                        "rank_score", "metrics_json")


def upsert_trader_score(con: sqlite3.Connection, row: dict) -> None:
    row.setdefault("scanned_at", int(time.time()))
    cols = ",".join(TRADER_SCORE_COLUMNS)
    binds = ",".join(f":{c}" for c in TRADER_SCORE_COLUMNS)
    sets = ",".join(f"{c}=excluded.{c}" for c in TRADER_SCORE_COLUMNS if c != "address")
    con.execute(f"INSERT INTO trader_scores ({cols}) VALUES ({binds}) "
                f"ON CONFLICT(address) DO UPDATE SET {sets}", row)
    con.commit()


def get_trader_score(con: sqlite3.Connection, address: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM trader_scores WHERE address=?",
                       (address.lower(),)).fetchone()


def top_trader_scores(con: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM trader_scores WHERE rank_score IS NOT NULL "
        "ORDER BY rank_score DESC LIMIT ?", (limit,)
    ).fetchall()


MARKET_META_COLUMNS = ("condition_id", "tick_size", "min_order_size", "accepting_orders",
                       "enable_order_book", "neg_risk", "category_derived", "fee_rate",
                       "end_ts", "fetched_at")


def upsert_market_meta(con: sqlite3.Connection, row: dict) -> None:
    row.setdefault("fetched_at", int(time.time()))
    cols = ",".join(MARKET_META_COLUMNS)
    binds = ",".join(f":{c}" for c in MARKET_META_COLUMNS)
    sets = ",".join(f"{c}=excluded.{c}" for c in MARKET_META_COLUMNS if c != "condition_id")
    con.execute(f"INSERT INTO market_meta ({cols}) VALUES ({binds}) "
                f"ON CONFLICT(condition_id) DO UPDATE SET {sets}", row)
    con.commit()


def get_market_meta(con: sqlite3.Connection, condition_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM market_meta WHERE condition_id=?",
                       (condition_id,)).fetchone()


# --- Backtest support ----------------------------------------------------------------------


def get_market(con: sqlite3.Connection, condition_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM markets WHERE condition_id=?", (condition_id,)).fetchone()


def trader_trades(con: sqlite3.Connection, wallet: str, since_ts: int = 0,
                  until_ts: int | None = None) -> list[sqlite3.Row]:
    """One wallet's ingested trades, oldest first -- the replay's event source.

    Ascending, unlike every API here, because a replay has to move forward through time. Hits
    idx_trades_wallet_ts either way.
    """
    sql = "SELECT * FROM trades WHERE wallet=? AND ts>=?"
    args: tuple = (wallet.lower(), since_ts)
    if until_ts is not None:
        sql += " AND ts<=?"
        args += (until_ts,)
    return con.execute(sql + " ORDER BY ts, id", args).fetchall()


def settlement(con: sqlite3.Connection, token_id: str) -> sqlite3.Row | None:
    """When a token's market resolved and whether this token was the winner.

    Returns end_ts, resolved, and `won` as 1/0/NULL. `won` is NULL when the market resolved but
    we never learned which outcome index this token is -- a state that must stay distinguishable
    from a loss, because settling an unknown at zero would silently invent losses.
    """
    return con.execute(
        """SELECT m.condition_id, m.end_ts, m.resolved, m.winning_index, a.outcome_index,
                  CASE WHEN m.winning_index IS NULL OR a.outcome_index IS NULL THEN NULL
                       WHEN m.winning_index = a.outcome_index THEN 1 ELSE 0 END AS won
           FROM assets a JOIN markets m ON m.condition_id = a.condition_id
           WHERE a.token_id = ?""",
        (token_id,),
    ).fetchone()


def fee_source_split(con: sqlite3.Connection, run_id: int) -> dict:
    """How much of a run's fee bill was computed from a market's own schedule versus guessed
    from the category fallback. A backtest resting on guessed fees should say so."""
    rows = con.execute(
        """SELECT COALESCE(m.fee_source, 'unknown') AS src, COALESCE(SUM(o.fee), 0) AS fee
           FROM orders o LEFT JOIN markets m ON m.condition_id = o.condition_id
           WHERE o.run_id = ? GROUP BY src""",
        (run_id,),
    ).fetchall()
    return {r["src"]: float(r["fee"]) for r in rows}


def closed_positions_for(con: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM positions WHERE run_id=? AND open=0 ORDER BY closed_ts, id", (run_id,)
    ).fetchall()


def reduce_position(con: sqlite3.Connection, position_id: int, fraction: float,
                    proceeds_usd: float, exit_fee: float, reason: str,
                    ts: int) -> tuple[float, int | None]:
    """Sell part of an open position. Returns (realized_pnl_on_the_sold_part, kept_position_id).

    A partial exit does not fit one `positions` row, because a row carries exactly one
    `realized_pnl` and one `open` flag. Rather than bolt a second PnL column on, the row is
    split: the original is shrunk to the portion being sold and closed against the proceeds,
    and the remainder is written as a fresh open row carrying the same average price and the
    original `opened_ts`.

    Cost and fees are apportioned by the same fraction, so `avg_price` is unchanged on both
    halves -- which matters because avg_price is what a stop-loss measures against, and a
    partial exit must not silently move the stop.

    Doing it this way keeps `run_cash` correct without touching it: the sold part's proceeds
    arrive as a closed position's realized_pnl, and the kept part continues to tie up exactly
    its own share of the cost. Booking the partial anywhere else would leave the proceeds
    invisible to the cash calculation.

    A `fraction` at or above 1 is a full exit and is handled by `close_position` instead.
    """
    if not 0.0 < fraction < 1.0:
        raise ValueError(f"fraction must be strictly between 0 and 1, got {fraction}")
    row = con.execute(
        "SELECT * FROM positions WHERE id=? AND open=1", (position_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no open position {position_id}")

    sold_shares = row["shares"] * fraction
    sold_cost = row["cost_usd"] * fraction
    sold_fees = row["fees_paid"] * fraction
    keep_shares = row["shares"] - sold_shares
    keep_cost = row["cost_usd"] - sold_cost
    keep_fees = row["fees_paid"] - sold_fees

    pnl = proceeds_usd - sold_cost - sold_fees - exit_fee
    # Shrink the original to the sold portion and close it in one statement, so no other reader
    # can ever observe a position whose size and open flag disagree.
    con.execute(
        """UPDATE positions
           SET shares=?, cost_usd=?, fees_paid=?, open=0, closed_ts=?, close_reason=?,
               realized_pnl=?
           WHERE id=?""",
        (sold_shares, sold_cost, sold_fees + exit_fee, ts, reason, pnl, position_id),
    )
    kept_id = None
    if keep_shares > 0:
        cur = con.execute(
            """INSERT INTO positions
                   (run_id, token_id, condition_id, shares, avg_price, cost_usd, fees_paid,
                    opened_ts, open)
               VALUES (?,?,?,?,?,?,?,?,1)""",
            (row["run_id"], row["token_id"], row["condition_id"], keep_shares,
             row["avg_price"], keep_cost, keep_fees, row["opened_ts"]),
        )
        kept_id = int(cur.lastrowid)
    con.commit()
    return pnl, kept_id


def last_trade_ts_by_condition(con: sqlite3.Connection) -> dict[str, int]:
    """The last fill we ever observed in each market, as a proxy for when it stopped trading.

    Needed because nothing in the data says when a market actually resolved. `markets.end_ts`
    is gamma's *scheduled* end -- kickoff, or a nominal deadline -- and in-play markets trade
    straight through it, so on some wallets more than half the fills land after it. Settling a
    position at a timestamp earlier than the fill that opened it credits the outcome instantly,
    which is look-ahead of the worst kind: the backtest learns who won before it has held the
    position for a single second.

    The last trade in a market is a lower bound on when it closed, and it is drawn from data we
    already have. One scan of `trades` beats one query per position.
    """
    return {r[0]: int(r[1]) for r in con.execute(
        "SELECT condition_id, MAX(ts) FROM trades GROUP BY condition_id")}


# --- Read models for the web console -------------------------------------------------------
# Shaped for display rather than for the engine: each returns plain dicts, already joined, so
# the HTTP layer never builds SQL of its own.


def overview(con: sqlite3.Connection) -> dict:
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "wallets": q("SELECT COUNT(*) FROM wallets"),
        "screened": q("SELECT COUNT(*) FROM wallets WHERE selected=1"),
        "scored": q("SELECT COUNT(*) FROM trader_scores"),
        "markets": q("SELECT COUNT(*) FROM markets"),
        "trades": q("SELECT COUNT(*) FROM trades"),
        "tasks": q("SELECT COUNT(*) FROM tasks"),
        "runs": q("SELECT COUNT(*) FROM task_runs"),
        "open_positions": q("SELECT COUNT(*) FROM positions WHERE open=1"),
        "newest_trade_ts": q("SELECT COALESCE(MAX(ts),0) FROM trades"),
    }


def ranked_traders(con: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = con.execute(
        """SELECT ts.*, w.username, w.selected, w.screen_reason
           FROM trader_scores ts LEFT JOIN wallets w ON w.address = ts.address
           WHERE ts.rank_score IS NOT NULL
           ORDER BY ts.rank_score DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def task_overview(con: sqlite3.Connection) -> list[dict]:
    """Every task with the state of its most recent run folded in."""
    rows = con.execute(
        """SELECT t.*,
                  (SELECT r.id FROM task_runs r WHERE r.task=t.name
                    ORDER BY r.started_at DESC LIMIT 1) AS last_run_id,
                  (SELECT r.end_bankroll FROM task_runs r WHERE r.task=t.name
                    ORDER BY r.started_at DESC LIMIT 1) AS last_end_bankroll,
                  (SELECT r.stopped_at FROM task_runs r WHERE r.task=t.name
                    ORDER BY r.started_at DESC LIMIT 1) AS last_stopped_at,
                  (SELECT COUNT(*) FROM task_runs r WHERE r.task=t.name) AS run_count
           FROM tasks t ORDER BY t.updated_at DESC""").fetchall()
    return [dict(r) for r in rows]


def run_overview(con: sqlite3.Connection, limit: int = 50, task: str | None = None
                 ) -> list[dict]:
    sql = """SELECT r.*, t.trader, t.slippage, t.fixed_usd, t.buy_method,
                    (SELECT COUNT(*) FROM signals s WHERE s.run_id=r.id) AS signals,
                    (SELECT COUNT(*) FROM signals s WHERE s.run_id=r.id AND s.action='copied')
                        AS copied,
                    (SELECT COUNT(*) FROM positions p WHERE p.run_id=r.id AND p.open=0)
                        AS closed,
                    (SELECT COALESCE(SUM(p.realized_pnl),0) FROM positions p
                      WHERE p.run_id=r.id AND p.open=0) AS realized
             FROM task_runs r LEFT JOIN tasks t ON t.name = r.task"""
    args: tuple = ()
    if task:
        sql += " WHERE r.task=?"
        args = (task,)
    return [dict(x) for x in con.execute(sql + " ORDER BY r.started_at DESC, r.id DESC LIMIT ?",
                                         args + (limit,))]


def run_detail(con: sqlite3.Connection, run_id: int, limit: int = 200) -> dict:
    """Everything needed to audit one run: what we saw, what we did, what it cost."""
    run = con.execute(
        """SELECT r.*, t.trader, t.slippage, t.fixed_usd, t.max_concurrent, t.behavior
           FROM task_runs r LEFT JOIN tasks t ON t.name=r.task WHERE r.id=?""",
        (run_id,)).fetchone()
    if run is None:
        return {}
    return {
        "run": dict(run),
        "summary": run_summary(con, run_id),
        "skips": [{"reason": r, "count": n} for r, n in skip_reasons(con, run_id)],
        "positions": [dict(r) for r in con.execute(
            """SELECT p.*, m.question, m.slug FROM positions p
               LEFT JOIN markets m ON m.condition_id = p.condition_id
               WHERE p.run_id=? ORDER BY COALESCE(p.closed_ts, p.opened_ts) DESC LIMIT ?""",
            (run_id, limit))],
        "orders": [dict(r) for r in con.execute(
            "SELECT * FROM orders WHERE run_id=? ORDER BY ts DESC, id DESC LIMIT ?",
            (run_id, limit))],
        "signals": [dict(r) for r in con.execute(
            "SELECT * FROM signals WHERE run_id=? ORDER BY trader_ts DESC, id DESC LIMIT ?",
            (run_id, limit))],
    }


def stored_scores(con: sqlite3.Connection) -> list[dict]:
    """Every scored wallet's raw metrics, straight from `metrics_json`.

    Lets a ranking be recomputed without refetching anything. The weights and the calibration
    formula are judgement calls that get revised; the settled positions they are computed from
    do not, so re-deriving a rank from stored metrics costs nothing and rescoring 900 wallets
    over the network costs a quarter of an hour.
    """
    return [dict(r) for r in con.execute(
        "SELECT * FROM trader_scores WHERE metrics_json IS NOT NULL")]


def wallet_summary(con: sqlite3.Connection, address: str) -> dict:
    address = address.lower()
    w = con.execute("SELECT * FROM wallets WHERE address=?", (address,)).fetchone()
    t = con.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM trades WHERE wallet=?",
                    (address,)).fetchone()
    return {
        "wallet": dict(w) if w else None,
        "score": dict(get_trader_score(con, address) or {}) or None,
        "trades": t["n"], "first_ts": t["a"], "last_ts": t["b"],
    }
