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
                      ("accepting_orders", "INTEGER"), ("enable_order_book", "INTEGER"),
                      ("neg_risk_id", "TEXT")):
        if col not in have:
            con.execute(f"ALTER TABLE markets ADD COLUMN {col} {decl}")
    # ...and for databases written before a partially-sold position had anywhere to put the fees
    # it had already expensed. Defaulting to 0 is right for old rows: nothing before this column
    # existed could record a partial exit in the first place.
    have = {r["name"] for r in con.execute("PRAGMA table_info(positions)")}
    for col, decl in (("fees_realized", "REAL NOT NULL DEFAULT 0"),):
        if col not in have:
            con.execute(f"ALTER TABLE positions ADD COLUMN {col} {decl}")
    # ...and before an order kept a thread back to the exchange, which is what reconciliation
    # pulls on when a response did not say what the order matched.
    have = {r["name"] for r in con.execute("PRAGMA table_info(orders)")}
    for col, decl in (("exchange_id", "TEXT"),):
        if col not in have:
            con.execute(f"ALTER TABLE orders ADD COLUMN {col} {decl}")
    # ...and before a signal recorded when its page arrived, which is what separates the feed's
    # lag from the loop's. Old rows keep NULL and the report falls back to the combined figure.
    have = {r["name"] for r in con.execute("PRAGMA table_info(signals)")}
    for col, decl in (("fetch_ts", "INTEGER"),):
        if col not in have:
            con.execute(f"ALTER TABLE signals ADD COLUMN {col} {decl}")
    have = {r["name"] for r in con.execute("PRAGMA table_info(market_meta)")}
    for col, decl in (("neg_risk_id", "TEXT"),):
        if col not in have:
            con.execute(f"ALTER TABLE market_meta ADD COLUMN {col} {decl}")
    # ...and before a score card could say whether the wallet's edge survives being copied.
    # ...and before a run could copy more than one wallet at a time.
    for table in ("positions", "orders"):
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if "trader" not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN trader TEXT")
    have = {r["name"] for r in con.execute("PRAGMA table_info(trader_scores)")}
    for col, decl in (("capture_ratio", "REAL"), ("edge_half_life_s", "REAL"),
                      ("hold_p50_s", "INTEGER"), ("fee_adjusted_roi", "REAL"),
                      ("recent_roi", "REAL"), ("luck_p", "REAL"), ("excluded", "TEXT")):
        if col not in have:
            con.execute(f"ALTER TABLE trader_scores ADD COLUMN {col} {decl}")
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


# --- the traders a task copies --------------------------------------------


def set_task_traders(con: sqlite3.Connection, task: str, rows: list[dict]) -> int:
    """Replace a task's roster. Editing a task's traders is a deliberate act, not a merge."""
    now = int(time.time())
    con.execute("DELETE FROM task_traders WHERE task=?", (task,))
    con.executemany(
        """INSERT INTO task_traders (task, address, weight, rank_score, active, added_ts)
           VALUES (?,?,?,?,1,?)""",
        [(task, r["address"].lower(), r.get("weight", 1.0), r.get("rank_score"), now)
         for r in rows])
    con.commit()
    return len(rows)


def task_traders(con: sqlite3.Connection, task: str, active_only: bool = False
                 ) -> list[sqlite3.Row]:
    sql = "SELECT * FROM task_traders WHERE task=?"
    if active_only:
        sql += " AND active=1"
    return con.execute(sql + " ORDER BY weight DESC, address", (task,)).fetchall()


def drop_task_trader(con: sqlite3.Connection, task: str, address: str, reason: str) -> None:
    """Stop copying one wallet. Their open positions are still managed to the end of the run --
    dropping a trader is a decision to stop taking their advice, not to abandon their trades."""
    con.execute(
        "UPDATE task_traders SET active=0, dropped_reason=? WHERE task=? AND address=?",
        (reason, task, address.lower()))
    con.commit()


