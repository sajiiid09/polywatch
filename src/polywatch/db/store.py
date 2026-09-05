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
        """INSERT INTO markets (condition_id, question, slug, category, closed, active, archived,
                start_ts, end_ts, outcomes_json, prices_json, uma_status_json, resolved,
                winning_index, neg_risk, fees_enabled, fee_type, fee_rate, fee_exponent,
                fee_taker_only, fee_rebate_rate, fee_source, fetched_at)
           VALUES (:condition_id,:question,:slug,:category,:closed,:active,:archived,:start_ts,
                :end_ts,:outcomes_json,:prices_json,:uma_status_json,:resolved,:winning_index,
                :neg_risk,:fees_enabled,:fee_type,:fee_rate,:fee_exponent,:fee_taker_only,
                :fee_rebate_rate,:fee_source,:fetched_at)
           ON CONFLICT(condition_id) DO UPDATE SET
                question=excluded.question, slug=excluded.slug, category=excluded.category,
                closed=excluded.closed, active=excluded.active, archived=excluded.archived,
                start_ts=excluded.start_ts, end_ts=excluded.end_ts,
                outcomes_json=excluded.outcomes_json, prices_json=excluded.prices_json,
                uma_status_json=excluded.uma_status_json, resolved=excluded.resolved,
                winning_index=excluded.winning_index, neg_risk=excluded.neg_risk,
                fees_enabled=excluded.fees_enabled, fee_type=excluded.fee_type,
                fee_rate=excluded.fee_rate, fee_exponent=excluded.fee_exponent,
                fee_taker_only=excluded.fee_taker_only, fee_rebate_rate=excluded.fee_rebate_rate,
                fee_source=excluded.fee_source, fetched_at=excluded.fetched_at""",
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
