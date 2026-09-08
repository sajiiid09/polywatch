"""A live book feed, so the exit ladder stops being fifteen seconds coarse.

The asymmetry this exists to fix: entry latency is bounded by data-api's activity cache and
cannot be engineered away -- a third party's fills can only be polled, because the market
websocket carries no wallet address and the user websocket reports only your own account. Exit
latency has no such excuse. The stop-loss, the trailing stop and the time stop are enforced by
this process, and until now the process looked at them once per poll. In a market that moves in
seconds, a fifteen-second stop-loss is a fifteen-second option written against us for free.

The CLOB market channel does exactly what is needed here: subscribe with a list of token ids and
it pushes a `book` snapshot and then `price_change` deltas as they happen. This module keeps
those in memory in the same shape `parse.records.parse_book` produces, so the engine reads a
streamed book and a fetched one through the same code.

Three deliberate properties:

  * **Optional.** `websocket-client` lives in the `stream` extra. Absent it, `connect` returns
    None and the engine polls exactly as before. The standard-library-only guarantee for paper
    mode and every analytic path is intact.
  * **Failing means falling back, never blocking.** A dropped socket, a slow one, or a token the
    feed has not sent yet all resolve to `book()` returning None, which sends the caller to HTTP.
    A stale price is far more dangerous here than a slow one.
  * **Staleness is measured, not assumed.** Every cached book carries the time it was last
    touched, and one older than `MAX_BOOK_AGE_S` is treated as absent. A socket that is open but
    silent is the failure mode that would otherwise be invisible.
"""

from __future__ import annotations

import json
import threading
import time

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# How long a streamed book is trusted. Polymarket pushes on change rather than on a heartbeat,
# so a quiet book is normal and this is not a liveness check -- it is a bound on how wrong the
# cached price can be before we would rather pay for a fresh one.
MAX_BOOK_AGE_S = 20.0

# How long to wait after a drop before reconnecting, and the ceiling on that backoff.
RECONNECT_BASE_S = 1.0
RECONNECT_CAP_S = 30.0


def available() -> bool:
    """Is the optional websocket dependency installed?"""
    try:
        import websocket  # noqa: F401
    except ImportError:
        return False
    return True


