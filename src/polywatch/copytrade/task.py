"""The Task dataclass that `schema.sql` has been promising since Phase 1.

One task is one standing instruction: copy this wallet, this way, with this much money. It is
the same object whether it drives a historical replay, a live paper run or (eventually) a live
one -- only the fill source changes underneath it. That is deliberate: a backtest that used
different rules from the thing it was meant to validate would be measuring the wrong program.

`config_json` in the tasks table is this dataclass serialised whole; the sibling columns are
denormalised copies so `task list` and ad-hoc SQL never have to parse JSON.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields

from ..config import DEFAULT_BANKROLL_USD, DEFAULT_SLIPPAGE

MODES = ("paper", "live")
BUY_METHODS = ("fixed", "mirror")
BEHAVIORS = ("buys", "buys_sells")
RISKS = ("conservative", "moderate")
STYLES = ("safe_and_steady", "value_hunter", "momentum")
HOLDS = ("quick_flips", "hours")
ACTIVITIES = ("casual", "active")
EXIT_KINDS = ("pct", "price")

# Price bands per style. A style is a claim about *where in the probability range* the edge is,
# and enforcing it as an entry filter is the only way the setting means anything -- otherwise
# it is a label on a run that behaved identically to every other run.
#   safe_and_steady  favourites; small edges, high hit rate, and the cheapest fees
#   value_hunter     underdogs; the tail the favourites-buyers are selling
#   momentum         anything liquid enough to move
STYLE_BANDS = {
    "safe_and_steady": (0.60, 0.97),
    "value_hunter": (0.03, 0.40),
    "momentum": (0.02, 0.98),
}

# Risk caps the entry price on top of the style band. `conservative` refuses the far tails in
# both directions: a 0.02 lottery ticket and a 0.98 near-certainty are opposite trades but both
# are bets the fee schedule and the resolution risk make bad at a $100 bankroll.
RISK_BANDS = {
    "conservative": (0.10, 0.92),
    "moderate": (0.01, 0.99),
}


@dataclass
class Task:
    name: str
    trader: str
    mode: str = "paper"
    bankroll: float = DEFAULT_BANKROLL_USD
    buy_method: str = "fixed"
    fixed_usd: float | None = 10.0
    max_market_usd: float = 25.0
    max_concurrent: int = 10
    slippage: float = DEFAULT_SLIPPAGE
    sl_kind: str | None = None
    sl_value: float | None = None
    tp_kind: str | None = None
    tp_value: float | None = None
    behavior: str = "buys_sells"
    risk: str = "moderate"
    category: str | None = None
    style: str = "momentum"
    hold: str = "hours"
    activity: str = "active"
    created_at: int | None = None
    updated_at: int | None = None

    def __post_init__(self):
        self.trader = self.trader.lower()
        _one_of("mode", self.mode, MODES)
        _one_of("buy_method", self.buy_method, BUY_METHODS)
        _one_of("behavior", self.behavior, BEHAVIORS)
        _one_of("risk", self.risk, RISKS)
        _one_of("style", self.style, STYLES)
        _one_of("hold", self.hold, HOLDS)
        _one_of("activity", self.activity, ACTIVITIES)
        for name in ("sl_kind", "tp_kind"):
            val = getattr(self, name)
            if val is not None:
                _one_of(name, val, EXIT_KINDS)
        if self.bankroll <= 0:
            raise ValueError(f"bankroll must be positive, got {self.bankroll}")
        if not 0.0 <= self.slippage < 1.0:
            raise ValueError(f"slippage must be in [0, 1), got {self.slippage}")
        if self.max_market_usd <= 0:
            raise ValueError(f"max_market_usd must be positive, got {self.max_market_usd}")
        if self.max_concurrent < 1:
            raise ValueError(f"max_concurrent must be at least 1, got {self.max_concurrent}")
        if self.buy_method == "fixed" and not (self.fixed_usd and self.fixed_usd > 0):
            raise ValueError("buy_method='fixed' needs a positive fixed_usd")

    # --- price bands ----------------------------------------------------------------------

    def price_band(self) -> tuple[float, float]:
        """The intersection of the style band and the risk band.

        Intersected rather than layered so that an impossible combination surfaces as an empty
        band -- and therefore as every signal skipped with a named reason -- instead of one
        setting silently winning over the other.
        """
        s_lo, s_hi = STYLE_BANDS[self.style]
        r_lo, r_hi = RISK_BANDS[self.risk]
        return max(s_lo, r_lo), min(s_hi, r_hi)

    # --- persistence ----------------------------------------------------------------------

    def to_row(self) -> dict:
        row = asdict(self)
        row["config_json"] = json.dumps(asdict(self), sort_keys=True)
        # store.upsert_task stamps these with setdefault, which a present-but-None key defeats.
        # Dropping them is what lets the database own the clock for a task's own metadata.
        for key in ("created_at", "updated_at"):
            if row.get(key) is None:
                row.pop(key)
        return row

    @classmethod
    def from_row(cls, row) -> "Task":
        names = {f.name for f in fields(cls)}
        return cls(**{k: row[k] for k in row.keys() if k in names})


def _one_of(name: str, value, allowed: tuple) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {allowed}, got {value!r}")
