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
from dataclasses import dataclass

from ..config import (CLOB_CHAIN_ID, DEFAULT_SIGNATURE_TYPE, ENV_API_CREDS, ENV_FUNDER,
                      ENV_PRIVATE_KEY, ENV_SIGNATURE_TYPE)
from . import book as bk


@dataclass
class Fill:
    """What an order actually did. `status` is what the engine branches on.

    'unknown' is the important one and it is not a synonym for 'rejected'. It means the exchange
    accepted the order and its response did not say what happened -- the shares may exist. The
    engine must not book a position from a guess, and must not assume nothing happened either;
    it records the order and hands the question to reconciliation.
    """
    status: str                 # 'filled' | 'partial' | 'rejected' | 'unknown'
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

    @property
    def unknown(self) -> bool:
        return self.status == "unknown"


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
        """Has a resting order filled *further*? None while it rests untouched.

        `order["shares"]` is the size the order was posted at and never changes;
        `order["filled_shares"]` is how much of it the engine has already booked. An
        implementation must return only the *new* shares, because the caller settles the
        position by them. Returning the running total instead books the first partial fill
        again on every subsequent tick, which inflates realized PnL by exactly the amount the
        take-profit was supposed to earn.
        """
        raise NotImplementedError

    def order_status(self, exchange_id: str) -> dict | None:
        """The exchange's own view of one order, for reconciliation. None when it cannot say."""
        return None

    def account_positions(self) -> dict[str, float] | None:
        """{token_id: shares} the exchange believes this account holds.

        None means "cannot be determined", which is different from an empty dict. Reconciliation
        treats the two very differently: an empty account is a fact, an unanswerable one is a
        reason to stop.
        """
        return None


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
        """A resting sell fills when bids arrive at or above it, and only for as much as they buy.

        Modelled as: every bid resting at or above our price would have crossed with us, so our
        order -- which was there first -- was hit for that much and no more. Filling the whole
        order off a one-share bid is not optimism about queue position, it is an invention of
        liquidity, and it would flatter exactly the exit that carries most of a run's PnL.

        We are the maker here, so the fill is at our own price rather than at the taker's.
        Queue position is still ignored, which is the same optimism the taker path carries and
        in the same direction.
        """
        price = order["price"]
        remaining = order["shares"] - float(order.get("filled_shares") or 0.0)
        if remaining <= 1e-6:
            return None
        levels = [(p, sz) for p, sz in (book.get("bids") or []) if p >= price - 1e-9]
        if not levels:
            return None
        w = bk._walk(levels, shares=remaining)
        if not w.filled:
            return None
        f = bk.fee(w.shares, price, fee_rate)
        return Fill("filled" if not w.exhausted else "partial", w.shares, w.shares * price,
                    price, f, price, price)


