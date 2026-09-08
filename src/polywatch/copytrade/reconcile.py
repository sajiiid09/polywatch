"""Does the exchange agree with the database?

In paper mode the question is meaningless -- nothing was posted, so the database is the only
account there is, and every function here returns "nothing to do".

In live mode it is the only question that matters. The `positions` table is built from what the
engine believes its orders did, and belief is not custody. A restart, a partial fill, a REDEEM
done in the UI, a resting take-profit that filled while the process was asleep, or an order
response this program could not parse all leave the database describing an account that does not
exist. Trading on that description is how a bounded loss becomes an unbounded one.

So: before a live run trades, and after any tick that produced an order whose outcome the
exchange did not state, the two views are diffed.

Two rules govern what happens next, and they are deliberately asymmetric.

  * A **shortfall** -- the database claims shares the exchange does not show -- stops the run.
    Those shares back a stop-loss that would sell something we do not have.
  * A **surplus** -- the exchange shows shares the database does not know about -- is adopted
    when, and only when, there is exactly one unresolved order that could account for it.
    Anything less certain is reported and stops the run rather than being guessed at.

Nothing here silently repairs anything. `RULES.md` L5 and L6 are what this module enforces.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db import store

# Share counts come back as floats from two different systems. A hundredth of a share is below
# every market's minimum order size, so a difference this small is arithmetic, not custody.
SHARE_TOLERANCE = 0.01


@dataclass
class Discrepancy:
    """Something the two views disagree about. Any of these stops a live run."""
    kind: str            # 'unreadable' | 'shortfall' | 'unresolved_order' | 'ambiguous_surplus'
    token_id: str
    local: float         # what the database believes
    remote: float        # what the exchange reports
    detail: str

    def __str__(self) -> str:
        tok = self.token_id[:12] if self.token_id else "-"
        return f"{self.kind:18} {tok:<12} db {self.local:>10.2f}  exchange {self.remote:>10.2f}" \
               f"  {self.detail}"


@dataclass
class Resolution:
    """An unknown order the exchange has now accounted for."""
    order_id: int
    token_id: str
    condition_id: str
    side: str
    shares: float
    avg_price: float
    source: str          # 'order_status' | 'account_diff' | 'nothing_matched'


def _order_fill(executor, row) -> tuple[float, float] | None:
    """Ask the exchange what one order did. (shares, avg_price), or None if it cannot say."""
    live = executor.order_status(row["exchange_id"]) if row["exchange_id"] else None
    if not isinstance(live, dict):
        return None
    try:
        matched = float(live.get("size_matched") or live.get("sizeMatched") or 0)
    except (TypeError, ValueError):
        return None
    if matched <= 0:
        return 0.0, 0.0
    try:
        price = float(live.get("price") or row["limit_price"])
    except (TypeError, ValueError):
        price = float(row["limit_price"])
    return matched, price


def resolve(con, run_id: int, executor, remote: dict[str, float] | None
            ) -> tuple[list[Resolution], list[Discrepancy]]:
    """Account for every order the exchange accepted without saying what it matched.

    First ask about the order directly, which is exact. Failing that, fall back to the account:
    if the exchange holds more of that token than this run has recorded, and this is the only
    unresolved order that could explain it, the difference is that order's fill.
    """
    resolutions: list[Resolution] = []
    problems: list[Discrepancy] = []
    pending = store.unknown_orders(con, run_id)
    if not pending:
        return resolutions, problems

    by_token: dict[str, int] = {}
    for row in pending:
        by_token[row["token_id"]] = by_token.get(row["token_id"], 0) + 1

    for row in pending:
        told = _order_fill(executor, row)
        if told is not None:
            shares, price = told
            source = "order_status" if shares > 0 else "nothing_matched"
            resolutions.append(Resolution(row["id"], row["token_id"], row["condition_id"],
                                          row["side"], shares, price, source))
            continue

        # The exchange would not talk about the order. Try the account instead -- but only when
        # one order could account for the gap, since two unknowns and one surplus is a guess.
        if remote is None or by_token[row["token_id"]] > 1:
            problems.append(Discrepancy(
                "unresolved_order", row["token_id"], float(row["intent_usd"]), 0.0,
                f"order {row['id']} ({row['side']}) accepted, outcome never established"))
            continue

        held = store.recorded_shares(con, run_id, row["token_id"])
        surplus = remote.get(row["token_id"], 0.0) - held
        if row["side"] == "BUY" and surplus > SHARE_TOLERANCE:
            resolutions.append(Resolution(row["id"], row["token_id"], row["condition_id"], "BUY",
                                          surplus, float(row["limit_price"]), "account_diff"))
        elif abs(surplus) <= SHARE_TOLERANCE:
            # The account matches what we already recorded, so the order did nothing.
            resolutions.append(Resolution(row["id"], row["token_id"], row["condition_id"],
                                          row["side"], 0.0, 0.0, "nothing_matched"))
        else:
            problems.append(Discrepancy(
                "ambiguous_surplus", row["token_id"], held,
                remote.get(row["token_id"], 0.0),
                f"order {row['id']} ({row['side']}) unresolved and the account does not settle it"))
    return resolutions, problems


def check(con, run_id: int, executor) -> tuple[list[Resolution], list[Discrepancy]]:
    """Diff the exchange against the database for one run.

    Returns (resolutions to apply, discrepancies that must stop the run). Paper mode returns
    empty lists: there is no exchange to disagree with.
    """
    if getattr(executor, "mode", "paper") != "live":
        return [], []

    remote = executor.account_positions()
    if remote is None:
        return [], [Discrepancy(
            "unreadable", "", 0.0, 0.0,
            "the exchange could not be asked what this account holds")]

    resolutions, problems = resolve(con, run_id, executor, remote)

    # Adopted buys are about to become shares, so count them before calling anything a shortfall.
    incoming: dict[str, float] = {}
    for r in resolutions:
        if r.side == "BUY" and r.shares > 0:
            incoming[r.token_id] = incoming.get(r.token_id, 0.0) + r.shares

    for pos in store.open_positions(con, run_id):
        token = pos["token_id"]
        have = remote.get(token, 0.0) + incoming.get(token, 0.0)
        if have < pos["shares"] - SHARE_TOLERANCE:
            problems.append(Discrepancy(
                "shortfall", token, pos["shares"], have,
                "the database is holding shares the exchange does not show"))
    return resolutions, problems


def describe(problems: list[Discrepancy]) -> str:
    """The block printed when a live run refuses to continue."""
    head = ["", "  RECONCILIATION FAILED -- the exchange and the database disagree.", ""]
    body = [f"    {p}" for p in problems]
    tail = [
        "",
        "  This run will not trade. Acting on a position the exchange does not agree exists",
        "  means a stop-loss that sells shares we do not have, and an exposure cap measured",
        "  against a book that is not ours.",
        "",
        "  Check the account in the Polymarket UI, reconcile by hand, and use",
        "  `polywatch task orders` to inspect or cancel resting orders left behind.",
        "",
    ]
    return "\n".join(head + body + tail)
