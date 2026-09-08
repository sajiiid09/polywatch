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
    gamma_id         TEXT,                 -- gamma's numeric id; what /markets/{id}/tags takes
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
    neg_risk_id      TEXT,                 -- shared by every outcome of one neg-risk event
    fees_enabled     INTEGER,
    fee_type         TEXT,                 -- e.g. 'sports_fees_v3', 'politics_fees'
    fee_rate         REAL,                 -- from the market's own feeSchedule when present
    fee_exponent     REAL,
    fee_taker_only   INTEGER,
    fee_rebate_rate  REAL,
    fee_source       TEXT,                 -- 'market' | 'fallback' -- so Step 4 can flag guesses
    -- Execution constraints, needed only by the trading side but shipped free with every
    -- gamma market response, so they are stored here rather than fetched again later.
    tick_size         REAL,                -- 0.1 | 0.01 | 0.001 | 0.0001, per market
    order_min_size    REAL,                -- shares; commonly 5
    accepting_orders  INTEGER,             -- goes false well before a market resolves
    enable_order_book INTEGER,
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

-- ---------------------------------------------------------------------------------------
-- Copy trading. Everything below is written by the bot, not by ingestion.
--
-- The design rule here is that a run must be reconstructable afterwards from this database
-- alone: every trade the target made that we saw, whether we copied it, and if not, why not.
-- A skipped signal is as much of a result as a filled order.

-- One row per named task. `config_json` is the whole Task dataclass; the columns beside it are
-- duplicates, denormalised so that `task list` and ad-hoc SQL do not have to parse JSON.
CREATE TABLE IF NOT EXISTS tasks (
    name            TEXT PRIMARY KEY,
    trader          TEXT NOT NULL,          -- the wallet being copied
    mode            TEXT NOT NULL,          -- 'paper' | 'live'
    bankroll        REAL NOT NULL,
    buy_method      TEXT NOT NULL,          -- 'fixed' | 'mirror'
    fixed_usd       REAL,                   -- used when buy_method='fixed'
    max_market_usd  REAL NOT NULL,          -- ceiling on total exposure to one market
    max_concurrent  INTEGER NOT NULL,
    slippage        REAL NOT NULL,          -- 0.07 = accept 7% worse than the book's VWAP
    sl_kind         TEXT,                   -- 'pct' | 'price' | NULL for none
    sl_value        REAL,
    tp_kind         TEXT,
    tp_value        REAL,
    behavior        TEXT NOT NULL,          -- 'buys' | 'buys_sells'
    risk            TEXT NOT NULL,          -- 'conservative' | 'moderate'
    category        TEXT,                   -- discovery preference only; does NOT block copies
    style           TEXT NOT NULL,          -- 'safe_and_steady' | 'value_hunter' | 'momentum'
    hold            TEXT NOT NULL,          -- 'quick_flips' | 'hours'
    activity        TEXT NOT NULL,          -- 'casual' | 'active'
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    config_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    id             INTEGER PRIMARY KEY,
    task           TEXT NOT NULL REFERENCES tasks(name),
    mode           TEXT NOT NULL,
    started_at     INTEGER NOT NULL,
    stopped_at     INTEGER,
    start_bankroll REAL NOT NULL,
    end_bankroll   REAL,
    stop_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task, started_at);

