"""A live fill feed for the wallets we copy, read from Polygon instead of from data-api.

stream.py opens by saying that entry latency "cannot be engineered away -- a third party's fills
can only be polled". That was measured, and it was wrong in one specific way worth spelling out,
because the mistake is an easy one to make twice.

It is true of Polymarket's websockets. The CLOB market channel carries no wallet address, and the
user channel reports only your own account. Both were probed; neither can tell you that someone
else just bought. The conclusion drawn from that -- that the fill is unobservable until data-api
indexes it -- skipped a source that was never Polymarket's to gate: the chain the fill settles on.

Probed 2026-09-12, and the numbers are the whole argument:

  * data-api's `timestamp` for a fill IS the Polygon block timestamp. Not close to it; equal to
    it, to the second. So the 13-21s of measured "feed lag" was never the trade being unknowable.
    It was the indexer's queue, and we were sitting in it.
  * `eth_subscribe` on free public RPC delivers blocks at, or a shade before, the timestamp those
    blocks claim. There is no meaningful queue to sit in.
  * The exchange's OrderFilled event carries the maker as an *indexed* topic, so the node filters
    to our roster before a byte crosses the wire. One block carries ~16 of these unfiltered.
  * The event body reconstructs data-api's own record exactly -- token, size, usdc, price all
    identical to the cent and the share, verified against a captured fill in
    tests/fixtures/orderfilled_log.json.

The same three properties stream.py insists on apply here, for the same reasons:

  * **Optional.** `websocket-client` is an extra. Absent it, `connect` returns None and the
    engine polls exactly as it did before.
  * **Failing means falling back, never blocking.** Both sockets dead means `live` is False and
    the 3s /activity poll carries the run at its old latency. This module can only ever make the
    engine earlier, never wrong and never stuck.
  * **Nothing here decides anything.** It decodes and enqueues. Every gate, every cap, every
    breaker and the whole execution path stay where they are and see the same shaped dict the
    poller hands them.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import OrderedDict

from ..config import (CHAIN_BLOCK_TS_CAP, CHAIN_CONNECT_TIMEOUT_S, CHAIN_DECIMALS,
                      CHAIN_EXCHANGES, CHAIN_ORDER_FILLED_TOPIC, CHAIN_READ_TIMEOUT_S,
                      CHAIN_RECONNECT_BASE_S, CHAIN_RECONNECT_CAP_S, CHAIN_SEEN_CAP, CHAIN_WSS)

SCALE = 10 ** CHAIN_DECIMALS

# Amounts and prices are rounded to this many places on both the chain path and the /activity
# path. Not cosmetic: `signals` is UNIQUE on (run_id, tx_hash, token_id, side, size) with `size`
# stored as REAL, and the two paths compute that float differently -- one divides an integer by
# 1e6, the other parses a decimal string. A value like 825.09091 can land a single ULP apart,
# the UNIQUE constraint then does not fire, and the run buys the same fill twice. Rounding both
# to the scale the exchange actually quotes in makes them bit-identical.
ROUND_DP = CHAIN_DECIMALS


def available() -> bool:
    """Is the optional websocket dependency installed?"""
    try:
        import websocket  # noqa: F401
    except ImportError:
        return False
    return True


def topic_for_address(address: str) -> str:
    """An address as a 32-byte log topic: left-padded to a full word, lowercase."""
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def _words(data: str) -> list[int]:
    body = data.removeprefix("0x")
    return [int(body[i:i + 64], 16) for i in range(0, len(body) - len(body) % 64, 64)]


def decode_order_filled(log: dict, wallet: str, ts: int, resolve=None) -> dict | None:
    """One OrderFilled log, seen from `wallet`'s side, as an /activity-shaped event.

    Returns None when the log is not an OrderFilled, not this wallet's, or malformed -- a frame
    we cannot read is a frame we ignore, never one we guess at.

    The body's first word is a side flag written from the maker's point of view -- 0 for a maker
    who paid collateral, 1 for one who delivered the token -- and the taker is the other side of
    the same trade. So the side depends on *which* party we are, which is why the roster is
    subscribed on both the maker and the taker topic rather than assuming end users only ever
    appear in one slot.

    `resolve` maps a token id to (condition_id, outcome, outcome_index). It is injected rather
    than imported so this function stays pure and offline-testable; when it is absent or returns
    nothing, the market fields come back empty and the caller decides what to do about it.

    It must not touch the database. This runs on the socket threads, a sqlite3 connection belongs
    to the thread that opened it, and the engine resolves on its own thread in `drain_trades` for
    exactly that reason. In practice nothing passes a resolver here at all; the parameter exists
    so the decoder can be tested end to end without one.
    """
    topics = log.get("topics") or []
    if len(topics) < 4 or topics[0].lower() != CHAIN_ORDER_FILLED_TOPIC:
        return None
    try:
        w = _words(log.get("data") or "")
    except ValueError:
        return None
    if len(w) < 4:
        return None

    want = wallet.lower()
    maker = "0x" + topics[2][-40:].lower()
    taker = "0x" + topics[3][-40:].lower()
    if want == maker:
        flip = False
    elif want == taker:
        flip = True
    else:
        return None

    # The body is (side, tokenId, makerAmountFilled, takerAmountFilled, fee, _, _), and the
    # first word is a flag rather than an asset id: 0 means the maker paid collateral and took
    # the outcome token, 1 means they delivered the token and took collateral. An earlier cut
    # read word0 as `makerAssetId` and inferred the side from which leg held asset id 0. That
    # is right for a buy by coincidence -- the flag is 0 there -- and wrong for every sell,
    # where the flag is 1 and no leg holds 0. It silently dropped every sell a copied wallet
    # made: measured at 52.8% coverage overall, and 0% on a wallet that happened to be selling.
    # Whichever amount is the token side follows from the flag, and the taker is simply the
    # other side of the same trade.
    flag, token = w[0], w[1]
    maker_gave, maker_got = w[2], w[3]
    if flag == 0:
        side, shares, usdc = "BUY", maker_got, maker_gave
    elif flag == 1:
        side, shares, usdc = "SELL", maker_gave, maker_got
    else:
        return None
    if flip:
        side = "SELL" if side == "BUY" else "BUY"
    if token == 0 or shares <= 0:
        return None

    size = round(shares / SCALE, ROUND_DP)
    usdc_size = round(usdc / SCALE, ROUND_DP)
    token_id = str(token)

    condition_id, outcome, outcome_index = "", None, None
    if resolve is not None:
        found = resolve(token_id)
        if found:
            condition_id, outcome, outcome_index = found

    return {
        "wallet": want,
        "kind": "TRADE",
        "token_id": token_id,
        "condition_id": condition_id,
        "side": side,
        "size": size,
        "price": round(usdc / shares, ROUND_DP),
        "usdc_size": usdc_size,
        "ts": ts,
        "outcome": outcome,
        "outcome_index": outcome_index,
        "tx_hash": (log.get("transactionHash") or "").lower(),
        "title": None,
        "slug": None,
        # Not part of the /activity shape. Carried so the report can say which source saw a fill
        # first, which is the only way to know whether any of this is earning its keep.
        "source": "chain",
    }


class TradeStream:
    """Fills by the copied wallets, off the chain, on background threads.

    One thread per endpoint, both feeding one queue. They race: whichever node pushes a given log
    first wins and the other copy is dropped on a (tx_hash, log_index) seen-set. Two endpoints
    rather than one because a single free RPC going quiet should cost the run nothing, and
    because the cost of the second is a socket.
    """

    def __init__(self, addresses, urls=CHAIN_WSS, log=print, resolve=None):
        self.addresses = {a.lower() for a in addresses if a}
        self.urls = tuple(urls)
        self.log = log
        self.resolve = resolve
        self.events: "queue.Queue[dict]" = queue.Queue()
        # Set whenever a fill is enqueued, so the engine can wait for one instead of sleeping
        # through it. Cleared by whoever consumes it.
        self.changed = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sockets: dict[str, object] = {}
        self._lock = threading.Lock()
        self._seen: "OrderedDict[tuple, bool]" = OrderedDict()
        self._block_ts: "OrderedDict[int, int]" = OrderedDict()
        self._known_exchanges = {e.lower() for e in CHAIN_EXCHANGES}
        self.connects = 0
        self.received = 0
        self.duplicates = 0
        self.undated = 0

    # --- lifecycle ----------------------------------------------------------------------

    def start(self) -> "TradeStream":
        for i, url in enumerate(self.urls):
            t = threading.Thread(target=self._run, args=(url,),
                                 name=f"polywatch-trades-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sockets = list(self._sockets.values())
        for ws in sockets:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - shutting down; nothing here is worth raising over
                pass

    @property
    def live(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    @property
    def connected(self) -> bool:
        """At least one socket is currently open, as opposed to merely retrying."""
        with self._lock:
            return bool(self._sockets)

    # --- reading ------------------------------------------------------------------------

    def drain(self, limit: int = 256) -> list[dict]:
        """Every fill enqueued since the last call, oldest first.

        Bounded so that a burst -- or a backlog after a stall -- cannot hold the engine inside
        one drain while the exit ladder goes unchecked. Whatever is left stays queued, and the
        wait loop comes straight back for it.
        """
        out: list[dict] = []
        while len(out) < limit:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        out.sort(key=lambda e: e["ts"])
        return out

    # --- the sockets --------------------------------------------------------------------

    def _subscriptions(self) -> list[dict]:
        """newHeads for block timestamps, then the roster on both sides of a match.

        Filtered by event signature and by our wallets, deliberately not by contract address.
        Address filtering is how the first cut lost fills silently: a live coverage check found
        a third exchange contract nobody had written down, and a fourth is only a deployment
        away. A log still has to name one of the copied wallets to reach us, which is filter
        enough -- the roster is the narrow part, not the address.
        """
        roster = sorted(topic_for_address(a) for a in self.addresses)
        return [
            {"method": "eth_subscribe", "params": ["newHeads"]},
            {"method": "eth_subscribe", "params": ["logs", {
                "topics": [CHAIN_ORDER_FILLED_TOPIC, None, roster]}]},
            {"method": "eth_subscribe", "params": ["logs", {
                "topics": [CHAIN_ORDER_FILLED_TOPIC, None, None, roster]}]},
        ]

    def _run(self, url: str) -> None:
        import websocket

        delay = CHAIN_RECONNECT_BASE_S
        while not self._stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(url, timeout=CHAIN_CONNECT_TIMEOUT_S)
                for i, sub in enumerate(self._subscriptions()):
                    ws.send(json.dumps({"jsonrpc": "2.0", "id": i + 1, **sub}))
                ws.settimeout(CHAIN_READ_TIMEOUT_S)
                with self._lock:
                    self._sockets[url] = ws
                self.connects += 1
                delay = CHAIN_RECONNECT_BASE_S
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        # A quiet roster is the normal case, not a dead socket. Ping and wait.
                        ws.ping()
                        continue
                    if not raw:
                        break
                    self._ingest(raw)
            except Exception as e:  # noqa: BLE001 - a dead socket is a fallback, not a crash
                if self._stop.is_set():
                    return
                self.log(f"  ~ chain stream {url.split('//')[-1]}: {type(e).__name__}: {e}; "
                         f"retrying, /activity still covering")
            finally:
                with self._lock:
                    self._sockets.pop(url, None)
                try:
                    if ws is not None:
                        ws.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._stop.wait(delay):
                return
            delay = min(CHAIN_RECONNECT_CAP_S, delay * 2)

    def _ingest(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict) or msg.get("method") != "eth_subscription":
            return
        result = (msg.get("params") or {}).get("result")
        if not isinstance(result, dict):
            return
        if "number" in result and "timestamp" in result:
            self._note_block(result)
        elif "topics" in result:
            self._note_log(result)

    def _note_block(self, head: dict) -> None:
        try:
            number = int(head["number"], 16)
            ts = int(head["timestamp"], 16)
        except (KeyError, TypeError, ValueError):
            return
        with self._lock:
            self._block_ts[number] = ts
            while len(self._block_ts) > CHAIN_BLOCK_TS_CAP:
                self._block_ts.popitem(last=False)

    def _note_log(self, log: dict) -> None:
        # A reorged-out log is withdrawn, not acted on. Polygon reorgs are shallow and a copy is
        # our own position at our own price, so this is a bad signal rather than a broken ledger
        # -- but there is no reason to trade on one we have been told to forget.
        if log.get("removed"):
            return
        key = (log.get("transactionHash"), log.get("logIndex"))
        with self._lock:
            if key in self._seen:
                self.duplicates += 1
                return
            self._seen[key] = True
            while len(self._seen) > CHAIN_SEEN_CAP:
                self._seen.popitem(last=False)

        source = (log.get("address") or "").lower()
        if source and source not in self._known_exchanges:
            self._known_exchanges.add(source)
            self.log(f"  ~ chain stream: fills arriving from an exchange contract not in "
                     f"CHAIN_EXCHANGES ({source}); decoding it, worth writing down")

        ts = self._timestamp_for(log)
        topics = log.get("topics") or []
        if len(topics) < 4:
            return
        # A match between two wallets we both copy is two events, not one: each side traded.
        parties = {"0x" + topics[2][-40:].lower(), "0x" + topics[3][-40:].lower()}
        for wallet in sorted(parties & self.addresses):
            try:
                ev = decode_order_filled(log, wallet, ts, self.resolve)
            except Exception as e:  # noqa: BLE001 - a malformed frame must not kill the socket
                self.log(f"  ~ chain stream: undecodable log "
                         f"{str(log.get('transactionHash'))[:12]}: {type(e).__name__}: {e}")
                continue
            if ev is None:
                continue
            self.received += 1
            self.events.put(ev)
            self.changed.set()

    def _timestamp_for(self, log: dict) -> int:
        """The block's own timestamp, or the clock when we have not been told it yet.

        Falling back to the clock overstates our speed by up to a block, so it is counted. It is
        still the honest choice: the alternative is an eth_getBlockByNumber round trip on the one
        path whose entire purpose is not making round trips.
        """
        try:
            number = int(log["blockNumber"], 16)
        except (KeyError, TypeError, ValueError):
            self.undated += 1
            return int(time.time())
        with self._lock:
            ts = self._block_ts.get(number)
        if ts is None:
            self.undated += 1
            return int(time.time())
        return ts


def connect(addresses, log=print, resolve=None) -> "TradeStream | None":
    """Start a chain trade stream, or return None when one is not available.

    None is not an error and is not reported as one: polling /activity is the supported path and
    this is an accelerator bolted onto it.
    """
    if not available():
        return None
    if not addresses:
        return None
    try:
        return TradeStream(addresses, log=log, resolve=resolve).start()
    except Exception as e:  # noqa: BLE001
        log(f"  ~ chain stream unavailable ({type(e).__name__}: {e}); /activity only")
        return None
