# RULES

Hard operating rules and invariants. `STRATEGY.md` says what we are trying to do; this document
says what the bot may and may not do while doing it.

A rule here is not a preference. Changing one means changing this file in the same commit, with
the reason, and adding an ADR under `docs/decisions/` if it reverses a recorded decision.

---

## 1. Invariants the code exists to protect

These are load-bearing. Code that breaks one is wrong even if its tests pass.

**I1 — A finished run must be explicable from the database alone.**
Every observed action of every copied trader lands in `signals`, whether or not it was copied, and
a skip always carries the reason it was skipped. A crashed run is still a readable one, which
matters because the interesting runs are the ones that end badly.

**I2 — A skipped signal is a result, not an absence.**
The skip histogram is the main finding of a paper run. Any refusal to trade must produce a named
skip reason on a `signals` row — never a silent return, and never only a rejected `orders` row.

**I3 — Copy latency is measured, never assumed.**
`signals` carries `trader_ts`, `fetch_ts` and `seen_ts` on every row. Tuning decisions about the
poll cadence come from `store.latency_stats`, not from anyone's intuition about how fast a cache
refreshes.

**I4 — Dedupe lives in the database, not in memory.**
Consecutive polls always overlap on purpose. An in-memory seen-set is lost on restart, which is
precisely when re-copying a stale trade costs the most. The `UNIQUE` constraint on `signals` is
the dedupe.

**I5 — Money is spent in exactly one class.**
`copytrade/execution.LiveExecutor`. "Does this program spend money" must stay a question with a
one-file answer. `fetch/client.py` is GET-only, unauthenticated and unsigned, and stays that way.

**I6 — All SQL lives in `db/store.py`.**
Callers pass and receive plain dicts and rows, never cursors. A Postgres port must be a rewrite of
one file, not a hunt through the codebase.

**I7 — Paper mode signs nothing and spends nothing.**
Paper fills against the real live book, bounded by the same limit price a live order would carry.
Its documented optimism is queue position and depth timing — never price. A paper fill at a price
the live book was not showing is a bug.

**I8 — A learned finding is a proposal, never an application.**
`copytrade/learn.py` reads the run record, groups it by trader archetype, and writes
`docs/STRATEGY_LEARNED.md` and a `strategy_snapshots` row. It writes nothing else: not a `Task`,
not a knob, not a roster, not a default in `config.py`. §6 below says a change contradicting a
measured finding needs a new measurement rather than an argument, and that applies to the bot's
own findings first. A proposal also carries its sample size or it is not one — anything under the
floor is demoted to an observation rather than inflated into a recommendation.

**I9 — A session hands over information, never state.**
`Engine.start` closes an abandoned run rather than adopting it, seeds the watermark to *now*, and
discards the trailing high-water marks. `polywatch session open` prints the last session's
briefing and restores none of that. A run started after a handoff inherits no positions, no
watermark and no high-water mark, and the briefing says so where an operator will read it.

## 2. Live-mode gates

Every one of these must hold before a signed order is posted.

**L1** — Live requires an explicit `--mode live`. It is never a default and never inherited from
a flag that means something else.

**L2** — Live requires a typed confirmation (`LIVE`) unless `--yes` is passed deliberately. The
confirmation states what is and is not protected while the run is up.

**L3** — Live requires the optional `live` extra to be installed. Absent it, the program refuses
rather than degrading to paper silently.

**L4** — Credentials come from the environment only. `POLYMARKET_PRIVATE_KEY`,
`POLYMARKET_FUNDER`, and the optional API creds are **never** written to the database, to
`data/raw/`, to a log line, to a report, or to a commit.

**L5** — A run must reconcile against the exchange before it trades. If the exchange's view of
our positions and open orders disagrees with the database, the run stops. It does not trade on a
book it cannot verify.

