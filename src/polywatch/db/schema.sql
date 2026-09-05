-- polywatch storage. SQLite, one file, read-only-derived data only.
--
-- Hot query this schema is shaped around:
--   SELECT price FROM prices WHERE token_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1
-- run once per trade per lag value (5M trades x 15 lags in the worst case). `prices` is
-- WITHOUT ROWID so the primary key IS the row: one B-tree seek, no second lookup.

CREATE TABLE IF NOT EXISTS wallets (
    address        TEXT PRIMARY KEY,
    source         TEXT NOT NULL,          -- 'leaderboard' | 'control'
    rank           INTEGER,
    username       TEXT,
    vol            REAL,
    pnl            REAL,
    trades_from_ts INTEGER,                -- oldest trade ts ingested, for resume
    selected       INTEGER,                -- 1 = passed screening, 0 = rejected, NULL = unscreened
    screen_reason  TEXT,                   -- why a wallet was rejected, kept for audit
    profile_json   TEXT,                   -- behavioural fingerprint at screening time
    fetched_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS markets (
    condition_id     TEXT PRIMARY KEY,
    question         TEXT,
    slug             TEXT,
    category         TEXT,                 -- gamma's topical label; NOT the fee category
    closed           INTEGER,
    active           INTEGER,
    archived         INTEGER,
    start_ts         INTEGER,
    end_ts           INTEGER,
    outcomes_json    TEXT,                 -- gamma ships these as JSON-encoded STRINGS
    prices_json      TEXT,
    uma_status_json  TEXT,
    resolved         INTEGER NOT NULL DEFAULT 0,
    winning_index    INTEGER,              -- index into outcomes_json, NULL unless resolved
    neg_risk         INTEGER,
    fees_enabled     INTEGER,
    fee_type         TEXT,                 -- e.g. 'sports_fees_v3', 'politics_fees'
    fee_rate         REAL,                 -- from the market's own feeSchedule when present
    fee_exponent     REAL,
    fee_taker_only   INTEGER,
    fee_rebate_rate  REAL,
    fee_source       TEXT,                 -- 'market' | 'fallback' -- so Step 4 can flag guesses
    fetched_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    token_id      TEXT PRIMARY KEY,        -- CLOB token id, the key prices-history takes
    condition_id  TEXT NOT NULL,
    outcome_index INTEGER NOT NULL,
    outcome       TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_condition ON assets(condition_id);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY,
    wallet        TEXT NOT NULL,
    token_id      TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    side          TEXT NOT NULL,           -- BUY | SELL
    size          REAL NOT NULL,           -- shares
    price         REAL NOT NULL,           -- 0..1, the implied probability of `outcome`
    ts            INTEGER NOT NULL,
    outcome       TEXT,
    outcome_index INTEGER,
    tx_hash       TEXT,
    -- One transaction can carry several fills, so tx_hash alone is not unique. This tuple is
    -- what makes re-ingestion idempotent.
    UNIQUE (wallet, token_id, ts, side, size, price, tx_hash)
);
CREATE INDEX IF NOT EXISTS idx_trades_wallet_ts ON trades(wallet, ts);
CREATE INDEX IF NOT EXISTS idx_trades_token_ts ON trades(token_id, ts);
CREATE INDEX IF NOT EXISTS idx_trades_condition ON trades(condition_id);

CREATE TABLE IF NOT EXISTS prices (
    token_id TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    price    REAL NOT NULL,
    PRIMARY KEY (token_id, ts)
) WITHOUT ROWID;

-- Which [start, end] spans have actually been fetched per asset. Without this, "no rows in that
-- range" is ambiguous between "not fetched yet" and "no quotes existed", and resume would refetch.
CREATE TABLE IF NOT EXISTS price_windows (
    token_id   TEXT NOT NULL,
    start_ts   INTEGER NOT NULL,
    end_ts     INTEGER NOT NULL,
    points     INTEGER NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (token_id, start_ts, end_ts)
);

-- Every HTTP call, so schema drift and rate-limit trouble are auditable after the fact.
CREATE TABLE IF NOT EXISTS ingest_log (
    id       INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL,
    url      TEXT NOT NULL,
    status   INTEGER,
    bytes    INTEGER,
    records  INTEGER,
    attempts INTEGER,
    error    TEXT,
    raw_path TEXT,
    ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingest_log_kind_ts ON ingest_log(kind, ts);