-- Every action of the target we observed. `action` is 'copied' or 'skipped' and `reason` names
-- the gate that rejected it, which is the main thing a paper run is run to find out.
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES task_runs(id),
    trader       TEXT NOT NULL,
    kind         TEXT NOT NULL,             -- TRADE | REDEEM | MERGE | SPLIT | ...
    tx_hash      TEXT NOT NULL,
    token_id     TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    side         TEXT,                      -- BUY | SELL, absent on non-trade events
    size         REAL,                      -- shares the target moved
    price        REAL,
    usdc_size    REAL,
    trader_ts    INTEGER NOT NULL,          -- when they traded
    -- Three timestamps, because "we were late" has two causes and only one of them is ours.
    -- fetch_ts - trader_ts is the feed's own lag (data-api caches /activity); seen_ts - fetch_ts
    -- is this loop's. Tuning the poll interval from their sum cannot tell which one moved.
    fetch_ts     INTEGER,                   -- when the response carrying this event landed
    seen_ts      INTEGER NOT NULL,          -- when we acted on it; the difference is copy latency
    action       TEXT NOT NULL,             -- 'copied' | 'skipped'
    reason       TEXT,
    -- One transaction can carry several fills, same as `trades`. This tuple is what makes the
    -- poller idempotent when consecutive polls overlap, which they always do.
    UNIQUE (run_id, tx_hash, token_id, side, size)
);
CREATE INDEX IF NOT EXISTS idx_signals_run ON signals(run_id, seen_ts);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES task_runs(id),
    signal_id     INTEGER REFERENCES signals(id),   -- NULL for our own exits
    mode          TEXT NOT NULL,            -- 'paper' | 'live'
    token_id      TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    side          TEXT NOT NULL,
    intent_usd    REAL NOT NULL,            -- what we meant to spend
    limit_price   REAL NOT NULL,            -- slippage-bounded, tick-rounded
    book_vwap     REAL,                     -- the book's price before our slippage allowance
    filled_shares REAL NOT NULL DEFAULT 0,
    avg_price     REAL,
    fee           REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,            -- 'filled' | 'partial' | 'rejected' | 'unknown'
    -- The CLOB's order id. Only reconciliation needs it, and only when `status` is 'unknown' --
    -- but that is precisely the case where the row is the only thread back to the order, so it
    -- is recorded on every live order rather than on the ones we later wish we had it for.
    exchange_id   TEXT,
    reason        TEXT,
    ts            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_run ON orders(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_orders_unknown ON orders(run_id, status) WHERE status = 'unknown';

CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES task_runs(id),
    token_id      TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    shares        REAL NOT NULL,
    avg_price     REAL NOT NULL,
    cost_usd      REAL NOT NULL,            -- excludes fees; the two fee columns track those
    -- Entry fees attributable to the shares STILL HELD. Scaled down when part of a position is
    -- sold, so the remainder's cost basis is the cost of the remainder and nothing more.
    fees_paid     REAL NOT NULL DEFAULT 0,
    -- Fees already expensed against realized_pnl: entry fees on shares that have gone, plus
    -- every exit fee. fees_paid + fees_realized is what the position has cost in fees overall.
    fees_realized REAL NOT NULL DEFAULT 0,
    opened_ts     INTEGER NOT NULL,
    closed_ts     INTEGER,
    close_reason  TEXT,                     -- which rung of the exit ladder fired
    -- Banked PnL. Accumulates across partial exits, so it is non-NULL on open positions too.
    realized_pnl  REAL NOT NULL DEFAULT 0,
    open          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_positions_run_open ON positions(run_id, open);
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_open_token
    ON positions(run_id, token_id) WHERE open = 1;

-- Stage 2 of discovery. One row per candidate wallet, all of it derived from
-- /closed-positions, so a rescan overwrites cleanly.
CREATE TABLE IF NOT EXISTS trader_scores (
    address          TEXT PRIMARY KEY,
    scanned_at       INTEGER NOT NULL,
    n_closed         INTEGER NOT NULL,
    win_rate         REAL,
    roi              REAL,
    realized_pnl     REAL,
    brier            REAL,                  -- avg_price as forecast vs the 0/1 settlement
    avg_entry_price  REAL,
    avg_stake_usd    REAL,
    max_drawdown     REAL,
    consistency      REAL,                  -- share of months in profit
    est_account_usd  REAL,                  -- from /value; the MIRROR sizing denominator
    top_category     TEXT,
    persona_fit      REAL,
    rank_score       REAL,
    -- Copyability, from copytrade/replay.py. These are the columns that decide whether a wallet
    -- is a candidate at all: a superb record with a capture ratio near zero is a wallet whose
    -- edge does not survive being copied, which is a fact about us, not about them.
    capture_ratio    REAL,                  -- copier roi at one poll interval / at zero lag
    edge_half_life_s REAL,                  -- lag at which copier roi reaches zero
    hold_p50_s       INTEGER,               -- median round-trip holding period
    fee_adjusted_roi REAL,                  -- their roi with both taker fees charged to it
    recent_roi       REAL,                  -- exponentially weighted toward recent form
    luck_p           REAL,                  -- bootstrap p-value that the record is not variance
    excluded         TEXT,                  -- why this wallet is not a candidate; NULL if it is
    metrics_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_trader_scores_rank ON trader_scores(rank_score DESC);

-- The decay curve behind capture_ratio: one row per wallet per copy lag. Kept rather than
-- collapsed into its summary because the shape matters -- a wallet whose roi falls off a cliff
-- between 15s and 30s is a different risk from one that decays gently over ten minutes, and the
-- two can share a capture ratio.
CREATE TABLE IF NOT EXISTS trader_replay (
    address        TEXT NOT NULL,
    lag_s          INTEGER NOT NULL,
    n              INTEGER NOT NULL,        -- round trips priced at both ends
    n_missing      INTEGER NOT NULL,        -- round trips with no quote at this lag
    copier_roi     REAL,                    -- per dollar staked, both taker fees charged
    copier_pnl     REAL,
    win_rate       REAL,
    scanned_at     INTEGER NOT NULL,
    PRIMARY KEY (address, lag_s)
);

-- Execution constraints per market. Separate from `markets` because these change on their own
-- schedule (a market stops accepting orders long before it resolves) and are only ever needed
-- for markets we might actually trade.
CREATE TABLE IF NOT EXISTS market_meta (
    condition_id      TEXT PRIMARY KEY,
    tick_size         REAL,
    min_order_size    REAL,
    accepting_orders  INTEGER,
    enable_order_book INTEGER,
    neg_risk          INTEGER,
    neg_risk_id       TEXT,                 -- what the per-event exposure cap is grouped on
    category_derived  TEXT,                 -- from feeType, since markets.category is always NULL
    fee_rate          REAL,
    end_ts            INTEGER,
    fetched_at        INTEGER NOT NULL
);

-- Resting exit orders. Polymarket's book takes a GTC limit order and holds it until it fills or
-- is cancelled, which is exactly the "sell my shares automatically at 0.62" behaviour the UI
-- exposes -- so a take-profit does not need the bot to be alive to fire. A stop-loss does: the
-- exchange has no stop order type, so a stop is a price the bot watches and a market sell it
-- sends, and it is unenforced whenever the process is not running. That asymmetry is the reason
-- this table exists rather than the exit ladder being held in memory: a TP posted live outlives
-- the session and must be findable (and cancellable) by the next one.
CREATE TABLE IF NOT EXISTS resting_orders (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES task_runs(id),
    position_id   INTEGER REFERENCES positions(id),
    mode          TEXT NOT NULL,            -- 'paper' | 'live'
    token_id      TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    side          TEXT NOT NULL,            -- always SELL today; BUY kept open for maker entries
    shares        REAL NOT NULL,
    price         REAL NOT NULL,            -- the resting limit, on-tick
    kind          TEXT NOT NULL,            -- 'take_profit' | 'manual'
    exchange_id   TEXT,                     -- the CLOB's order id, live mode only
    status        TEXT NOT NULL,            -- 'open' | 'filled' | 'cancelled' | 'rejected'
    filled_shares REAL NOT NULL DEFAULT 0,
    avg_price     REAL,
    reason        TEXT,
    placed_ts     INTEGER NOT NULL,
    settled_ts    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_resting_run_status ON resting_orders(run_id, status);
