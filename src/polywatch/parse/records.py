"""Raw JSON -> row dicts ready for the store. The only place upstream shapes are interpreted."""

from __future__ import annotations

import json

from ..config import FEE_EXPONENT_DEFAULT, FEE_FALLBACK, FEE_FALLBACK_DEFAULT
from .fields import FieldError, iso_ts, json_str, opt, req


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
        "fees_enabled": 1 if rec.get("feesEnabled") else 0,
        "fee_type": opt(rec, "feeType", str, ctx),
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
