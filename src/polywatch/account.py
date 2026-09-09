"""The account this program trades from, read the way anyone else can read it.

Everything here is an unauthenticated GET against data-api -- the same view the Polymarket UI
shows -- so it works before any key is in the environment and it stays honest about what the
exchange thinks, rather than what our own database recorded. That is the point: this is the
command an operator runs to check `polywatch task report` against reality.

The address comes from POLYMARKET_FUNDER (the proxy that holds the USDC), or from an explicit
argument for looking at someone else's wallet. No signing, no credentials, no writes.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from .config import ENV_FUNDER
from .fetch import polymarket as api
from .fetch.client import Client


def snapshot(client: Client, address: str, *, activity_limit: int = 10) -> dict:
    """{address, value_usd, positions, activity} -- open marks and the recent tape.

    Positions come back biggest-first by current mark. A wallet that has been trading a while
    holds hundreds of them, most worth nothing -- expired longshots the account never redeemed
    -- and printing those in arrival order buries the handful that still carry money.
    """
    val = api.portfolio_value(client, address) or []
    value_usd = float(val[0].get("value") or 0.0) if val else 0.0
    pos = api.positions(client, address) or []
    pos.sort(key=lambda p: float(p.get("currentValue") or 0.0), reverse=True)
    return {
        "address": address,
        "value_usd": value_usd,
        "positions": pos,
        "activity": api.activity(client, address, limit=activity_limit) or [],
    }


def _age(ts) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        return "?"
    return dt.strftime("%Y-%m-%d %H:%M")


def format_snapshot(snap: dict, *, limit: int = 20) -> str:
    out = [f"account {snap['address']}",
           f"  open positions marked at ${snap['value_usd']:,.2f}"
           "   (positions only -- idle USDC is not counted here)"]

    pos = snap["positions"]
    if not pos:
        out.append("\n  no open positions")
    else:
        shown = pos[:limit] if limit else pos
        out.append(f"\n  {len(pos)} open position(s)"
                   + (f", {len(shown)} largest by value:" if len(shown) < len(pos) else ":"))
        for p in shown:
            title = str(p.get("title") or p.get("slug") or p.get("conditionId") or "?")[:58]
            pnl = float(p.get("cashPnl") or 0.0)
            pct = float(p.get("percentPnl") or 0.0)
            out.append(
                f"    {title}\n"
                f"      {p.get('outcome', '?'):>4}  {float(p.get('size') or 0):>10,.2f} sh"
                f"  entry {float(p.get('avgPrice') or 0):.3f}"
                f"  now {float(p.get('curPrice') or 0):.3f}"
                f"  value ${float(p.get('currentValue') or 0):>8,.2f}"
                f"  pnl ${pnl:+,.2f} ({pct:+,.1f}%)"
            )
        if len(shown) < len(pos):
            rest = sum(float(p.get("currentValue") or 0.0) for p in pos[limit:])
            out.append(f"    ... and {len(pos) - len(shown)} more worth ${rest:,.2f} together")

    act = snap["activity"]
    if act:
        out.append(f"\n  last {len(act)} activity row(s):")
        for a in act:
            title = str(a.get("title") or a.get("conditionId") or "?")[:44]
            out.append(
                f"    {_age(a.get('timestamp'))}  {str(a.get('type') or ''):<8}"
                f"  {str(a.get('side') or ''):<4}  {float(a.get('size') or 0):>9,.2f} sh"
                f" @ {float(a.get('price') or 0):.3f}  {title}"
            )
    return "\n".join(out)


def live_preflight() -> list[tuple[str, bool, str]]:
    """Everything live mode needs, checked without placing an order.

    The last check constructs a LiveExecutor, which authenticates against the CLOB and derives
    API credentials if none are set. That is a signature over a login payload, not an order --
    nothing here can move money. It is the only way to find out that the key, the funder and
    the signature type agree before a real session depends on it.

    Returns (check, ok, detail) rows. No secret is ever put in a detail string.
    """
    from .config import DEFAULT_SIGNATURE_TYPE, ENV_API_CREDS, ENV_PRIVATE_KEY, ENV_SIGNATURE_TYPE

    rows: list[tuple[str, bool, str]] = []
    key = os.environ.get(ENV_PRIVATE_KEY)
    rows.append((ENV_PRIVATE_KEY, bool(key), "set" if key else "missing"))

    funder = os.environ.get(ENV_FUNDER)
    rows.append((ENV_FUNDER, bool(funder), funder or "missing"))

    creds = [os.environ.get(k) for k in ENV_API_CREDS]
    rows.append(("API creds", True,
                 "set" if all(creds) else "unset -- will be derived from the key"))

    sig = os.environ.get(ENV_SIGNATURE_TYPE) or str(DEFAULT_SIGNATURE_TYPE)
    label = {"0": "bare EOA", "1": "email/magic proxy", "2": "browser-wallet proxy"}.get(sig, "?")
    rows.append(("signature type", sig in ("0", "1", "2"), f"{sig} ({label})"))

    try:
        import py_clob_client  # noqa: F401
        rows.append(("py-clob-client", True, "installed"))
    except ImportError:
        rows.append(("py-clob-client", False, "missing -- uv pip install -e '.[live]'"))
        return rows

    if not (key and funder):
        rows.append(("CLOB auth", False, "skipped -- key or funder missing"))
        return rows
    try:
        from .copytrade.execution import LiveExecutor
        ex = LiveExecutor()
        rows.append(("CLOB auth", True, "authenticated; credentials work"))
    except Exception as e:  # noqa: BLE001 - any failure here is a failed preflight
        rows.append(("CLOB auth", False, f"{type(e).__name__}: {e}"))
        return rows

    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        resp = ex.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    except Exception as e:  # noqa: BLE001
        rows.append(("USDC balance", False, f"could not read: {type(e).__name__}: {e}"))
        return rows
    rows.extend(collateral_rows(resp))
    return rows


def collateral_rows(resp: dict) -> list[tuple[str, bool, str]]:
    """Balance and allowance checks, read from one get_balance_allowance response.

    The allowance one is the check an operator otherwise discovers by having their first live
    order rejected. Polymarket's exchange contracts can only move USDC the account has approved
    them to move, and a freshly created account has approved nothing -- the approvals happen as
    a side effect of the first trade placed through the web UI, which an account driven only by
    this program never makes.

    USDC has six decimals on Polygon, which is why the raw balance is divided rather than read.
    """
    bal = float(resp.get("balance") or 0) / 1e6
    allow = {k: float(v or 0) / 1e6 for k, v in (resp.get("allowances") or {}).items()}
    approved = sum(1 for v in allow.values() if v > 0)
    return [
        ("USDC balance", bal > 0, f"${bal:,.2f}" + ("" if bal > 0 else " -- nothing to trade with")),
        ("allowances", approved > 0,
         f"{approved}/{len(allow)} exchange contracts approved" if approved else
         "none approved -- place one trade in the Polymarket UI to set them"),
    ]


def format_preflight(rows: list[tuple[str, bool, str]]) -> str:
    out = ["live-mode preflight (no order is placed)"]
    for name, ok, detail in rows:
        out.append(f"  [{'ok' if ok else '--'}] {name:16} {detail}")
    bad = [n for n, ok, _ in rows if not ok]
    out.append("\n  ready" if not bad else f"\n  not ready: {', '.join(bad)}")
    return "\n".join(out)


def resolve_address(explicit: str | None) -> str:
    """The wallet to read. Explicit argument wins; otherwise the configured funder."""
    addr = explicit or os.environ.get(ENV_FUNDER)
    if not addr:
        raise RuntimeError(
            f"no address: pass one, or set {ENV_FUNDER} to your Polymarket (proxy) address"
        )
    return addr
