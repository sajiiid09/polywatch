"""Stage 1 of discovery: assemble a candidate pool worth screening.

`OVERALL / ALL / PNL` -- the only slice ingestion ever read -- returns the same handful of whales
every time. Those wallets are the least copyable population on the site: their edge is size and
latency, their trades move the book they trade in, and a $100 bankroll mirroring them is buying
the exhaust.

The leaderboard is a much larger surface than one slice of it. Eleven categories, four time
windows, two orderings, each pageable, is a couple of hundred distinct queries returning several
hundred distinct wallets. Sweeping it is the difference between choosing from the top fifty
traders on the site and choosing from the top fifty *in each of eleven categories over four
horizons*, which is where a smaller, still-active, still-copyable wallet actually appears.

Two biases are deliberate:

  * **Toward smaller wallets.** Ranking by PnL sorts by bankroll as much as by ability. A wallet
    turning $2k into $3k is both more impressive and more copyable at a $100 bankroll than one
    turning $2M into $2.1M. Volume is recorded and used as a filter downstream; it is never used
    to rank.
  * **Toward breadth over depth.** One page per query beats ten pages of the OVERALL slice. A
    wallet that tops the WEATHER/WEEK board and appears nowhere else is exactly the kind of
    candidate this exists to surface.

Nothing here judges a wallet. It returns a deduplicated pool and records where each one was
found, which is itself a signal: a wallet appearing on eight boards is a different proposition
from one appearing on a single day's crypto list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import (LEADERBOARD_CATEGORIES, LEADERBOARD_ORDERINGS, LEADERBOARD_PAGE,
                      LEADERBOARD_PERIODS)
from ..db import store
from ..fetch import polymarket as api
from ..fetch.client import FetchError
from ..parse import records


@dataclass
class Candidate:
    """One wallet, plus every board it turned up on."""
    address: str
    username: str | None = None
    vol: float | None = None
    pnl: float | None = None
    best_rank: int | None = None
    boards: list[str] = field(default_factory=list)

    @property
    def appearances(self) -> int:
        return len(self.boards)

    def merge(self, row: dict, board: str) -> None:
        self.boards.append(board)
        self.username = self.username or row.get("username")
        # Keep the largest reported figures. Different boards report different windows, and the
        # widest one is the closest thing to a lifetime number the leaderboard offers.
        for attr, key in (("vol", "vol"), ("pnl", "pnl")):
            v = row.get(key)
            if v is not None and (getattr(self, attr) is None or v > getattr(self, attr)):
                setattr(self, attr, v)
        rank = row.get("rank")
        if rank is not None and (self.best_rank is None or rank < self.best_rank):
            self.best_rank = rank


def boards(categories=LEADERBOARD_CATEGORIES, periods=LEADERBOARD_PERIODS,
           orderings=LEADERBOARD_ORDERINGS) -> list[tuple[str, str, str]]:
    """Every (category, period, ordering) combination to ask for."""
    return [(c, p, o) for c in categories for p in periods for o in orderings]


def sweep(client, *, categories=LEADERBOARD_CATEGORIES, periods=LEADERBOARD_PERIODS,
          orderings=LEADERBOARD_ORDERINGS, pages: int = 1, log=print
          ) -> dict[str, Candidate]:
    """Read the leaderboard across every slice. Returns candidates keyed by address.

    A failed board is logged and skipped rather than fatal: two hundred queries against a public
    endpoint will occasionally lose one, and losing the WEATHER/DAY board is not a reason to
    discard the other hundred and ninety-nine.
    """
    pool: dict[str, Candidate] = {}
    todo = boards(categories, periods, orderings)
    for i, (category, period, ordering) in enumerate(todo, 1):
        board = f"{category}/{period}/{ordering}"
        for page in range(pages):
            try:
                payload = api.leaderboard(client, offset=page * LEADERBOARD_PAGE,
                                          category=category, time_period=period,
                                          order_by=ordering)
            except FetchError as e:
                log(f"  ! {board} p{page}: {e}")
                break
            rows = records.parse_leaderboard(payload, source=f"sweep:{board}")
            for row in rows:
                addr = row["address"]
                pool.setdefault(addr, Candidate(addr)).merge(row, board)
            if len(rows) < LEADERBOARD_PAGE:
                break
        if i % 20 == 0 or i == len(todo):
            log(f"  swept {i}/{len(todo)} boards, {len(pool)} distinct wallets")
    return pool


def persist(con, pool: dict[str, Candidate]) -> int:
    """Write the pool into `wallets` so the existing screen can run over it.

    `rank` carries the best rank the wallet reached on any board, which is the only ordering the
    leaderboard offers that is not a proxy for account size. `source` records how many boards it
    appeared on, because breadth of appearance is itself evidence.
    """
    rows = [{
        "address": c.address,
        "source": f"sweep:{c.appearances}",
        "rank": c.best_rank,
        "username": c.username,
        "vol": c.vol,
        "pnl": c.pnl,
    } for c in pool.values()]
    return store.upsert_wallets(con, rows)
