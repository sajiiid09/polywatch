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

ACTIVITY_PAGE = 100           # server max is 500; 100 is plenty for a 1Hz poll
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
DEFAULT_SLIPPAGE = 0.07

# feeType strings carry the category that `markets.category` never does. Matched by substring,
# longest first, against feeType and then the (usually NULL) category column.
FEE_TYPE_CATEGORIES = ("geopolitics", "politics", "economics", "finance", "culture", "weather",
                       "mentions", "crypto", "sports", "tech", "world")


# --- Backtest -----------------------------------------------------------------------------
# The replay has no historical order books, so our fill price is the target's own fill price
# worsened by a flat penalty. That one number stands in for both the book walk and copy
# latency, which makes it the largest modelling assumption in the whole backtest -- so the CLI
# sweeps it by default rather than quoting a single figure that hides it.
BACKTEST_SLIPPAGE_SWEEP = (0.0, 0.03, 0.07)

# Ceiling on MIRROR sizing, as a fraction of bankroll. /value reports open positions only and
# excludes idle cash, so the denominator is a floor on the target's account and every mirrored
# fraction it produces is an overstatement. Without a cap, one trade by a wallet whose cash
# sits idle would size to the whole bankroll.
MIRROR_MAX_FRACTION = 0.10

# Stake per copied trade, as a fraction of bankroll. `stake x max_concurrent` is the share of
# the account deployed at once, and it turned out to matter more than anything else the backtest
# measures: at 2% x 5 slots (10% deployed) five of six screened wallets finished profitable even
# at 7% slippage, while at 10% x 10 slots (fully deployed) four of the six were wiped out at
# every slippage. A small bankroll dies of ruin long before it dies of a bad trader.
DEFAULT_STAKE_FRACTION = 0.02

# Below this a position is dust: worth less than the fee to close it, and kept open it would
# distort every open-position count the gates read.
MIN_POSITION_USD = 0.01


# --- Discovery funnel ---------------------------------------------------------------------
# Four stages ordered by cost per wallet, so expensive evidence is only gathered for wallets
# that survived the cheap evidence.

# Leaderboard pages per (category, period, ordering) combination. 11 x 4 x 2 x this many calls.
# Two pages is 100 wallets per combination, which after dedupe is a candidate pool in the high
# hundreds -- plenty, and still under 200 requests.
SWEEP_PAGES_PER_COMBO = 2

# Walk-forward split. Wallets are RANKED on [now - RANK_WINDOW_DAYS, now - VALIDATION_DAYS] and
# then VALIDATED on the unseen days since. Ranking a wallet on the same history you judge it by
# is curve fitting, and the whole point of the split is that the second number was never
# available to the first.
RANK_WINDOW_DAYS = 60
VALIDATION_DAYS = 7

# A wallet must have traded this recently to be worth copying at all. Two days rather than the
# screen's usual seven: a wallet that last traded six days ago may simply have stopped, and
# copying silence produces no trades and no information.
MAX_RECENCY_DAYS_ACTIVE = 2.0

# Below this many settled positions before the cutoff, the metrics are noise. skill.brier makes
# the same point: a Brier over four markets means nothing.
MIN_SETTLED_FOR_RANK = 30

# Pre-cutoff settled positions to gather before paging stops. /closed-positions is newest
# first, so a prolific wallet's first pages sit entirely inside the validation window and are
# filtered away by the cutoff -- paging has to continue until enough OLD positions are in hand,
# and a flat page cap would silently score those wallets on almost nothing. Well above
# MIN_SETTLED_FOR_RANK so the metrics are stable, well below the 1,000-row ceiling so a deep
# history does not cost twenty requests.
RANK_SAMPLE_TARGET = 200

# Parallel fetchers for the scoring stage. Shares one global rate limiter, so this raises
# throughput without raising the request rate.
RANK_WORKERS = 4

# How many top-ranked wallets get the expensive walk-forward treatment.
FINALIST_COUNT = 15

# rank_score weights. Explicit rather than tuned: every one of these is a claim about what
# makes a trader copyable, and a reader should be able to disagree with a specific number.
RANK_WEIGHTS = {
    "calibration": 0.30,   # 1 - brier/0.25; the only metric measuring judgement not outcome
    "consistency": 0.25,   # share of months in profit; small wins over time beat one big score
    "roi": 0.20,           # return per dollar deployed, which is what copying reproduces
    "drawdown": 0.15,      # 1 - max_drawdown; a bad number here is damning
    "evidence": 0.10,      # min(n_closed/100, 1); stops a short record outranking a long one
}

# Brier score of forecasting 0.5 on everything. At or above it the entry prices carry no
# information whatever the PnL says, so calibration scores zero rather than merely poorly.
BRIER_COINFLIP = 0.25