**L6** — An order whose fill status cannot be determined from the response is `unknown`, never
`rejected`. It is resolved by reconciliation before any position is booked against it. Booking a
guess is how the database and reality diverge permanently.

**L7** — Resting GTC orders outlive the process. A run that ends leaves them cancelled, or leaves
them deliberately and says so. The next run is told about orphans rather than discovering the
shares are gone.

## 3. Risk limits

Enforced in code, not by convention.

| Limit | Knob | Default |
|---|---|---|
| Per-trade stake | `fixed_usd` / `mirror_max_frac` | $10 / 10% |
| Per-market exposure | `max_market_usd` | $25 |
| Per-trader exposure | `per_trader_usd` | share of bankroll |
| Concurrent positions | `max_concurrent` | 3 |
| Session loss | `max_daily_loss_usd` | $20 |
| Session drawdown | `max_drawdown_pct` | 25% |
| Session length | `session_hours` | 5h, then flatten |
| Slippage | `slippage` | 7% against the touch |
| Entry fee ceiling | `max_fee_frac` | 12% of stake |
| Entry price band | `min_price` / `max_price` | 0.05 – 0.95 |

Circuit breakers are measured on **realized plus unrealized** PnL. Measuring on realized alone
produces a bot that sits calmly through a total loss because it has not sold yet.

## 4. Never

- **Never hold both outcomes of the same market.** Double cost, double fees, net exposure near
  zero. It is a pure loss and it is gated.
- **Never advance the poll watermark past an event that was not handled.** Re-reading is free;
  losing a fill is not.
- **Never drop feed events silently.** A truncated read is a counted run error.
- **Never copy a trade without having looked at the book.** Liquidity is an entry gate, not a
  discovery made after the signal is already recorded as copied.
- **Never book a position from an unreadable exchange response.** See L6.
- **Never leave a live position open without saying so.** Nothing watches the stop-loss once the
  process returns.
- **Never rank candidate wallets by volume or by absolute PnL.** Both sort by bankroll as much as
  by ability. Volume filters; it does not rank.
- **Never score a wallet from a PnL-sorted prefix of its history.** The server's default sort is
  `REALIZEDPNL DESC`; reading the first pages hands you nothing but its best trades. Probed on a
  real wallet, the first 200 rows were 100% winners. Always sort by `TIMESTAMP`.
- **Never treat `/value` as a trader's account size.** It is open positions only and excludes
  idle cash, so it is a floor. Dividing a trade by it overstates their conviction.

## 5. Data handling

- Every HTTP response is written to `data/raw/` verbatim and logged to `ingest_log` **before** any
  caller looks at it, so upstream schema drift leaves evidence on disk.
- The trading poller is the one exception: it disables raw dumps and success logging, because
  polling forever would add a quarter-million rows a day to say nothing happened. **Failures are
  still logged** — the flag is `log_ok`, not `log`.
- Re-ingestion is idempotent. Trades dedupe on a natural key, markets upsert, and price windows
  already recorded are never refetched.
- `docs/STRATEGY_LEARNED.md` and `docs/PROGRESS.md` are **outputs**, regenerated and appended
  by the program. Editing them by hand loses the edit at the next run and desynchronises the
  copy of the same text stored in `sessions.briefing_md`. Add a human note with
  `session close --note`, and put reasoning that should survive in `STRATEGY.md` or an ADR.
- Rate limiting is self-imposed and global across threads. Polymarket publishes no limit for these
  public endpoints; we impose one rather than discover theirs the hard way.

## 6. Changing the rules

- A new config knob goes on the `Task` dataclass. `Task.from_row` tolerates unknown keys and
  missing ones, so adding a knob never invalidates a saved task and never needs a migration.
- A schema change is additive, applied through the `PRAGMA table_info` pattern in
  `store.init_db`. Existing databases must keep opening.
- A change to a default that contradicts a measured finding is not allowed without superseding the
  measurement. The finding in `STRATEGY.md` §4 is the standing example.
