"""Raw JSON -> row dicts ready for the store. The only place upstream shapes are interpreted."""

from __future__ import annotations

import json

from ..config import (FEE_EXPONENT_DEFAULT, FEE_FALLBACK, FEE_FALLBACK_DEFAULT,
                      FEE_TYPE_CATEGORIES)
from .fields import FieldError, iso_ts, json_str, opt, req

# Places to round every amount to. Polymarket quotes USDC and shares to six decimals, and
# both the /activity path and the chain path must land on the identical float -- see
# parse_activity.
AMOUNT_DP = 6



def parse_leaderboard(payload: list, source: str = "leaderboard") -> list[dict]:
    out = []
    for rec in payload:
        ctx = "leaderboard record"
        out.append({
            "address": req(rec, "proxyWallet", str, ctx).lower(),
            "source": source,
            "rank": opt(rec, "rank", int, ctx),
            "username": opt(rec, "userName", str, ctx),
            "vol": opt(rec, "vol", float, ctx),
            "pnl": opt(rec, "pnl", float, ctx),
        })
    return out


def parse_trades(payload: list) -> list[dict]:
    out = []
    for rec in payload:
        ctx = "trade record"
        out.append({
            "wallet": req(rec, "proxyWallet", str, ctx).lower(),
            "token_id": req(rec, "asset", str, ctx),
            "condition_id": req(rec, "conditionId", str, ctx),
            "side": req(rec, "side", str, ctx).upper(),
            "size": req(rec, "size", float, ctx),
            "price": req(rec, "price", float, ctx),
            "ts": req(rec, "timestamp", int, ctx),
            "outcome": opt(rec, "outcome", str, ctx),
            "outcome_index": opt(rec, "outcomeIndex", int, ctx),
            "tx_hash": opt(rec, "transactionHash", str, ctx, ""),
        })
    return out


def _fee_fields(rec: dict, ctx: str) -> dict:
    """Fee parameters for a market.

    Prefer the market's own `feeSchedule` object -- live markets carry
    {exponent, rate, takerOnly, rebateRate} and their `feeType` already reads 'sports_fees_v3',
    i.e. the chain is ahead of the published V2 table. Fall back to the documented per-category
    rates only when the market ships nothing, and record which source was used so Step 4 can
    mark those PnL numbers as resting on an assumption.
    """
    sched = rec.get("feeSchedule")
    if isinstance(sched, dict) and sched.get("rate") is not None:
        return {
            "fee_rate": float(sched["rate"]),
            "fee_exponent": float(sched.get("exponent", FEE_EXPONENT_DEFAULT)),
            "fee_taker_only": 1 if sched.get("takerOnly", True) else 0,
            "fee_rebate_rate": opt(sched, "rebateRate", float, ctx),
            "fee_source": "market",
        }

    fee_type = (opt(rec, "feeType", str, ctx, "") or "").lower()
    category = (opt(rec, "category", str, ctx, "") or "").lower()
    rate = FEE_FALLBACK_DEFAULT
    for key, val in FEE_FALLBACK.items():
        if key in fee_type or key in category:
            rate = val
            break
    return {
        "fee_rate": rate,
        "fee_exponent": FEE_EXPONENT_DEFAULT,
        "fee_taker_only": 1,
        "fee_rebate_rate": None,
        "fee_source": "fallback",
    }


def _resolution(closed: int, prices: list | None) -> tuple[int, int | None]:
    """Decide whether a market resolved, and to which outcome.

    Resolution is read off outcomePrices: a settled market shows exactly one outcome at 1 and the
    rest at 0. Anything else -- still trading, half-settled, a price like 0.98 -- is treated as
    unresolved, because Brier needs a hard 0/1 outcome and a near-1 price is a market opinion,
    not a result.
    """
    if not closed or not prices:
        return 0, None
    try:
        vals = [float(p) for p in prices]
    except (TypeError, ValueError):
        return 0, None
    winners = [i for i, v in enumerate(vals) if v >= 0.999]
    losers = [v for v in vals if v <= 0.001]
    if len(winners) == 1 and len(losers) == len(vals) - 1:
        return 1, winners[0]
    return 0, None


