"""Order execution. Two implementations behind one interface.

`PaperExecutor` fills against the real, live order book but signs nothing and spends nothing.
`LiveExecutor` signs EIP-712 orders with a private key and posts them to the CLOB. They are
deliberately the same shape so the engine never branches on mode: everything above this module
is identical whether money is moving or not, and "does this program spend money" stays a
one-import question.

Three things about order types, because the exit ladder depends on all three:

  * A copy of someone else's fill is a taker order. It is posted as FAK (fill-and-kill) at a
    slippage-bounded limit: take what the book offers at or better than our price, cancel the
    rest. A GTC order here would rest at the top of the book and get picked off.
  * A take-profit is a maker order. Posted GTC, it sits on the book until the price comes to it
    and fills without anyone watching -- the same thing the Polymarket UI does when you place a
    sell limit above the market. This is the only exit that survives the bot being closed.
  * A stop-loss is not an order at all. The CLOB has no stop or stop-limit type, so a stop is a
    price this process watches and a market sell it sends when the price is crossed. It is
    enforced only while the bot is running. That is a real limitation and it is stated in the
    run banner rather than buried here.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ..config import CLOB_CHAIN_ID, ENV_API_CREDS, ENV_FUNDER, ENV_PRIVATE_KEY, TICK_SIZE_FALLBACK
from . import book as bk


@dataclass
class Fill:
    """What an order actually did. `status` is what the engine branches on."""
    status: str                 # 'filled' | 'partial' | 'rejected'
    shares: float = 0.0
    cost: float = 0.0           # USDC out on a buy, in on a sell, excluding fee
    avg_price: float = 0.0
    fee: float = 0.0
    book_vwap: float = 0.0
    limit_price: float = 0.0
    reason: str | None = None
    exchange_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.shares > 0


@dataclass
class Resting:
    """A GTC order left on the book."""
    status: str                 # 'open' | 'rejected'
    price: float
    shares: float
    exchange_id: str | None = None
    reason: str | None = None


class Executor:
    """The interface the engine codes against."""

    mode = "abstract"

    def buy(self, token_id: str, usd: float, book: dict, *, limit: float, tick: float,
            fee_rate: float, min_shares: float) -> Fill:
        raise NotImplementedError

    def sell(self, token_id: str, shares: float, book: dict, *, limit: float, tick: float,
             fee_rate: float) -> Fill:
        raise NotImplementedError

    def place_resting_sell(self, token_id: str, shares: float, price: float, *,
                           tick: float) -> Resting:
        raise NotImplementedError

    def cancel(self, exchange_id: str) -> bool:
        raise NotImplementedError

    def resting_fill(self, order: dict, book: dict, *, fee_rate: float) -> Fill | None:
        """Has a resting order filled? Returns a Fill when it has, None while it still rests."""
        raise NotImplementedError


class PaperExecutor(Executor):
    """Fills against the live book. Signs nothing, spends nothing, is optimistic in exactly one
    documented way.

    The optimism: a paper order consumes the book as it stood microseconds ago and pays no
    queue penalty, so it fills where a real order might have missed by a tick. It is bounded by
    the same limit price a live order would carry, so the error is one of timing, not of price
    -- paper cannot fill at a price the live book was not showing. Read a paper run's fill rate
    as an upper bound and its PnL as the best case.
    """

    mode = "paper"

    def buy(self, token_id: str, usd: float, book: dict, *, limit: float, tick: float,
            fee_rate: float, min_shares: float) -> Fill:
        levels = [(p, s) for p, s in (book.get("asks") or []) if p <= limit + 1e-9]
        if not levels:
            ask = bk.best_ask(book)
            return Fill("rejected", limit_price=limit,
                        reason=f"no ask at or below {limit:.3f} (best {ask})")
        w = bk._walk(levels, usd=usd)
        if not bk.meets_min_size(w.shares, min_shares):
            return Fill("rejected", limit_price=limit, book_vwap=w.vwap,
                        reason=f"{w.shares:.1f} shares below market minimum {min_shares:g}")
        f = bk.fee(w.shares, w.vwap, fee_rate)
        return Fill("partial" if w.exhausted else "filled", w.shares, w.cost, w.vwap, f,
                    w.vwap, limit)

    def sell(self, token_id: str, shares: float, book: dict, *, limit: float, tick: float,
             fee_rate: float) -> Fill:
        levels = [(p, s) for p, s in (book.get("bids") or []) if p >= limit - 1e-9]
        if not levels:
            bid = bk.best_bid(book)
            return Fill("rejected", limit_price=limit,
                        reason=f"no bid at or above {limit:.3f} (best {bid})")
        w = bk._walk(levels, shares=shares)
        f = bk.fee(w.shares, w.vwap, fee_rate)
        return Fill("partial" if w.exhausted else "filled", w.shares, w.cost, w.vwap, f,
                    w.vwap, limit)

    def place_resting_sell(self, token_id: str, shares: float, price: float, *,
                           tick: float) -> Resting:
        return Resting("open", bk.round_to_tick(price, tick, side="SELL"), shares,
                       exchange_id=None)

    def cancel(self, exchange_id: str) -> bool:
        return True

    def resting_fill(self, order: dict, book: dict, *, fee_rate: float) -> Fill | None:
        """A resting sell fills when someone bids up to it.

        Modelled as: the best bid reached our price, so our order -- which was on the book
        before that bid arrived -- was hit. This ignores queue position, which is the same
        optimism the taker path carries and in the same direction.
        """
        best = bk.best_bid(book)
        if best is None or best < order["price"] - 1e-9:
            return None
        shares = order["shares"]
        price = order["price"]
        f = bk.fee(shares, price, fee_rate)
        return Fill("filled", shares, shares * price, price, f, price, price)


class LiveExecutor(Executor):
    """Real orders, real money, on Polygon.

    Requires `py-clob-client` (an optional dependency, installed with the `live` extra) and a
    funded Polymarket account. Credentials come from the environment and are never written to
    the database, the raw dump, or a log line:

        POLYMARKET_PRIVATE_KEY   the EOA key that signs orders
        POLYMARKET_FUNDER        the proxy address holding the USDC (your Polymarket address)
        POLYMARKET_API_KEY/_SECRET/_PASSPHRASE   optional; derived from the key if absent

    Construction alone does not trade, but it does authenticate. Nothing above this module ever
    instantiates it without an explicit --mode live on the command line.
    """

    mode = "live"

    def __init__(self, host: str | None = None, chain_id: int = CLOB_CHAIN_ID):
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError as e:  # pragma: no cover - depends on an optional install
            raise RuntimeError(
                "live mode needs py-clob-client: uv pip install 'polywatch[live]'"
            ) from e

        key = os.environ.get(ENV_PRIVATE_KEY)
        funder = os.environ.get(ENV_FUNDER)
        if not key:
            raise RuntimeError(f"live mode needs {ENV_PRIVATE_KEY} in the environment")
        if not funder:
            raise RuntimeError(f"live mode needs {ENV_FUNDER} (your Polymarket proxy address)")

        from ..config import CLOB
        creds = None
        vals = [os.environ.get(k) for k in ENV_API_CREDS]
        if all(vals):
            creds = ApiCreds(api_key=vals[0], api_secret=vals[1], api_passphrase=vals[2])

        # signature_type=1 is the email/magic proxy wallet; 2 is a browser wallet's proxy. Both
        # route through `funder`, which is why the funder address is required rather than
        # derived: the signing key and the account holding the money are not the same address.
        self.client = ClobClient(host or CLOB, key=key, chain_id=chain_id,
                                 signature_type=1, funder=funder, creds=creds)
        if creds is None:
            self.client.set_api_creds(self.client.create_or_derive_api_creds())

    # --- helpers ------------------------------------------------------------------------

    def _post(self, token_id: str, price: float, shares: float, side: str, order_type: str):
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        args = OrderArgs(price=round(price, 6), size=round(shares, 2),
                         side=BUY if side == "BUY" else SELL, token_id=token_id)
        signed = self.client.create_order(args)
        return self.client.post_order(signed, getattr(OrderType, order_type))

    @staticmethod
    def _filled(resp: dict) -> tuple[float, float]:
        """(shares, usdc) actually matched, from whichever field the server used.

        The CLOB reports matched amounts inconsistently between order types and versions, so
        this reads the fields that exist and returns zeros rather than guessing. A zero here
        means "the response did not say", which the caller reports as a partial rather than
        booking a fill that may not have happened.
        """
        for shares_key, usd_key in (("sizeMatched", "makingAmount"), ("size_matched", "amount")):
            if shares_key in resp:
                try:
                    return float(resp.get(shares_key) or 0), float(resp.get(usd_key) or 0)
                except (TypeError, ValueError):
                    return 0.0, 0.0
        return 0.0, 0.0

    # --- interface ----------------------------------------------------------------------

    def buy(self, token_id: str, usd: float, book: dict, *, limit: float, tick: float,
            fee_rate: float, min_shares: float) -> Fill:
        w = bk.buy_for_usd(book, usd)
        if not w.filled:
            return Fill("rejected", limit_price=limit, reason="empty ask side")
        shares = round(usd / limit, 2)
        if not bk.meets_min_size(shares, min_shares):
            return Fill("rejected", limit_price=limit, book_vwap=w.vwap,
                        reason=f"{shares:.1f} shares below market minimum {min_shares:g}")
        try:
            resp = self._post(token_id, limit, shares, "BUY", "FAK")
        except Exception as e:  # noqa: BLE001 - any failure here must not kill the loop
            return Fill("rejected", limit_price=limit, book_vwap=w.vwap,
                        reason=f"{type(e).__name__}: {e}")
        if not resp.get("success", True):
            return Fill("rejected", limit_price=limit, book_vwap=w.vwap,
                        reason=str(resp.get("errorMsg") or resp)[:200])
        got, spent = self._filled(resp)
        if got <= 0:
            return Fill("rejected", limit_price=limit, book_vwap=w.vwap,
                        exchange_id=resp.get("orderID"),
                        reason="accepted but nothing matched (FAK killed)")
        avg = spent / got if spent > 0 else limit
        return Fill("filled" if got >= shares - 0.01 else "partial", got, got * avg, avg,
                    bk.fee(got, avg, fee_rate), w.vwap, limit,
                    exchange_id=resp.get("orderID"))

    def sell(self, token_id: str, shares: float, book: dict, *, limit: float, tick: float,
             fee_rate: float) -> Fill:
        w = bk.sell_shares(book, shares)
        try:
            resp = self._post(token_id, limit, round(shares, 2), "SELL", "FAK")
        except Exception as e:  # noqa: BLE001
            return Fill("rejected", limit_price=limit, book_vwap=w.vwap,
                        reason=f"{type(e).__name__}: {e}")
        if not resp.get("success", True):
            return Fill("rejected", limit_price=limit, book_vwap=w.vwap,
                        reason=str(resp.get("errorMsg") or resp)[:200])
        got, got_usd = self._filled(resp)
        if got <= 0:
            return Fill("rejected", limit_price=limit, book_vwap=w.vwap,
                        exchange_id=resp.get("orderID"), reason="accepted but nothing matched")
        avg = got_usd / got if got_usd > 0 else limit
        return Fill("filled" if got >= shares - 0.01 else "partial", got, got * avg, avg,
                    bk.fee(got, avg, fee_rate), w.vwap, limit,
                    exchange_id=resp.get("orderID"))

    def place_resting_sell(self, token_id: str, shares: float, price: float, *,
                           tick: float) -> Resting:
        px = bk.round_to_tick(price, tick, side="SELL")
        try:
            resp = self._post(token_id, px, round(shares, 2), "SELL", "GTC")
        except Exception as e:  # noqa: BLE001
            return Resting("rejected", px, shares, reason=f"{type(e).__name__}: {e}")
        if not resp.get("success", True):
            return Resting("rejected", px, shares,
                           reason=str(resp.get("errorMsg") or resp)[:200])
        return Resting("open", px, shares, exchange_id=resp.get("orderID"))

    def cancel(self, exchange_id: str) -> bool:
        try:
            resp = self.client.cancel(order_id=exchange_id)
        except Exception:  # noqa: BLE001
            return False
        return bool(resp)

    def resting_fill(self, order: dict, book: dict, *, fee_rate: float) -> Fill | None:
        """Ask the exchange, not the book -- live, the order either matched or it did not."""
        oid = order.get("exchange_id")
        if not oid:
            return None
        try:
            live = self.client.get_order(oid)
        except Exception:  # noqa: BLE001
            return None
        matched = float(live.get("size_matched") or 0)
        if matched <= 0:
            return None
        price = float(live.get("price") or order["price"])
        status = str(live.get("status") or "").upper()
        done = matched >= order["shares"] - 0.01 or status in ("MATCHED", "FILLED")
        return Fill("filled" if done else "partial", matched, matched * price, price,
                    bk.fee(matched, price, fee_rate), price, price, exchange_id=oid)


def make(mode: str) -> Executor:
    if mode == "paper":
        return PaperExecutor()
    if mode == "live":
        return LiveExecutor()
    raise ValueError(f"unknown mode {mode!r}")