class LiveExecutor(Executor):
    """Real orders, real money, on Polygon.

    Requires `py-clob-client` (an optional dependency, installed with the `live` extra) and a
    funded Polymarket account. Credentials come from the environment and are never written to
    the database, the raw dump, or a log line:

        POLYMARKET_PRIVATE_KEY   the EOA key that signs orders
        POLYMARKET_FUNDER        the proxy address holding the USDC (your Polymarket address)
        POLYMARKET_API_KEY/_SECRET/_PASSPHRASE   optional; derived from the key if absent
        POLYMARKET_SIGNATURE_TYPE  1 (default) email/magic proxy, 2 browser-wallet proxy, 0 EOA

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

        # signature_type=1 is the email/magic proxy wallet, 2 a browser wallet's proxy, 0 a bare
        # EOA. 1 and 2 route through `funder`, which is why the funder address is required rather
        # than derived: the signing key and the account holding the money are not the same
        # address. Guessing wrong is not dangerous but it is opaque -- every order comes back
        # rejected at the signature check with nothing said about the wallet type -- so it is a
        # setting rather than a constant.
        raw = os.environ.get(ENV_SIGNATURE_TYPE)
        try:
            sig_type = DEFAULT_SIGNATURE_TYPE if raw in (None, "") else int(raw)
        except ValueError:
            raise RuntimeError(f"{ENV_SIGNATURE_TYPE}={raw!r} is not 0, 1 or 2") from None
        if sig_type not in (0, 1, 2):
            raise RuntimeError(f"{ENV_SIGNATURE_TYPE}={raw!r} is not 0, 1 or 2")
        self.signature_type = sig_type
        self.client = ClobClient(host or CLOB, key=key, chain_id=chain_id,
                                 signature_type=sig_type, funder=funder, creds=creds)
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

    # Every field name the CLOB has been observed to report a matched amount under, paired with
    # the notional field that accompanies it. The list is additive: a new server version adds a
    # pair here rather than changing how an unreadable response is treated.
    MATCH_FIELDS = (("sizeMatched", "makingAmount"), ("size_matched", "amount"),
                    ("sizeMatched", "takingAmount"), ("matched_size", "matched_amount"),
                    ("filledSize", "filledAmount"))

    @classmethod
    def _filled(cls, resp: dict) -> tuple[float, float] | None:
        """(shares, usdc) actually matched -- or None when the response does not say.

        The distinction between "matched nothing" and "did not say" is the whole point of this
        function, and getting it wrong is the one failure this program cannot reconcile
        afterwards. A response carrying `sizeMatched: 0` is a fact: the FAK order was killed and
        no shares exist. A response carrying no field we recognise is not a fact about the
        order, it is a fact about our parser -- the shares may well be sitting in the account.

        Returning zeros for both cases, as this used to, books a real fill as a rejection and
        leaves shares on the exchange that the database has no row for. So: zeros only when the
        server said zero, None otherwise, and the caller turns None into `unknown` rather than
        `rejected`.
        """
        if not isinstance(resp, dict):
            return None
        for shares_key, usd_key in cls.MATCH_FIELDS:
            if shares_key not in resp:
                continue
            try:
                return float(resp.get(shares_key) or 0), float(resp.get(usd_key) or 0)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _implausible(avg: float, *, side: str, limit: float, touch: float | None) -> bool:
        """Is this average price one the order could not actually have achieved?

        A taker fill lies between the touch and the limit we signed: a buy pays at least the
        best ask and never more than its limit, a sell receives at most the best bid and never
        less than its limit. An average outside that band is not a surprising fill, it is a
        misread response -- and the response is genuinely ambiguous, because `makingAmount` is
        USDC on a buy and shares on a sell, so pairing it with a share count yields exactly
        1.000 when the two are the same number.

        Booking that would record a sale at a dollar a share and hand the run a profit it never
        made. `unknown` is the honest status: the shares moved, the price did not survive the
        round trip, and reconciliation asks the exchange rather than this parser.
        """
        if avg <= 0:
            return True
        tol = 0.02                     # a couple of ticks of slack for rounding on both sides
        if side == "BUY":
            if avg > limit + tol:
                return True
            return touch is not None and avg < touch - tol
        if avg < limit - tol:
            return True
        return touch is not None and avg > touch + tol

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
        matched = self._filled(resp)
        if matched is None:
            return Fill("unknown", limit_price=limit, book_vwap=w.vwap,
                        exchange_id=resp.get("orderID"),
                        reason="accepted, but the response did not say what matched")
        got, spent = matched
        if got <= 0:
            return Fill("rejected", limit_price=limit, book_vwap=w.vwap,
                        exchange_id=resp.get("orderID"),
                        reason="accepted but nothing matched (FAK killed)")
        avg = spent / got if spent > 0 else limit
        if self._implausible(avg, side="BUY", limit=limit, touch=bk.best_ask(book)):
            return Fill("unknown", limit_price=limit, book_vwap=w.vwap,
                        exchange_id=resp.get("orderID"),
                        reason=f"matched {got:.2f} shares at an unreadable price ({avg:.4f})")
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
        matched = self._filled(resp)
        if matched is None:
            return Fill("unknown", limit_price=limit, book_vwap=w.vwap,
                        exchange_id=resp.get("orderID"),
                        reason="accepted, but the response did not say what matched")
        got, got_usd = matched
        if got <= 0:
            return Fill("rejected", limit_price=limit, book_vwap=w.vwap,
                        exchange_id=resp.get("orderID"), reason="accepted but nothing matched")
        avg = got_usd / got if got_usd > 0 else limit
        if self._implausible(avg, side="SELL", limit=limit, touch=bk.best_bid(book)):
            return Fill("unknown", limit_price=limit, book_vwap=w.vwap,
                        exchange_id=resp.get("orderID"),
                        reason=f"matched {got:.2f} shares at an unreadable price ({avg:.4f})")
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

    def order_status(self, exchange_id: str) -> dict | None:
        """The exchange's own view of one order. None when it cannot be reached or does not know."""
        if not exchange_id:
            return None
        try:
            return self.client.get_order(exchange_id)
        except Exception:  # noqa: BLE001 - reconciliation treats "cannot say" as its own answer
            return None

    def account_positions(self) -> dict[str, float] | None:
        """{token_id: shares} the exchange believes the funder account holds.

        Read from data-api rather than from the CLOB because /positions is the same view the
        Polymarket UI shows, which is the view an operator will check this against.
        """
        from ..fetch import polymarket as api
        from ..fetch.client import Client, FetchError
        funder = os.environ.get(ENV_FUNDER)
        if not funder:
            return None
        try:
            payload = api.positions(Client(con=None, dump_raw=False, log_ok=False), funder)
        except FetchError:
            return None
        out: dict[str, float] = {}
        for rec in payload or []:
            token = rec.get("asset")
            if token:
                out[str(token)] = out.get(str(token), 0.0) + float(rec.get("size") or 0.0)
        return out

    def resting_fill(self, order: dict, book: dict, *, fee_rate: float) -> Fill | None:
        """Ask the exchange, not the book -- live, the order either matched or it did not."""
        oid = order.get("exchange_id")
        if not oid:
            return None
        try:
            live = self.client.get_order(oid)
        except Exception:  # noqa: BLE001
            return None
        # `size_matched` is cumulative over the life of the order, so the new shares are what
        # it reports minus what we have already settled against the position.
        matched = float(live.get("size_matched") or 0)
        already = float(order.get("filled_shares") or 0.0)
        fresh = matched - already
        if fresh <= 1e-6:
            return None
        price = float(live.get("price") or order["price"])
        status = str(live.get("status") or "").upper()
        done = matched >= order["shares"] - 0.01 or status in ("MATCHED", "FILLED")
        return Fill("filled" if done else "partial", fresh, fresh * price, price,
                    bk.fee(fresh, price, fee_rate), price, price, exchange_id=oid)


def make(mode: str) -> Executor:
    if mode == "paper":
        return PaperExecutor()
    if mode == "live":
        return LiveExecutor()
    raise ValueError(f"unknown mode {mode!r}")