def trader_pnl(con: sqlite3.Connection, run_id: int) -> dict[str, dict]:
    """Per-trader attribution for one run: what each wallet's signals actually earned.

    The point of copying several traders is being able to tell them apart afterwards. Without
    this a portfolio run reports one number and the bad wallet hides inside it.
    """
    out: dict[str, dict] = {}
    for r in con.execute(
        """SELECT COALESCE(trader,'?') t, COUNT(*) n,
                  COALESCE(SUM(realized_pnl),0) pnl,
                  COALESCE(SUM(fees_paid),0) + COALESCE(SUM(fees_realized),0) fees,
                  SUM(CASE WHEN open=1 THEN 1 ELSE 0 END) still_open,
                  COALESCE(SUM(CASE WHEN open=1 THEN cost_usd + fees_paid ELSE 0 END),0) tied
           FROM positions WHERE run_id=? GROUP BY COALESCE(trader,'?')""", (run_id,)):
        out[r[0]] = {"positions": int(r[1]), "realized_pnl": float(r[2]),
                     "fees": float(r[3]), "open": int(r[4]), "exposure": float(r[5]),
                     "signals": 0, "copied": 0}
    for r in con.execute(
        """SELECT trader, COUNT(*), SUM(CASE WHEN action='copied' THEN 1 ELSE 0 END)
           FROM signals WHERE run_id=? GROUP BY trader""", (run_id,)):
        row = out.setdefault(r[0], {"positions": 0, "realized_pnl": 0.0, "fees": 0.0,
                                    "open": 0, "exposure": 0.0, "signals": 0, "copied": 0})
        row["signals"] = int(r[1])
        row["copied"] = int(r[2] or 0)
    return out


def trader_exposure(con: sqlite3.Connection, run_id: int, trader: str) -> float:
    """Open cost across every position one trader's signals opened."""
    row = con.execute(
        """SELECT COALESCE(SUM(cost_usd + fees_paid), 0) FROM positions
           WHERE run_id=? AND trader=? AND open=1""", (run_id, trader.lower())).fetchone()
    return float(row[0])


def trader_realized(con: sqlite3.Connection, run_id: int, trader: str) -> float:
    row = con.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE run_id=? AND trader=?",
        (run_id, trader.lower())).fetchone()
    return float(row[0])


def start_run(con: sqlite3.Connection, task: str, mode: str, bankroll: float) -> int:
    cur = con.execute(
        "INSERT INTO task_runs (task, mode, started_at, start_bankroll) VALUES (?,?,?,?)",
        (task, mode, int(time.time()), bankroll),
    )
    con.commit()
    return int(cur.lastrowid)


