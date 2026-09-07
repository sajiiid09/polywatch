"""A task: one trader, one bankroll, one set of rules for copying them.

Persisted whole into `tasks.config_json`; the individual columns beside it are a denormalised
copy so that `task list` and ad-hoc SQL do not have to parse JSON. `config_json` is the source
of truth on read -- new knobs are added here and appear in old databases as their defaults,
which is why the engine never grew a schema migration for each new rule.

The defaults are shaped for the quick-flip pattern, because that is the pattern a copier can
actually capture: a trade held for twenty minutes is still there when we see it fifteen seconds
late, whereas a multi-day thesis position is not copied so much as joined at a worse price
after the move. Long-hold ideas are better handled as suggestions to act on by hand -- see
`hold='hours'`, which loosens the exits but is not what the poller is tuned for.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields

from ..config import (DEFAULT_BANKROLL_USD, DEFAULT_MAX_CONCURRENT, DEFAULT_MAX_DAILY_LOSS_USD,
                      DEFAULT_MAX_DRAWDOWN_PCT, DEFAULT_MAX_HOLD_S, DEFAULT_MAX_MARKET_USD,
                      DEFAULT_SLIPPAGE, DEFAULT_STOP_LOSS_PCT, DEFAULT_TAKE_PROFIT_PCT,
                      DEFAULT_TRAIL_PCT, MAX_ENTRY_PRICE, MAX_SIGNAL_AGE_S, MIN_ENTRY_PRICE,
                      MIN_SECONDS_TO_CLOSE, POLL_INTERVAL_S, SESSION_MAX_HOURS)


@dataclass
class Task:
    name: str
    trader: str
    mode: str = "paper"                     # 'paper' | 'live'
    bankroll: float = DEFAULT_BANKROLL_USD

    # --- sizing -------------------------------------------------------------------------
    buy_method: str = "fixed"               # 'fixed' | 'mirror'
    fixed_usd: float = 10.0
    # MIRROR: their stake as a fraction of their account, applied to ours. Capped, because a
    # trader who bets 40% of their book on one market is not someone to mirror proportionally.
    mirror_max_frac: float = 0.10
    max_market_usd: float = DEFAULT_MAX_MARKET_USD
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    slippage: float = DEFAULT_SLIPPAGE

    # --- exits --------------------------------------------------------------------------
    # sl_kind/tp_kind: 'pct' measures off the average entry price, 'price' is an absolute
    # 0..1 level, None disables the rung.
    sl_kind: str | None = "pct"
    sl_value: float | None = DEFAULT_STOP_LOSS_PCT
    tp_kind: str | None = "pct"
    tp_value: float | None = DEFAULT_TAKE_PROFIT_PCT
    trail_pct: float = DEFAULT_TRAIL_PCT    # 0 disables
    max_hold_s: int = DEFAULT_MAX_HOLD_S
    # Post the take-profit as a resting GTC limit sell on the exchange instead of watching for
    # the price and selling at market. This is the one exit that survives the bot being closed.
    resting_tp: bool = True
    # Sell when the trader sells. The whole reason to copy a quick-flip trader is that they
    # know when to get out; the exit ladder is a floor under that, not a replacement for it.
    follow_exit: bool = True

    # --- gates --------------------------------------------------------------------------
    max_signal_age_s: int = MAX_SIGNAL_AGE_S
    min_price: float = MIN_ENTRY_PRICE
    max_price: float = MAX_ENTRY_PRICE
    min_seconds_to_close: int = MIN_SECONDS_TO_CLOSE
    min_trade_usd: float = 0.0              # ignore the trader's own dust

    # --- run bounds ---------------------------------------------------------------------
    poll_interval_s: float = POLL_INTERVAL_S
    session_hours: float = SESSION_MAX_HOURS
    max_daily_loss_usd: float = DEFAULT_MAX_DAILY_LOSS_USD
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT
    flatten_on_stop: bool = True

    # --- persona, carried for discovery and reporting only ------------------------------
    behavior: str = "buys_sells"
    risk: str = "conservative"
    category: str | None = None
    style: str = "momentum"
    hold: str = "quick_flips"
    activity: str = "active"

    notes: str = ""

    def __post_init__(self) -> None:
        self.trader = self.trader.lower()
        if self.mode not in ("paper", "live"):
            raise ValueError(f"mode must be paper or live, got {self.mode!r}")
        if self.buy_method not in ("fixed", "mirror"):
            raise ValueError(f"buy_method must be fixed or mirror, got {self.buy_method!r}")
        for kind_attr in ("sl_kind", "tp_kind"):
            k = getattr(self, kind_attr)
            if k not in (None, "pct", "price"):
                raise ValueError(f"{kind_attr} must be pct, price or None, got {k!r}")
        if not 0 <= self.slippage < 1:
            raise ValueError("slippage is a fraction, 0..1")
        if self.min_price >= self.max_price:
            raise ValueError("min_price must be below max_price")

    # --- persistence --------------------------------------------------------------------

    def as_row(self) -> dict:
        """The `tasks` row: the denormalised columns plus the whole config as JSON."""
        d = asdict(self)
        row = {c: d.get(c) for c in (
            "name", "trader", "mode", "bankroll", "buy_method", "fixed_usd", "max_market_usd",
            "max_concurrent", "slippage", "sl_kind", "sl_value", "tp_kind", "tp_value",
            "behavior", "risk", "category", "style", "hold", "activity")}
        row["config_json"] = json.dumps(d, sort_keys=True)
        return row

    @classmethod
    def from_row(cls, row) -> "Task":
        """Rebuild from a `tasks` row, tolerating a config written by an older version.

        Unknown keys are dropped and missing ones fall back to the dataclass default, so adding
        a knob here never invalidates a saved task.
        """
        cfg = json.loads(row["config_json"]) if row["config_json"] else {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in cfg.items() if k in known})

    # --- derived ------------------------------------------------------------------------

    def stake_usd(self, trader_usd: float, trader_account_usd: float) -> float:
        """What to spend copying a trade of `trader_usd` by an account worth
        `trader_account_usd`.

        FIXED ignores both and bets the same amount every time. MIRROR scales their conviction
        onto our bankroll: the fraction of their account they just risked, times ours, capped at
        `mirror_max_frac`. The cap matters more than the ratio -- /value reports open positions
        only, so it understates their account and therefore overstates the fraction.
        """
        if self.buy_method == "fixed":
            usd = self.fixed_usd
        else:
            frac = (trader_usd / trader_account_usd) if trader_account_usd > 0 else 0.0
            usd = min(frac, self.mirror_max_frac) * self.bankroll
        return min(usd, self.max_market_usd)

    def stop_price(self, avg_price: float) -> float | None:
        if self.sl_kind is None or self.sl_value is None:
            return None
        return avg_price * (1 - self.sl_value) if self.sl_kind == "pct" else self.sl_value

    def target_price(self, avg_price: float) -> float | None:
        if self.tp_kind is None or self.tp_value is None:
            return None
        return avg_price * (1 + self.tp_value) if self.tp_kind == "pct" else self.tp_value


QUICK_FLIP = dict(hold="quick_flips", max_hold_s=2700, max_signal_age_s=120,
                  poll_interval_s=15.0, trail_pct=0.06, follow_exit=True)

# Looser everything: a position meant to be held for hours cannot have a 45-minute time stop or
# a 6% trailing exit, and following the trader out matters less when their exit is a day away.
# This preset exists so the same machinery can babysit a manual idea, not because the poller is
# any good at finding one.
SLOW_HOLD = dict(hold="hours", max_hold_s=6 * 3600, max_signal_age_s=900,
                 poll_interval_s=30.0, trail_pct=0.0, sl_value=0.25, tp_value=0.30)

PRESETS = {"quick_flips": QUICK_FLIP, "hours": SLOW_HOLD}


def preset(name: str, **overrides) -> dict:
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; have {sorted(PRESETS)}")
    return {**PRESETS[name], **overrides}
