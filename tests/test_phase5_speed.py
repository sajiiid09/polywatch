"""Phase 5: how often the exits are actually checked, and what a tick costs.

Entry latency is bounded by data-api's activity cache and cannot be engineered away. Exit
latency has no such excuse -- the stop-loss, the trailing stop and the time stop are enforced by
this process and by nothing else, so the interval between checks is the resolution of every
protection a run has.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from polywatch.copytrade import stream
from polywatch.copytrade.engine import Engine
from polywatch.copytrade.execution import PaperExecutor
from polywatch.copytrade.task import Task, preset
from polywatch.db import store

from test_copytrade_engine import FakeAPI, activity_event, book_of, engine_for


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.init_db(c)
    return c


# --- one book per position per tick, not two ------------------------------


class CountingAPI(FakeAPI):
    def __init__(self, feed, books):
        super().__init__(feed, books)
        self.book_calls = 0

    def book(self, client, token_id, **kw):
        self.book_calls += 1
        if token_id not in self.books:
            # What the real client does when the CLOB has nothing for a token.
            from polywatch.fetch.client import FetchError
            raise FetchError(f"404 on /book?token_id={token_id}")
        return super().book(client, token_id, **kw)


def engine_counting(con, feed, books, task=None, now=1010):
    import polywatch.copytrade.engine as engine_mod
    fake = CountingAPI(feed, books)
    engine_mod.api = fake
    t = task or Task(name="t", trader="0xtrader", **preset("quick_flips"))
    store.upsert_task(con, t.as_row())
    eng = Engine(con, t, PaperExecutor(), client=None, log=lambda *a: None, now=lambda: now)
    eng._trader_account = lambda _a: 10_000.0
    eng.start()
    return eng, fake


def test_the_breakers_reuse_the_marks_the_sweep_just_computed(con):
    """Refetching every book to recompute numbers we already had doubled the cost of a tick --
    and delayed the breaker check by exactly that long."""
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, fake = engine_counting(con, [activity_event()], books)
    eng.poll_signals()
    assert store.open_positions(con, eng.state.run_id)

    fake.book_calls = 0
    eng.manage_positions()
    after_sweep = fake.book_calls
    eng.check_breakers()
    assert fake.book_calls == after_sweep, "the breakers must not fetch the books again"


def test_a_stale_mark_cache_is_not_reused_on_a_later_tick(con):
    """Reusing last tick's marks would run the breakers on a price that has moved."""
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, fake = engine_counting(con, [activity_event()], books)
    eng.poll_signals()
    eng.manage_positions()

    eng.state.polls += 1                      # a new tick, with no sweep yet
    fake.book_calls = 0
    eng.unrealized()
    assert fake.book_calls > 0


def test_books_for_several_positions_are_fetched_together(con):
    """A serial sweep makes the gap between stop-loss checks grow with the number of positions
    held, which is exactly backwards."""
    books = {f"tok{i}": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]} for i in range(3)}
    eng, _ = engine_counting(con, [], books)
    got = eng.books(["tok0", "tok1", "tok2"])
    assert set(got) == {"tok0", "tok1", "tok2"}