class BookStream:
    """Live order books for a set of tokens, maintained on a background thread.

    Thread-safety is one lock around one dict. The engine only ever reads whole book objects and
    the socket thread only ever replaces them, so no reader can observe a half-updated book.
    """

    def __init__(self, url: str = WS_URL, log=print, max_age_s: float = MAX_BOOK_AGE_S):
        self.url = url
        self.log = log
        self.max_age_s = max_age_s
        self._books: dict[str, tuple[float, dict]] = {}
        self._tokens: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ws = None
        self._stop = threading.Event()
        self.updates = 0
        self.connects = 0
        # Set whenever a book changes, so a caller can wait for movement instead of sleeping
        # through it. Cleared by whoever consumes it.
        self.changed = threading.Event()

    # --- lifecycle ----------------------------------------------------------------------

    def start(self, tokens: list[str]) -> "BookStream":
        self._tokens = {t for t in tokens if t}
        self._thread = threading.Thread(target=self._run, name="polywatch-books", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:  # noqa: BLE001 - shutting down; nothing here is worth raising over
            pass

    def watch(self, tokens: list[str]) -> None:
        """Add tokens to the subscription, cycling the socket so it takes effect now.

        The CLOB's subscribe message is sent once per connection, so a new token means a new
        connection. That sounds expensive and is not: on subscribe the server sends a fresh
        snapshot for every token in the list, so reconnecting re-establishes the books we
        already had rather than discarding them. The cost is a second or two of falling back to
        HTTP, and the alternative -- a position we hold whose book is never streamed -- is
        exactly the position whose stop-loss most needs watching.
        """
        new = {t for t in tokens if t} - self._tokens
        if not new:
            return
        self._tokens |= new
        if self._thread is None:
            return
        try:
            if self._ws is not None:
                self._ws.close()      # the run loop reconnects with the wider subscription
        except Exception:  # noqa: BLE001
            pass

    # --- reading ------------------------------------------------------------------------

    def book(self, token_id: str) -> dict | None:
        """The current book, or None when it is absent or too old to trust."""
        with self._lock:
            entry = self._books.get(token_id)
        if entry is None:
            return None
        ts, book = entry
        if time.monotonic() - ts > self.max_age_s:
            return None
        return book

    def age(self, token_id: str) -> float | None:
        with self._lock:
            entry = self._books.get(token_id)
        return None if entry is None else time.monotonic() - entry[0]

    @property
    def live(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- the socket ---------------------------------------------------------------------

    def _run(self) -> None:
        import websocket

        delay = RECONNECT_BASE_S
        while not self._stop.is_set():
            try:
                self._ws = websocket.create_connection(self.url, timeout=10)
                self._ws.send(json.dumps({"assets_ids": sorted(self._tokens), "type": "market"}))
                self.connects += 1
                delay = RECONNECT_BASE_S
                while not self._stop.is_set():
                    raw = self._ws.recv()
                    if not raw:
                        break
                    self._ingest(raw)
            except Exception as e:  # noqa: BLE001 - a dead socket is a fallback, not a crash
                if self._stop.is_set():
                    return
                self.log(f"  ~ book stream: {type(e).__name__}: {e}; falling back to polling")
            finally:
                try:
                    if self._ws is not None:
                        self._ws.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._stop.wait(delay):
                return
            delay = min(RECONNECT_CAP_S, delay * 2)

    def _ingest(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        for msg in (payload if isinstance(payload, list) else [payload]):
            if not isinstance(msg, dict):
                continue
            token = msg.get("asset_id") or msg.get("market")
            if not token:
                continue
            event = msg.get("event_type")
            if event == "book":
                self._store(token, msg)
            elif event == "price_change":
                self._apply_change(token, msg)

    def _store(self, token_id: str, msg: dict) -> None:
        """A full snapshot. Parsed by the same function the HTTP path uses."""
        from ..parse import records
        try:
            book = records.parse_book(msg)
        except Exception:  # noqa: BLE001 - a malformed frame must not kill the socket
            return
        book["token_id"] = book.get("token_id") or token_id
        with self._lock:
            self._books[token_id] = (time.monotonic(), book)
        self.updates += 1
        self.changed.set()

    def _apply_change(self, token_id: str, msg: dict) -> None:
        """A delta. Applied onto a copy, because a reader may be holding the current one.

        A change for a token we have no snapshot of is dropped rather than treated as the whole
        book. Half a book priced as if it were all of it is exactly the sort of number that
        would fire a stop-loss for no reason.
        """
        with self._lock:
            entry = self._books.get(token_id)
        if entry is None:
            return
        _ts, current = entry
        bids = {p: sz for p, sz in current["bids"]}
        asks = {p: sz for p, sz in current["asks"]}
        for change in msg.get("changes") or []:
            try:
                price = float(change["price"])
                size = float(change["size"])
            except (KeyError, TypeError, ValueError):
                continue
            side = str(change.get("side", "")).upper()
            book_side = bids if side in ("BUY", "BID") else asks
            if size <= 0:
                book_side.pop(price, None)
            else:
                book_side[price] = size
        updated = {
            "token_id": current.get("token_id") or token_id,
            "condition_id": current.get("condition_id", ""),
            "bids": sorted(bids.items(), key=lambda lv: -lv[0]),
            "asks": sorted(asks.items(), key=lambda lv: lv[0]),
        }
        with self._lock:
            self._books[token_id] = (time.monotonic(), updated)
        self.updates += 1
        self.changed.set()


def connect(tokens: list[str], log=print) -> BookStream | None:
    """Start a book stream, or return None when one is not available.

    None is not an error and is not reported as one: polling is the supported path and the
    stream is an optimisation on top of it.
    """
    if not available():
        return None
    try:
        return BookStream(log=log).start(tokens)
    except Exception as e:  # noqa: BLE001
        log(f"  ~ book stream unavailable ({type(e).__name__}: {e}); polling instead")
        return None