def finish_run(con: sqlite3.Connection, run_id: int, end_bankroll: float, reason: str) -> None:
    con.execute(
        "UPDATE task_runs SET stopped_at=?, end_bankroll=?, stop_reason=? WHERE id=?",
        (int(time.time()), end_bankroll, reason, run_id),
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
    row.setdefault("fetch_ts", row["seen_ts"])
    cur = con.execute(
        """INSERT OR IGNORE INTO signals
               (run_id, trader, kind, tx_hash, token_id, condition_id, side, size, price,
                usdc_size, trader_ts, fetch_ts, seen_ts, action, reason)
           VALUES (:run_id,:trader,:kind,:tx_hash,:token_id,:condition_id,:side,:size,:price,
                :usdc_size,:trader_ts,:fetch_ts,:seen_ts,:action,:reason)""",
        row,
    )
    con.commit()
    return int(cur.lastrowid) if cur.rowcount else None


def signals(con: sqlite3.Connection, run_id: int, action: str | None = None
            ) -> list[sqlite3.Row]:
    sql = "SELECT * FROM signals WHERE run_id=?"
    args: tuple = (run_id,)
    if action is not None:
        sql += " AND action=?"
        args += (action,)
    return con.execute(sql + " ORDER BY seen_ts, id", args).fetchall()


def trader_observed_shares(con: sqlite3.Connection, run_id: int, trader: str, token_id: str,
                           before_ts: int | None = None) -> float:
    """Shares of one token the target has accumulated *as far as this run has watched them*.

    Buys minus sells across every signal recorded for them, copied or skipped -- which is why
    skipped signals are recorded at all. It is a floor, not their position: anything they were
    holding before the run started is invisible to it.

    That floor is the safe direction. Used to size a proportional follow-exit, underestimating
    what they hold overestimates the fraction they just sold, so we exit at least as much as they
    did. The failure mode is the old behaviour -- selling everything -- not selling too little.
    """
    sql = """SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN size ELSE -size END), 0)
             FROM signals WHERE run_id=? AND trader=? AND token_id=? AND kind='TRADE'
                          AND side IS NOT NULL"""
    args: tuple = (run_id, trader, token_id)
    if before_ts is not None:
        sql += " AND trader_ts < ?"
        args += (before_ts,)
    return float(con.execute(sql, args).fetchone()[0])


def skip_reasons(con: sqlite3.Connection, run_id: int) -> list[tuple[str, int]]:
    """Why we passed on trades, most common first. The main output of a paper run."""
    return [(r[0], r[1]) for r in con.execute(
        """SELECT reason, COUNT(*) FROM signals
           WHERE run_id=? AND action='skipped' GROUP BY reason ORDER BY COUNT(*) DESC""",
        (run_id,),
    )]


def insert_order(con: sqlite3.Connection, row: dict) -> int:
    row.setdefault("ts", int(time.time()))
    row.setdefault("exchange_id", None)
    row.setdefault("trader", None)
    cur = con.execute(
        """INSERT INTO orders
               (run_id, signal_id, trader, mode, token_id, condition_id, side, intent_usd,
                limit_price, book_vwap, filled_shares, avg_price, fee, status, exchange_id,
                reason, ts)
           VALUES (:run_id,:signal_id,:trader,:mode,:token_id,:condition_id,:side,:intent_usd,
                :limit_price,:book_vwap,:filled_shares,:avg_price,:fee,:status,:exchange_id,
                :reason,:ts)""",
        row,
    )
    con.commit()
    return int(cur.lastrowid)


def orders(con: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM orders WHERE run_id=? ORDER BY ts, id",
                       (run_id,)).fetchall()


def unknown_orders(con: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    """Orders the exchange accepted without saying what they matched.

    Each one of these is a question the database cannot answer on its own, and every one of them
    must be closed out by reconciliation before the run is allowed to keep trading.
    """
    return con.execute(
        "SELECT * FROM orders WHERE run_id=? AND status='unknown' ORDER BY ts, id",
        (run_id,)).fetchall()


def resolve_order(con: sqlite3.Connection, order_id: int, status: str, shares: float,
                  avg_price: float | None, fee: float, reason: str) -> None:
    """Write back what an order turned out to have done, once the exchange has been asked."""
    con.execute(
        """UPDATE orders SET status=?, filled_shares=?, avg_price=?, fee=?, reason=?
           WHERE id=?""",
        (status, shares, avg_price, fee, reason, order_id))
    con.commit()


def recorded_shares(con: sqlite3.Connection, run_id: int, token_id: str) -> float:
    """Shares of one token this run believes it is holding."""
    row = con.execute(
        """SELECT COALESCE(SUM(shares), 0) FROM positions
           WHERE run_id=? AND token_id=? AND open=1""", (run_id, token_id)).fetchone()
    return float(row[0])


def open_position(con: sqlite3.Connection, row: dict) -> int:
    row.setdefault("opened_ts", int(time.time()))
    row.setdefault("trader", None)
    cur = con.execute(
        """INSERT INTO positions
               (run_id, trader, token_id, condition_id, shares, avg_price, cost_usd, fees_paid,
                opened_ts, open)
           VALUES (:run_id,:trader,:token_id,:condition_id,:shares,:avg_price,:cost_usd,
                :fees_paid,:opened_ts,1)""",
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


def settle_position(con: sqlite3.Connection, position_id: int, shares_sold: float,
                    proceeds_usd: float, exit_fee: float, reason: str) -> tuple[float, bool]:
    """Realize all or part of a position. Returns (pnl of the sold portion, position closed).

    Two columns carry fees, and the split is the whole point of this function:

      * `fees_paid` is the entry fee attributable to the shares *still held*. It is what
        `exits.mark` adds to `cost_usd` to get the cost basis a stop-loss is measured against,
        and what `run_cash` treats as tied up.
      * `fees_realized` is everything already expensed against realized PnL -- entry fees on
        shares that have been sold, plus every exit fee.

    Scaling `fees_paid` down by the sold fraction is not cosmetic. Leaving the whole entry fee on
    a remainder makes that remainder's cost basis larger than it ever was, so the stop-loss fires
    against a loss that is partly fictional and `run_cash` reports less money than the run has.

    A partial sale leaves the position open with the remainder, its PnL banked, and the ladder
    free to try again on the next tick.
    """
    row = con.execute(
        "SELECT shares, cost_usd, fees_paid, realized_pnl FROM positions WHERE id=?",
        (position_id,)).fetchone()
    if row is None:
        raise KeyError(f"no position {position_id}")
    held = row["shares"]
    if held <= 0:
        raise ValueError(f"position {position_id} holds no shares")

    closed = shares_sold >= held - 1e-6
    frac = 1.0 if closed else shares_sold / held
    part_cost = row["cost_usd"] * frac
    part_fees = row["fees_paid"] * frac
    pnl = proceeds_usd - part_cost - part_fees - exit_fee
    banked = (row["realized_pnl"] or 0.0) + pnl

    if closed:
        con.execute(
            """UPDATE positions
               SET open=0, closed_ts=?, close_reason=?, realized_pnl=?,
                   cost_usd=0, fees_paid=0, fees_realized=fees_realized+?
               WHERE id=?""",
            (int(time.time()), reason, banked, part_fees + exit_fee, position_id))
    else:
        con.execute(
            """UPDATE positions
               SET shares=shares-?, cost_usd=cost_usd-?, fees_paid=fees_paid-?,
                   fees_realized=fees_realized+?, realized_pnl=?
               WHERE id=?""",
            (shares_sold, part_cost, part_fees, part_fees + exit_fee, banked, position_id))
    con.commit()
    return pnl, closed


def close_position(con: sqlite3.Connection, position_id: int, proceeds_usd: float,
                   exit_fee: float, reason: str) -> float:
    """Settle a whole position and return its realized PnL. Thin wrapper over settle_position."""
    row = con.execute("SELECT shares FROM positions WHERE id=?", (position_id,)).fetchone()
    if row is None:
        raise KeyError(f"no position {position_id}")
    pnl, _ = settle_position(con, position_id, row["shares"], proceeds_usd, exit_fee, reason)
    return pnl


def open_positions(con: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM positions WHERE run_id=? AND open=1 ORDER BY opened_ts", (run_id,)
    ).fetchall()


def open_position_for(con: sqlite3.Connection, run_id: int, token_id: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM positions WHERE run_id=? AND token_id=? AND open=1", (run_id, token_id)
    ).fetchone()


def open_position_other_outcome(con: sqlite3.Connection, run_id: int, condition_id: str,
                                token_id: str) -> sqlite3.Row | None:
    """An open position on a DIFFERENT outcome of the same market.

    Holding YES and NO of one market pays two entry fees and two exit fees to end up with
    approximately no exposure, so this is the query behind a gate rather than a report.
    """
    return con.execute(
        """SELECT * FROM positions
           WHERE run_id=? AND condition_id=? AND token_id<>? AND open=1 LIMIT 1""",
        (run_id, condition_id, token_id)).fetchone()


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


def event_exposure(con: sqlite3.Connection, run_id: int, neg_risk_id: str) -> float:
    """Open cost across every market belonging to one neg-risk event.

    The outcomes of a neg-risk event are mutually exclusive slices of a single question. Holding
    three of them is one view expressed three times, and a cap applied per market would let it
    through three times over.
    """
    row = con.execute(
        """SELECT COALESCE(SUM(p.cost_usd + p.fees_paid), 0) FROM positions p
           JOIN market_meta m ON m.condition_id = p.condition_id
           WHERE p.run_id=? AND p.open=1 AND m.neg_risk_id=?""",
        (run_id, neg_risk_id)).fetchone()
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
    #
    # Summed over every position, not just closed ones: a position that has been partially sold
    # is still open and has already banked the PnL of the part that went.
    realized = con.execute(
        """SELECT COALESCE(SUM(realized_pnl), 0) FROM positions
           WHERE run_id=?""", (run_id,)).fetchone()[0]
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
        # Both columns: `fees_paid` is what open shares still carry, `fees_realized` is what
        # has already been expensed. Their sum is what the run has actually paid the exchange.
        "fees_paid": q("""SELECT COALESCE(SUM(fees_paid),0) + COALESCE(SUM(fees_realized),0)
                          FROM positions WHERE run_id=?"""),
        "realized_pnl": q(
            "SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE run_id=?"),
        "cash": run_cash(con, run_id),
    }


TRADER_SCORE_COLUMNS = ("address", "scanned_at", "n_closed", "win_rate", "roi", "realized_pnl",
                        "brier", "avg_entry_price", "avg_stake_usd", "max_drawdown",
                        "consistency", "est_account_usd", "top_category", "persona_fit",
                        "rank_score", "capture_ratio", "edge_half_life_s", "hold_p50_s",
                        "fee_adjusted_roi", "recent_roi", "luck_p", "excluded", "metrics_json")


def upsert_trader_score(con: sqlite3.Connection, row: dict) -> None:
    row.setdefault("scanned_at", int(time.time()))
    for c in TRADER_SCORE_COLUMNS:
        row.setdefault(c, None)
    cols = ",".join(TRADER_SCORE_COLUMNS)
    binds = ",".join(f":{c}" for c in TRADER_SCORE_COLUMNS)
    sets = ",".join(f"{c}=excluded.{c}" for c in TRADER_SCORE_COLUMNS if c != "address")
    con.execute(f"INSERT INTO trader_scores ({cols}) VALUES ({binds}) "
                f"ON CONFLICT(address) DO UPDATE SET {sets}", row)
    con.commit()


def get_trader_score(con: sqlite3.Connection, address: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM trader_scores WHERE address=?",
                       (address.lower(),)).fetchone()


def top_trader_scores(con: sqlite3.Connection, limit: int = 30,
                      include_excluded: bool = False) -> list[sqlite3.Row]:
    """The shortlist, best first. Excluded wallets are held back unless asked for.

    An excluded wallet is not a near-miss to be considered anyway -- it failed a hard gate, such
    as an edge that does not survive being copied. Seeing them is useful for understanding a
    sweep; ranking them alongside candidates is not.
    """
    sql = "SELECT * FROM trader_scores WHERE rank_score IS NOT NULL"
    if not include_excluded:
        sql += " AND excluded IS NULL"
    return con.execute(sql + " ORDER BY rank_score DESC LIMIT ?", (limit,)).fetchall()


def upsert_trader_replay(con: sqlite3.Connection, address: str, rows: list[dict]) -> int:
    """Replace one wallet's whole decay curve. A rescan overwrites cleanly."""
    now = int(time.time())
    con.execute("DELETE FROM trader_replay WHERE address=?", (address.lower(),))
    con.executemany(
        """INSERT INTO trader_replay
               (address, lag_s, n, n_missing, copier_roi, copier_pnl, win_rate, scanned_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        [(address.lower(), r["lag_s"], r["n"], r["n_missing"], r["copier_roi"],
          r["copier_pnl"], r["win_rate"], now) for r in rows])
    con.commit()
    return len(rows)


def trader_replay(con: sqlite3.Connection, address: str) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM trader_replay WHERE address=? ORDER BY lag_s",
                       (address.lower(),)).fetchall()


def trades_for(con: sqlite3.Connection, address: str, since_ts: int | None = None
               ) -> list[sqlite3.Row]:
    """One wallet's fills, oldest first -- the raw material for round-trip matching."""
    sql = "SELECT * FROM trades WHERE wallet=?"
    args: tuple = (address.lower(),)
    if since_ts is not None:
        sql += " AND ts >= ?"
        args += (since_ts,)
    return con.execute(sql + " ORDER BY ts", args).fetchall()


def fee_rate_by_condition(con: sqlite3.Connection) -> dict[str, tuple[float | None, str | None]]:
    """{condition_id: (fee_rate, category)} for every market we have ingested.

    One query rather than one per position: replay charges a fee on both legs of every round
    trip, and looking each rate up individually turns a scoring pass into a few thousand queries.
    """
    return {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT condition_id, fee_rate, fee_type FROM markets")}


MARKET_META_COLUMNS = ("condition_id", "tick_size", "min_order_size", "accepting_orders",
                       "enable_order_book", "neg_risk", "neg_risk_id", "category_derived",
                       "fee_rate", "end_ts", "fetched_at")


def upsert_market_meta(con: sqlite3.Connection, row: dict) -> None:
    row.setdefault("fetched_at", int(time.time()))
    for c in MARKET_META_COLUMNS:
        row.setdefault(c, None)
    cols = ",".join(MARKET_META_COLUMNS)
    binds = ",".join(f":{c}" for c in MARKET_META_COLUMNS)
    sets = ",".join(f"{c}=excluded.{c}" for c in MARKET_META_COLUMNS if c != "condition_id")
    con.execute(f"INSERT INTO market_meta ({cols}) VALUES ({binds}) "
                f"ON CONFLICT(condition_id) DO UPDATE SET {sets}", row)
    con.commit()


def get_market_meta(con: sqlite3.Connection, condition_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM market_meta WHERE condition_id=?",
                       (condition_id,)).fetchone()


# --- resting exit orders --------------------------------------------------
# A GTC sell left on the book. In live mode this outlives the process, which is the whole point
# of it and also the reason it is a table rather than a variable: the next run has to be able to
# find orders the last one left behind, and either adopt or cancel them.

RESTING_COLUMNS = ("run_id", "position_id", "mode", "token_id", "condition_id", "side",
                   "shares", "price", "kind", "exchange_id", "status", "filled_shares",
                   "avg_price", "reason", "placed_ts", "settled_ts")


def insert_resting(con: sqlite3.Connection, row: dict) -> int:
    row.setdefault("placed_ts", int(time.time()))
    row.setdefault("status", "open")
    row.setdefault("filled_shares", 0.0)
    for c in RESTING_COLUMNS:
        row.setdefault(c, None)
    cols = ",".join(RESTING_COLUMNS)
    binds = ",".join(f":{c}" for c in RESTING_COLUMNS)
    cur = con.execute(f"INSERT INTO resting_orders ({cols}) VALUES ({binds})", row)
    con.commit()
    return int(cur.lastrowid)


def open_resting(con: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM resting_orders WHERE run_id=? AND status='open' ORDER BY placed_ts",
        (run_id,)).fetchall()


def resting_for_position(con: sqlite3.Connection, position_id: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM resting_orders WHERE position_id=? AND status='open'",
        (position_id,)).fetchall()


def settle_resting(con: sqlite3.Connection, resting_id: int, status: str,
                   filled_shares: float = 0.0, avg_price: float | None = None,
                   reason: str | None = None) -> None:
    con.execute(
        """UPDATE resting_orders
           SET status=?, filled_shares=?, avg_price=?, reason=?, settled_ts=?
           WHERE id=?""",
        (status, filled_shares, avg_price, reason, int(time.time()), resting_id),
    )
    con.commit()


def orphan_resting(con: sqlite3.Connection, mode: str = "live") -> list[sqlite3.Row]:
    """Live orders still marked open on the book from a run that has already stopped.

    A killed process leaves real orders resting on a real exchange. They are not a bug to be
    hidden -- a take-profit that fills after the bot exits is the feature working -- but the
    next run has to be told about them rather than discovering the shares are gone.
    """
    return con.execute(
        """SELECT r.* FROM resting_orders r JOIN task_runs t ON t.id = r.run_id
           WHERE r.status='open' AND r.mode=? AND t.stopped_at IS NOT NULL
           ORDER BY r.placed_ts""", (mode,)).fetchall()


def latency_split(con: sqlite3.Connection, run_id: int) -> dict:
    """Where copy latency actually comes from, averaged over a run.

    `feed` is fetch_ts - trader_ts: how stale the activity endpoint's answer was before we ever
    saw it, which no amount of polling faster can fix. `loop` is seen_ts - fetch_ts: our own
    processing, which is the only half worth optimising. Rows written before fetch_ts existed
    report loop=0 and are honest about it by carrying the whole gap in `feed`.
    """
    row = con.execute(
        """SELECT COUNT(*),
                  AVG(COALESCE(fetch_ts, seen_ts) - trader_ts),
                  AVG(seen_ts - COALESCE(fetch_ts, seen_ts))
           FROM signals WHERE run_id=?""", (run_id,)).fetchone()
    if not row or not row[0]:
        return {"n": 0}
    return {"n": int(row[0]), "feed": float(row[1] or 0.0), "loop": float(row[2] or 0.0)}


def latency_samples(con: sqlite3.Connection, run_id: int | None = None) -> list[int]:
    """seen_ts - trader_ts for every signal: how late we were, in seconds.

    This is measured, not assumed, and it is the number that decides whether copying a fast
    trader is possible at all. Both columns have always been on `signals` for exactly this.
    """
    sql = "SELECT seen_ts - trader_ts FROM signals"
    args: tuple = ()
    if run_id is not None:
        sql += " WHERE run_id=?"
        args = (run_id,)
    return [int(r[0]) for r in con.execute(sql, args) if r[0] is not None]


def latency_stats(con: sqlite3.Connection, run_id: int | None = None) -> dict:
    """Percentiles of copy latency. Nearest-rank, so p50 of an even sample is a real
    observation rather than an average of two that never happened."""
    xs = sorted(latency_samples(con, run_id))
    if not xs:
        return {"n": 0}

    def pct(p: float) -> int:
        return xs[min(len(xs) - 1, max(0, int(round(p / 100 * len(xs) + 0.5)) - 1))]

    return {"n": len(xs), "min": xs[0], "p50": pct(50), "p90": pct(90), "p99": pct(99),
            "max": xs[-1], "mean": sum(xs) / len(xs)}