def test_a_repeated_token_is_only_fetched_once(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, fake = engine_counting(con, [], books)
    fake.book_calls = 0
    eng.books(["tok", "tok", "tok"])
    assert fake.book_calls == 1


def test_a_book_that_fails_to_fetch_is_absent_rather_than_fatal(con):
    books = {"good": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, _ = engine_counting(con, [], books)
    got = eng.books(["good", "missing"])
    assert "good" in got and "missing" not in got


# --- the stream -----------------------------------------------------------


def a_stream(max_age_s=20.0):
    return stream.BookStream(log=lambda *a: None, max_age_s=max_age_s)


def snapshot(token="tok", bids=(("0.49", "100"),), asks=(("0.51", "100"),)):
    return json.dumps([{
        "event_type": "book", "asset_id": token, "market": "cond",
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks]}])


def change(token="tok", side="BUY", price="0.50", size="42"):
    return json.dumps([{"event_type": "price_change", "asset_id": token,
                        "changes": [{"side": side, "price": price, "size": size}]}])


def test_a_snapshot_is_parsed_into_the_same_shape_a_fetched_book_has():
    """The engine reads a streamed book and a fetched one through the same code."""
    s = a_stream()
    s._ingest(snapshot())
    b = s.book("tok")
    assert b["bids"] == [(0.49, 100.0)] and b["asks"] == [(0.51, 100.0)]


def test_a_delta_updates_the_level_it_names_and_leaves_the_rest():
    s = a_stream()
    s._ingest(snapshot(bids=(("0.49", "100"), ("0.48", "50"))))
    s._ingest(change(price="0.49", size="7"))
    assert s.book("tok")["bids"] == [(0.49, 7.0), (0.48, 50.0)]


def test_a_delta_of_zero_size_removes_the_level():
    s = a_stream()
    s._ingest(snapshot(bids=(("0.49", "100"), ("0.48", "50"))))
    s._ingest(change(price="0.49", size="0"))
    assert s.book("tok")["bids"] == [(0.48, 50.0)]


def test_the_best_price_stays_first_after_a_delta_arrives():
    """The asymmetry upstream sends -- bids ascending, asks ascending -- is very easy to get
    backwards, and produces a plausible-looking wrong price when you do."""
    s = a_stream()
    s._ingest(snapshot(bids=(("0.40", "10"),), asks=(("0.60", "10"),)))
    s._ingest(change(side="BUY", price="0.55", size="5"))
    s._ingest(change(side="SELL", price="0.56", size="5"))
    b = s.book("tok")
    assert b["bids"][0][0] == 0.55 and b["asks"][0][0] == 0.56


def test_a_delta_for_a_book_we_have_no_snapshot_of_is_dropped():
    """Half a book priced as if it were all of it is exactly the number that fires a stop-loss
    for no reason."""
    s = a_stream()
    s._ingest(change(token="unknown"))
    assert s.book("unknown") is None


def test_a_book_older_than_the_staleness_bound_is_treated_as_absent():
    """A socket that is open but silent is the failure mode that would otherwise be invisible.
    A stale price here is far more dangerous than a slow one."""
    s = a_stream(max_age_s=0.0)
    s._ingest(snapshot())
    time.sleep(0.01)
    assert s.book("tok") is None


def test_a_malformed_frame_does_not_kill_the_socket():
    s = a_stream()
    s._ingest("not json at all")
    s._ingest(json.dumps([{"event_type": "book", "asset_id": "tok", "bids": "nonsense"}]))
    s._ingest(snapshot())
    assert s.book("tok") is not None


def test_an_update_wakes_anyone_waiting_on_the_book():
    """This is what turns a fifteen-second exit check into a continuous one."""
    s = a_stream()
    assert not s.changed.is_set()
    s._ingest(snapshot())
    assert s.changed.is_set()


def test_watching_a_new_token_widens_the_subscription():
    s = a_stream()
    s._tokens = {"a"}
    s.watch(["b", "c"])
    assert s._tokens == {"a", "b", "c"}


def test_a_deliberate_resubscribe_is_not_reported_as_a_dead_socket():
    """`watch` closes the socket the reader is blocked on, so the reader wakes with
    `OSError: Bad file descriptor`. Logging that as "falling back to polling" describes a
    working resubscribe as a failure, and backing off before reconnecting leaves the position
    that prompted it unstreamed for exactly as long as the backoff."""
    said = []
    s = stream.BookStream(log=said.append)
    s._thread = object()                      # `watch` only cycles a stream that has started
    s._tokens = {"a"}
    s.watch(["b"])
    assert s._cycling.is_set() and not said



# --- the engine prefers the stream and falls back without it --------------


class FakeStream:
    """A stream we control, standing in for a live socket."""

    def __init__(self, books=None, live=True):
        self._books = books or {}
        self.live = live
        self.changed = threading.Event()
        self.watched: list[str] = []

    def book(self, token_id):
        return self._books.get(token_id)

    def watch(self, tokens):
        self.watched += list(tokens)


def test_a_streamed_book_is_used_instead_of_a_request(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, fake = engine_counting(con, [], books)
    eng.stream = FakeStream({"tok": book_of([(0.60, 10)], [(0.61, 10)])})
    fake.book_calls = 0
    assert eng.book("tok")["bids"][0][0] == 0.60
    assert fake.book_calls == 0


def test_a_token_the_stream_has_not_sent_yet_falls_back_to_a_request(con):
    """Failing means falling back, never blocking."""
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, fake = engine_counting(con, [], books)
    eng.stream = FakeStream({})
    fake.book_calls = 0
    assert eng.book("tok") is not None
    assert fake.book_calls == 1


def test_opening_a_position_subscribes_its_token(con):
    """A position we hold whose book is never streamed is the one whose stop most needs it."""
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, _ = engine_counting(con, [activity_event()], books)
    eng.stream = FakeStream({})
    eng.poll_signals()
    assert "tok" in eng.stream.watched


def test_the_wait_re_checks_the_exits_whenever_the_book_moves(con):
    """Without a stream the ladder runs once per poll interval; with one it runs on movement."""
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, _ = engine_counting(con, [activity_event()], books)
    eng.poll_signals()

    feed = FakeStream({})
    eng.stream = feed
    checks = {"n": 0}
    eng.manage_positions = lambda: checks.__setitem__("n", checks["n"] + 1)

    def poke():
        for _ in range(3):
            time.sleep(0.02)
            feed.changed.set()

    threading.Thread(target=poke, daemon=True).start()
    eng._wait(0.2)
    assert checks["n"] >= 2, "a quiet sleep would have checked the exits zero times"


def test_without_a_stream_the_wait_is_just_a_sleep(con):
    eng, _ = engine_counting(con, [], {})
    started = time.monotonic()
    eng._wait(0.05)
    assert time.monotonic() - started >= 0.04


def test_a_dead_stream_is_the_same_as_no_stream(con):
    books = {"tok": {"bids": [(0.49, 1000)], "asks": [(0.51, 1000)]}}
    eng, fake = engine_counting(con, [], books)
    eng.stream = FakeStream({}, live=False)
    started = time.monotonic()
    eng._wait(0.05)
    assert time.monotonic() - started >= 0.04


def test_silence_on_the_socket_is_a_heartbeat_not_a_disconnect(monkeypatch):
    """Polymarket pushes on change, so a book nobody is trading sends nothing for minutes. A
    read timeout applied to that silence tore down a good connection, dropped every cached book
    and reconnected into the same silence -- which a live run hit within two minutes."""
    import websocket

    class QuietSocket:
        def __init__(self):
            self.sent = []
            self.recvs = 0

        def send(self, msg):
            self.sent.append(msg)

        def settimeout(self, _t):
            pass

        def recv(self):
            self.recvs += 1
            if self.recvs <= 3:
                raise websocket.WebSocketTimeoutException("Connection timed out")
            return snapshot()

        def close(self):
            pass

    sock = QuietSocket()
    made = {"n": 0}

    def create_connection(url, timeout=None):
        made["n"] += 1
        if made["n"] > 1:
            raise RuntimeError("the socket should not have been rebuilt")
        return sock

    monkeypatch.setattr(websocket, "create_connection", create_connection)

    s = a_stream()
    s._tokens = {"tok"}
    s.start(["tok"])
    for _ in range(200):
        if s.book("tok") is not None:
            break
        time.sleep(0.01)
    s.stop()

    assert s.connects == 1, "the connection was torn down and rebuilt"
    assert s.pings == 3, "each timeout should have sent a keepalive"
    assert s.book("tok") is not None, "the book that arrived after the silence was lost"
