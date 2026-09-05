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