def parse_market(rec: dict) -> tuple[dict, list[dict]]:
    """Returns (market row, asset rows)."""
    ctx = f"market {rec.get('conditionId', '?')}"
    condition_id = req(rec, "conditionId", str, ctx)
    outcomes = json_str(rec, "outcomes", ctx, []) or []
    prices = json_str(rec, "outcomePrices", ctx, []) or []
    token_ids = json_str(rec, "clobTokenIds", ctx, []) or []
    uma = json_str(rec, "umaResolutionStatuses", ctx, []) or []
    closed = 1 if rec.get("closed") else 0
    resolved, winner = _resolution(closed, prices)

    market = {
        "condition_id": condition_id,
        # Gamma's own numeric id, distinct from conditionId. Needed because /markets/{id}/tags
        # -- the only place the real category taxonomy lives -- takes this and not the
        # condition id. Dropping it made the tags endpoint uncallable.
        "gamma_id": opt(rec, "id", str, ctx),
        "question": opt(rec, "question", str, ctx),
        "slug": opt(rec, "slug", str, ctx),
        "category": opt(rec, "category", str, ctx),
        "closed": closed,
        "active": 1 if rec.get("active") else 0,
        "archived": 1 if rec.get("archived") else 0,
        "start_ts": iso_ts(rec, "startDate", ctx) or iso_ts(rec, "createdAt", ctx),
        "end_ts": iso_ts(rec, "endDate", ctx),
        "outcomes_json": json.dumps(outcomes),
        "prices_json": json.dumps(prices),
        "uma_status_json": json.dumps(uma),
        "resolved": resolved,
        "winning_index": winner,
        "neg_risk": 1 if rec.get("negRisk") else 0,
        # The id shared by every outcome of one neg-risk event. Without it a per-market exposure
        # cap caps nothing there: three condition ids can be three ways of holding one view.
        "neg_risk_id": opt(rec, "negRiskMarketID", str, ctx),
        "fees_enabled": 1 if rec.get("feesEnabled") else 0,
        "fee_type": opt(rec, "feeType", str, ctx),
        # Execution constraints. Read here rather than from the CLOB /tick-size endpoint
        # because gamma already ships them alongside everything else we ingest, at no extra
        # request. An order priced off-tick or under the min size is rejected outright.
        "tick_size": opt(rec, "orderPriceMinTickSize", float, ctx),
        "order_min_size": opt(rec, "orderMinSize", float, ctx),
        "accepting_orders": 1 if rec.get("acceptingOrders") else 0,
        "enable_order_book": 1 if rec.get("enableOrderBook") else 0,
    }
    market.update(_fee_fields(rec, ctx))

    assets = []
    for i, token in enumerate(token_ids):
        assets.append({
            "token_id": str(token),
            "condition_id": condition_id,
            "outcome_index": i,
            "outcome": outcomes[i] if i < len(outcomes) else None,
        })
    return market, assets


def parse_price_history(payload: dict) -> list[tuple[int, float]]:
    if not isinstance(payload, dict) or "history" not in payload:
        raise FieldError(f"prices-history: expected dict with 'history', got {type(payload).__name__}")
    out = []
    for pt in payload["history"]:
        ctx = "price point"
        out.append((req(pt, "t", int, ctx), req(pt, "p", float, ctx)))
    return out


# --- Copy trading -------------------------------------------------------------------------


def parse_activity(payload: list) -> list[dict]:
    """The /activity feed -- a superset of /trades, and the poller's input.

    `side` is optional here in a way it never is on /trades: a REDEEM or a MERGE has no side,
    and those rows still matter because they are how a position can vanish without a sell.

    Amounts are rounded to `AMOUNT_DP`. That is not cosmetic. The same fill now reaches the
    engine by two routes -- this one, which parses Polymarket's decimal string, and the chain
    decoder, which divides a uint256 by 1e6 -- and `signals` is UNIQUE on
    (run_id, tx_hash, token_id, side, size) with `size` stored as REAL. A value such as
    825.09091 can land one ULP apart between those two computations, in which case the UNIQUE
    constraint does not fire and the run buys the same fill twice. Rounding both paths to the
    six places the exchange actually quotes in makes them bit-identical.
    """
    out = []
    for rec in payload:
        ctx = "activity record"
        out.append({
            "wallet": req(rec, "proxyWallet", str, ctx).lower(),
            "kind": req(rec, "type", str, ctx).upper(),
            "token_id": opt(rec, "asset", str, ctx, ""),
            "condition_id": opt(rec, "conditionId", str, ctx, ""),
            "side": (opt(rec, "side", str, ctx, "") or "").upper(),
            "size": round(opt(rec, "size", float, ctx, 0.0), AMOUNT_DP),
            "price": round(opt(rec, "price", float, ctx, 0.0), AMOUNT_DP),
            "usdc_size": round(opt(rec, "usdcSize", float, ctx, 0.0), AMOUNT_DP),
            "ts": req(rec, "timestamp", int, ctx),
            "outcome": opt(rec, "outcome", str, ctx),
            "outcome_index": opt(rec, "outcomeIndex", int, ctx),
            "tx_hash": opt(rec, "transactionHash", str, ctx, ""),
            "title": opt(rec, "title", str, ctx),
            "slug": opt(rec, "slug", str, ctx),
        })
    return out


