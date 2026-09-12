"""Static configuration. No secrets here -- polywatch is read-only and unauthenticated."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
DB_PATH = DATA / "polywatch.db"

USER_AGENT = "polywatch/0.1 (read-only analytics)"

GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Verified 2026-09-05 by probing: /leaderboard 404s, /v1/leaderboard is live and caps at 50 rows
# per call regardless of `limit`; `offset` pages correctly, up to 1000.
#
# Correction 2026-09-06: the filter params were mis-named in the original probe. They are
# `category`, `timePeriod` and `orderBy` -- not `window`/`rankBy`, which the server drops in
# silence. All three work. The leaderboard is therefore a much larger surface than it looked:
# 11 categories x 4 windows x 2 orderings, each pageable to 1000.
LEADERBOARD_PAGE = 50
TRADES_PAGE = 1000  # verified: limit=1000 returns 1000 rows, ordered timestamp DESC
MARKET_BATCH = 20   # repeated `condition_ids` params batch fine; keeps the URL under ~2KB

# Politeness. Polymarket publishes no rate limit for these public endpoints, so this is a
# self-imposed cap: slow enough to stay invisible, fast enough to finish 50 wallets in minutes.
MAX_RPS = 10.0
MAX_ATTEMPTS = 6
BACKOFF_BASE = 1.5
BACKOFF_CAP = 60.0

# Price-history windows around each trade. PRE covers the pre-trade mark; POST must exceed the
# largest lag in the Step 4 sweep (15 min) with headroom, since a lagged price needs a quote at or
# after t+15min to be resolvable.
WINDOW_PRE_S = 300
WINDOW_POST_S = 1500
# Two windows on the same asset closer than this get merged into one request -- cheaper than
# issuing two calls whose payloads would overlap anyway.
WINDOW_MERGE_GAP_S = 1800
PRICE_FIDELITY = 1  # minutes. Verified: fidelity=1 is accepted with startTs/endTs (but NOT with
                    # interval=1m, which rejects anything under 10).

# Fee schedule fallback, used only when a market carries no `feeSchedule` object.
# Verified 2026-09-05 against docs.polymarket.com/trading/fees (Fee Structure V2, sports revised
# July 2026). Live markets ship their own feeSchedule -- and their feeType already reads
# "sports_fees_v3" -- so prefer the market's own values and treat this as a last resort.
FEE_FALLBACK = {
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "tech": 0.04,
    "mentions": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "geopolitics": 0.0,
    "world": 0.0,
}
FEE_FALLBACK_DEFAULT = 0.05
FEE_EXPONENT_DEFAULT = 1.0


# --- Copy trading -------------------------------------------------------------------------
# All page sizes verified live 2026-09-06 against the documented maxima.

LEADERBOARD_CATEGORIES = ("OVERALL", "POLITICS", "SPORTS", "ESPORTS", "CRYPTO", "CULTURE",
                          "MENTIONS", "WEATHER", "ECONOMICS", "TECH", "FINANCE")
LEADERBOARD_PERIODS = ("DAY", "WEEK", "MONTH", "ALL")
LEADERBOARD_ORDERINGS = ("PNL", "VOL")
LEADERBOARD_MAX_OFFSET = 1000

# Server max, verified 2026-09-12: limit=500 returns 500, limit=600 returns 500. Was 100 on the
# reasoning that a 1Hz poll never needs more -- true of the common case and irrelevant to the one
# that costs anything. Every poll passes `start`, so the server returns only what is new and the
# response is the same handful of rows either way; the limit is a cap, not a fetch size. What it
# changes is the catch-up after a stall, where the page cap used to mean five round trips and a
# `truncated` error at 500 events. It now takes one.
ACTIVITY_PAGE = 500
POSITIONS_PAGE = 500
CLOSED_POSITIONS_PAGE = 50    # server caps here whatever we ask for

# Execution floors. Both are per-market and shipped by gamma as `orderMinSize` and
# `orderPriceMinTickSize`; these are the values to assume when a market omits them. Every live
# market sampled on 2026-09-06 used min size 5 and tick 0.001.
MIN_ORDER_SHARES_FALLBACK = 5.0
TICK_SIZE_FALLBACK = 0.001
TICK_SIZES = (0.1, 0.01, 0.001, 0.0001)

# Bankroll defaults. Deliberately small: the whole point of paper mode is to find out whether
# a $100 account survives Polymarket's taker fees before any real money is exposed to them.
DEFAULT_BANKROLL_USD = 100.0
# Stake per copied trade. Read this against DEFAULT_MAX_DRAWDOWN_PCT rather than against the
# bankroll: the breaker halts the run at a fixed fraction of the bankroll, so the stake decides
# how many losing trades the run survives before it stops itself. At $10 on a $100 bankroll the
# 25% breaker is a handful of bad flips deep, which is thin -- $10 stakes want $250 or more
# behind them.
DEFAULT_STAKE_USD = 10.0
DEFAULT_SLIPPAGE = 0.07

# feeType strings carry the category that `markets.category` never does. Matched by substring,
# longest first, against feeType and then the (usually NULL) category column.
FEE_TYPE_CATEGORIES = ("geopolitics", "politics", "economics", "finance", "culture", "weather",
                       "mentions", "crypto", "sports", "tech", "world")


# --- Copy-trading engine ------------------------------------------------------------------

# Poll cadence for /activity. Was 15s on the reasoning that data-api caches the feed, so a
# faster poll buys nothing but rate-limit risk. The signals table has now been read, as that
# comment asked: at a 15s poll the observed detection lag (trader's fill -> we saw it) ran
# 13-23s, median 23s. Set against the copy-lag replay -- which puts the edge half-life of a
# quick-swing wallet at 51-59s, and copier ROI at +16..+27% at 15s of lag, +8..+11% at 30s and
# at or below zero by 60s -- a median 23s was spending nearly half the edge on the poll itself.
# 3s costs 2 requests/second across a six-wallet roster, well inside MAX_RPS, and moves the
# median toward whatever the cache floor turns out to be. Re-read the table before changing it
# again; it is the only thing here that is measured rather than argued.
POLL_INTERVAL_S = 3.0
POLL_OVERLAP_S = 120        # how far back each poll re-reads, so a slow page cannot drop a fill
POLL_TIMEOUT_S = 5.0        # a 30s socket timeout inside the loop would freeze the stop-loss

# A quick-flip trader's edge decays in minutes -- and, measured, faster than that: a 51-59s
# half-life on the fast wallets. Copying a fill noticed two minutes late is not copying them, it
# is buying what they already moved, at a price that has already absorbed it. 45s keeps a signal
# only while more than half its edge is still there. Signals older than this are skipped and
# counted -- if most skips are 'stale' the poll interval is wrong, or the trader is too fast to
# copy at all, which for some wallets is the honest answer.
MAX_SIGNAL_AGE_S = 45

# Session bound. The bot is not meant to run unattended: stop-loss and trailing exits are
# enforced by this process, so when it is not running they are not enforced either. A run ends
# at this age and flattens whatever it is holding.
SESSION_MAX_HOURS = 5.0
FLATTEN_AT_SESSION_END = True

# Exit ladder defaults, quick-flip shaped.
DEFAULT_STOP_LOSS_PCT = 0.15      # off avg entry, after fees
DEFAULT_TAKE_PROFIT_PCT = 0.10
DEFAULT_TRAIL_PCT = 0.0           # 0 disables; 0.06 trails 6% off the high-water mark
DEFAULT_MAX_HOLD_S = 2700         # 45 min. A quick flip that is still open after this failed.
DEFAULT_MAX_CONCURRENT = 3
DEFAULT_MAX_MARKET_USD = 25.0

# Run-level circuit breakers. Hit either and the run stops and flattens -- the point of a
# 4-hour session is to be able to lose a bounded amount while not watching it.
DEFAULT_MAX_DAILY_LOSS_USD = 20.0
DEFAULT_MAX_DRAWDOWN_PCT = 0.25

# Market-close guard. Liquidity thins and the book gaps as a market approaches resolution, so
# entering here is how a quick flip turns into an unsellable position.
MIN_SECONDS_TO_CLOSE = 900
# Price band. Fees are proportional to min(p, 1-p), and the book is thinnest at the extremes.
MIN_ENTRY_PRICE = 0.05
MAX_ENTRY_PRICE = 0.95

# Live trading. Read from the environment, never stored in the database or a config file.
ENV_PRIVATE_KEY = "POLYMARKET_PRIVATE_KEY"
ENV_FUNDER = "POLYMARKET_FUNDER"        # the proxy/funder address that holds the USDC
ENV_API_CREDS = ("POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE")
# Opt-in for live trading with no terminal attached. `--yes` alone is a person skipping a
# confirmation they have already read; `--yes` from cron is money moving with nobody watching,
# and only the operator can tell the two apart.
ENV_UNATTENDED = "POLYWATCH_UNATTENDED"
# Which wallet signs. 1 is the email/magic-login proxy, 2 a browser wallet's proxy, 0 a bare EOA
# that holds its own USDC. Wrong value means every order is rejected at the signature check.
ENV_SIGNATURE_TYPE = "POLYMARKET_SIGNATURE_TYPE"
DEFAULT_SIGNATURE_TYPE = 1
CLOB_CHAIN_ID = 137                      # Polygon mainnet

# Fee floor. A round trip costs 2 * rate * min(p, 1-p) / p of the stake -- 10% at even odds in
# a 5% category, under 1% near the extremes -- so a percentage take-profit under that number
# cannot be reached profitably however the trade goes. MIN_EDGE is the margin demanded on top
# of the floor; MAX_FEE_FRAC refuses the entry outright when the fee alone would eat this much
# of the stake, which no exit rule can undo.
DEFAULT_MIN_EDGE = 0.02
DEFAULT_MAX_FEE_FRAC = 0.12

# Liquidity floor. The spread is the other cost a round trip pays before the trade is right about
# anything, and it is expressed as a fraction of the mid so it reads in the same unit as the fee
# floor above: 10% here is the same size of problem as a 10% round-trip fee. A book this wide is
# not a market a copier can flip in, whatever the trader saw in it.
DEFAULT_MAX_SPREAD_FRAC = 0.10
# Absolute depth demanded on the side we are about to take, over and above being able to fill our
# own stake. 0 means "just enough for us", which is the honest default at a $10 stake -- there is
# no point demanding a deep book to spend ten dollars.
DEFAULT_MIN_DEPTH_USD = 0.0

# How many pages of /activity one poll will read before giving up and calling the read truncated.
# Each page is ACTIVITY_PAGE events; five of them is 500 events in one poll interval, which is far
# past any human and well past the point where copying is the right response.
MAX_ACTIVITY_PAGES = 5


# --- Trader evaluation ----------------------------------------------------------------------

# Below this many settled positions a wallet's record is not evidence of anything, and a
# significance test over it is theatre. Reported as None rather than as a flattering number.
MIN_SAMPLE_FOR_LUCK = 20

# Copy lags to replay a candidate's history at, in seconds. 0 is the trader themselves, charged
# our fees; 15 is one poll interval, which is the honest expectation; 900 is there to show where
# the edge is definitively gone rather than because anyone would copy that late.
REPLAY_LAGS = (0, 15, 30, 60, 300, 900)
# The lag a candidate is judged at -- one poll interval, the cadence the bot actually runs.
REPLAY_REFERENCE_LAG = 15

# How much of the trader's own edge has to survive one poll interval before copying them is
# worth doing at all. Below this the wallet may be excellent and is still not a candidate.
MIN_CAPTURE_RATIO = 0.30

# Share of a wallet's round trips that must resolve to a price at both ends before the replay is
# allowed to decide anything about it. Price history is fetched in windows around known trades,
# so a wallet whose exits fall outside those windows produces a curve drawn through a minority of
# its own trades -- which is not a reason to exclude it, and not a reason to trust it either.
MIN_REPLAY_COVERAGE = 0.30

# What ranking believes, in one place. Copyability outweighs quality because a great trader we
# cannot follow is worth zero, and evidence outweighs return because a good Brier is hard to fake
# and a good ROI is not. Every component is already bounded to [0, 1] by rank.py, so these are
# relative importances and nothing more -- they do not need to sum to anything.
RANK_WEIGHTS = {
    "capture": 3.0,        # how much of their edge survives one poll interval
    "persistence": 2.0,    # how long it survives at all
    "hold": 2.0,           # is their holding period one a poller can work with
    "brier": 2.0,          # calibration: the one metric that measures judgement
    "consistency": 2.0,    # months in profit, not one enormous month
    "luck": 1.5,           # distinguishable from their own variance
    "fee_roi": 1.5,        # return after both taker fees
    "recent": 1.0,         # recent form over lifetime
    "drawdown": 1.0,       # a disqualifier, not a virtue
}


# --- Strategy classification ------------------------------------------------------------------
# What kind of trader a wallet is. Rule-based over trades already in the database rather than
# clustered or labelled by a model: the point of a label is that it is stable and arguable, and a
# wallet has to classify the same way twice for an archetype's track record to mean anything.

# How far before a fill to read the price, to tell buying-into-a-move from fading one. 300s
# because WINDOW_PRE_S is 300 -- this is the history the replay backfill already fetches, so
# classification costs no extra requests.
PRE_DRIFT_S = 300

# A wallet with fewer trades than this is not classified at all. Same floor as the luck test,
# for the same reason: below it a label describes the sample, not the trader.
MIN_ARCHETYPE_TRADES = MIN_SAMPLE_FOR_LUCK

# The margin between the top two archetype scores that counts as a decisive call. Scores are all
# in [0,1] and a wallet usually looks a bit like several things, so raw margins are small; this
# is the divisor that turns one into a 0..1 confidence. 0.15 means "a 0.15 gap is certain".
ARCHETYPE_MARGIN_SCALE = 0.15
# Below this confidence the wallet is 'unclassified'. An honest refusal to label beats a coin
# flip presented as a verdict -- the same reason skill.py reports a thin metric as missing.
MIN_ARCHETYPE_CONFIDENCE = 0.25

# How many copied positions an archetype needs before its PnL is allowed to propose anything.
# Under this it is reported as an observation instead.
MIN_ARCHETYPE_POSITIONS = 15

# A skip reason has to be this share of a run's skips before it counts as dominating it.
SKIP_DOMINANCE_FRAC = 0.35


# --- Chain trade stream -----------------------------------------------------------------------
# Why this exists: data-api /activity ships `cache-control: public, max-age=15`, and the
# measured feed lag across four real runs was 13-21s against a loop cost of 0.0-0.4s. Verified
# 2026-09-12 by probe: data-api's `timestamp` for a fill IS the Polygon block timestamp, to the
# second. The information was on chain the whole time; the wait was the indexer's, not ours.
#
# This supersedes the claim in fetch/polymarket.py and ADR-0003 that a third party's fills
# "cannot be streamed, only polled". That is true of Polymarket's own websockets -- the CLOB
# market channel carries no wallet address and the user channel reports only your own account.
# It is not true of the chain, where the maker is an indexed topic and therefore filterable by
# the node before a byte reaches us.

# Two endpoints, raced. Both verified 2026-09-12 to accept eth_subscribe over WSS and to deliver
# newHeads at or slightly before the block's own stated timestamp. Free, no key, no signup; the
# redundancy is because one of them going quiet must not cost a session its speed.
CHAIN_WSS = ("wss://polygon-bor-rpc.publicnode.com", "wss://polygon.drpc.org")

# Polymarket's exchange contracts on Polygon, as far as we know them. This list is NOT used to
# filter the subscription, and that is deliberate. Filtering by address is how the first cut
# silently lost fills: a coverage check against the live feed turned up a third contract,
# 0xe111180000d2663c0091e4f400237545b87b996b, that no amount of reading the docs would have
# produced, and there is no reason to think the set is closed now. The roster topic filter is
# already narrow enough on its own -- a log has to name one of our wallets as maker or taker to
# reach us at all -- so subscribing by event signature alone catches any contract Polymarket
# deploys next, at no extra traffic.
#
# What the list is for: recognising an address we have not seen before and saying so, rather
# than trading on it in silence.
CHAIN_EXCHANGES = ("0xe2222d279d744050d28e00520010520000310f59",   # the bulk of current flow
                   "0xe111180000d2663c0091e4f400237545b87b996b",   # found by coverage check
                   "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",   # legacy CTF exchange
                   "0xc5d563a36ae78145c45a50134d48a1215220f80a")   # NegRisk; dormant when sampled

# OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker, ...).
# topic1 is the order hash, topic2 the maker, topic3 the taker; the 224-byte body is
# (makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled, fee, _, _).
CHAIN_ORDER_FILLED_TOPIC = \
    "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"

# Polymarket amounts are USDC-scaled: six decimals, on both legs.
CHAIN_DECIMALS = 6

# Connect quickly; tolerate silence. A roster of six wallets can go minutes without a fill, and
# a read timeout applied to that silence would tear down a working socket. As in stream.py, the
# read timeout is a heartbeat interval rather than a failure.
CHAIN_CONNECT_TIMEOUT_S = 10.0
CHAIN_READ_TIMEOUT_S = 30.0
CHAIN_RECONNECT_BASE_S = 1.0
CHAIN_RECONNECT_CAP_S = 30.0

# How many (tx_hash, log_index) pairs to remember when deduping the two racing endpoints. One
# block carries ~16 OrderFilled logs unfiltered and far fewer once maker-filtered, so this is
# several hundred blocks of memory for a few tens of kilobytes.
CHAIN_SEEN_CAP = 4096

# Block timestamps, cached from the parallel newHeads subscription so the hot path never has to
# make an eth_getBlockByNumber round trip to date an event.
CHAIN_BLOCK_TS_CAP = 512


# --- Generated documents ----------------------------------------------------------------------
# Written by the bot, read by whoever operates it next. Kept out of STRATEGY.md, which is
# hand-written and stays that way: a machine must not overwrite reasoning it did not derive.
DOCS = ROOT / "docs"
LEARNED_DOC = DOCS / "STRATEGY_LEARNED.md"
PROGRESS_DOC = DOCS / "PROGRESS.md"