def parse_positions(payload: list) -> list[dict]:
    """A wallet's open positions. `cash_pnl` and `pct_pnl` are Polymarket's own marks."""
    out = []
    for rec in payload:
        ctx = "position record"
        out.append({
            "wallet": req(rec, "proxyWallet", str, ctx).lower(),
            "token_id": req(rec, "asset", str, ctx),
            "condition_id": req(rec, "conditionId", str, ctx),
            "shares": req(rec, "size", float, ctx),
            "avg_price": opt(rec, "avgPrice", float, ctx, 0.0),
            "cur_price": opt(rec, "curPrice", float, ctx, 0.0),
            "initial_value": opt(rec, "initialValue", float, ctx, 0.0),
            "current_value": opt(rec, "currentValue", float, ctx, 0.0),
            "cash_pnl": opt(rec, "cashPnl", float, ctx, 0.0),
            "pct_pnl": opt(rec, "percentPnl", float, ctx, 0.0),
            "entry_fees": opt(rec, "entryFeesUsdc", float, ctx, 0.0),
            "redeemable": 1 if rec.get("redeemable") else 0,
            "outcome": opt(rec, "outcome", str, ctx),
            "outcome_index": opt(rec, "outcomeIndex", int, ctx),
            "title": opt(rec, "title", str, ctx),
        })
    return out


def parse_closed_positions(payload: list) -> list[dict]:
    """A wallet's settled positions -- the raw material for every skill metric.

    `cost` is derived rather than read: upstream gives shares (`totalBought`) and the average
    entry price, and the product is what was actually staked. ROI needs a denominator and this
    is the only honest one available without replaying every fill.

    `cur_price` is the settled outcome, 1 or 0, which doubles as the Brier target against
    `avg_price` as the forecast. It is kept raw rather than coerced, because a market that
    settled oddly should be visible as odd rather than silently rounded to a win or a loss.
    """
    out = []
    for rec in payload:
        ctx = "closed position record"
        shares = opt(rec, "totalBought", float, ctx, 0.0)
        avg_price = opt(rec, "avgPrice", float, ctx, 0.0)
        out.append({
            "wallet": req(rec, "proxyWallet", str, ctx).lower(),
            "token_id": req(rec, "asset", str, ctx),
            "condition_id": req(rec, "conditionId", str, ctx),
            "shares": shares,
            "avg_price": avg_price,
            "cost": shares * avg_price,
            "cur_price": opt(rec, "curPrice", float, ctx, 0.0),
            "realized_pnl": opt(rec, "realizedPnl", float, ctx, 0.0),
            "outcome": opt(rec, "outcome", str, ctx),
            "outcome_index": opt(rec, "outcomeIndex", int, ctx),
            "end_ts": iso_ts(rec, "endDate", ctx),
            "title": opt(rec, "title", str, ctx),
            "slug": opt(rec, "slug", str, ctx),
        })
    return out


def parse_book(payload: dict) -> dict:
    """The CLOB order book, normalised so the best price is always first.

    Upstream sends both sides as price-ascending strings, which means the best bid is at the
    END of the bids list and the best ask at the START of the asks list -- an asymmetry that is
    very easy to get backwards and produces a plausible-looking wrong price when you do. Sorting
    here means copytrade/book.py can walk either side with the same loop.
    """
    if not isinstance(payload, dict):
        raise FieldError(f"book: expected dict, got {type(payload).__name__}")

    def levels(key: str) -> list[tuple[float, float]]:
        rows = payload.get(key) or []
        out = []
        for lv in rows:
            ctx = f"book {key} level"
            out.append((req(lv, "price", float, ctx), req(lv, "size", float, ctx)))
        return out

    return {
        "token_id": opt(payload, "asset_id", str, "book", ""),
        "condition_id": opt(payload, "market", str, "book", ""),
        "bids": sorted(levels("bids"), key=lambda lv: -lv[0]),
        "asks": sorted(levels("asks"), key=lambda lv: lv[0]),
    }


def derive_category(fee_type: str | None, category: str | None = None) -> str | None:
    """Best-effort market category from the fee type.

    `markets.category` is NULL for every market we have ingested, but `feeType` reads
    'sports_fees_v3', 'crypto_fees_v2', 'politics_fees' and so on -- the taxonomy is there, just
    wearing a different hat. Matching it costs nothing, where gamma's /tags endpoint costs one
    request per market. Longest match first so 'geopolitics' never reads as 'politics'.
    """
    hay = f"{(fee_type or '').lower()} {(category or '').lower()}"
    for key in sorted(FEE_TYPE_CATEGORIES, key=len, reverse=True):
        if key in hay:
            return key
    return None
