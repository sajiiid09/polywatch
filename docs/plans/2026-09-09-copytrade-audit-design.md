# Polywatch copy-trading bot — full audit + improvement plan

**Status:** accepted 2026-09-09. Implementation in progress.

## Context

`polywatch` started as read-only Polymarket analytics (commit `17c0d79`) and grew a copy-trading
engine over three commits (`d2a4b2c`, `75627e8`, `03b9788`). It now:

- ingests leaderboard wallets, their trades, market metadata and minute-resolution price windows
  into a 1 GB SQLite file;
- screens wallets behaviourally (`screen.py`) to drop market-making bots;
- scores one wallet at a time on settled positions (`copytrade/skill.py`);
- polls one trader's `/activity` feed and copies their buys, with an exit ladder, circuit
  breakers and paper/live execution (`copytrade/engine.py`).

The user asked for a complete audit with three goals: make the bot **faster**, make it **trade
better**, and above all make it **evaluate the traders it copies far more rigorously** — plus
anything else the audit surfaces, documented and cleared before implementation.

Baseline is healthy: 104 tests pass in 0.23s, the code is unusually well-commented, and the
data-layer decisions (WAL, WITHOUT ROWID `prices`, raw-response dumps, `signals` recording skips
as first-class rows) are sound. The problems are not sloppiness — they are unfinished intent and
a handful of accounting defects.

---

## Audit findings

Severity: **P0** loses money or corrupts state · **P1** materially degrades decisions ·
**P2** correctness/quality · **P3** cosmetic.

### A. Trader evaluation — the largest gap (user's stated priority)

**A1 (P1) — Stage 1 discovery is documented but does not exist.**
`copytrade/discover.py:1-20` describes a two-stage funnel: sweep the leaderboard across 11
categories × 4 time windows × 2 orderings to build a several-hundred-wallet pool, screen it
behaviourally, deep-score the survivors, print a ranked shortlist. None of that is implemented.
There is no sweep function, no ranking, and no `polywatch discover` command.
`config.py:69-73` defines `LEADERBOARD_CATEGORIES / _PERIODS / _ORDERINGS / _MAX_OFFSET` — all
unused. `ingest.ingest_wallets` only ever pulls `OVERALL/ALL/PNL`, i.e. exactly the "same handful
of whales" the docstring says is useless.

**A2 (P1) — `rank_score` can never be non-NULL, so `top_trader_scores` always returns empty.**
`discover.score_trader` (`discover.py:88-91`) writes `top_category: None`, `persona_fit: None`,
`rank_score: None`; `store.top_trader_scores` (`store.py:548`) filters
`WHERE rank_score IS NOT NULL`. Three schema columns and an index exist for a ranking layer that
was never written.

**A3 (P1) — the skill metrics do not measure *copyability*.**
`skill.py` computes win rate, ROI, Brier, drawdown, consistency, entry price, stake. Every one is
about the trader in isolation. Nothing answers "can *we* capture any of this at a 15-second poll
and a taker fee?" Missing, in rough order of decisiveness:

- **Hold-time distribution.** The gate on everything. A 20-minute flip is copyable 15s late; a
  40-second scalp is not. Derivable by matching BUY→SELL per token from `/activity` or `trades`.
- **Post-entry price drift at lag.** The real question: *after* they buy, does the price keep
  moving in their favour long enough that a copier who is 15–60s late still gets paid? The whole
  `prices` table, `price_at()` hot query and `WINDOW_POST_S = 1500` ("must exceed the largest lag
  in the Step 4 sweep") were built for this sweep — and the sweep was never written.
- **Fee-adjusted, copier-realizable ROI.** `skill.roi` is the trader's own return at their entry
  price, ignoring both taker fees and our slippage. `book.round_trip_fee_frac` exists and is
  never applied to a candidate's history. A wallet whose median entry is 0.50 in a 7% crypto
  market needs +14% before a copier breaks even.
- **Recent form vs lifetime.** Scores run over up to 1000 settled positions with no time
  weighting. A wallet great in March and bleeding now scores identically to one improving.
- **Luck discrimination.** `n_closed` is reported but never used as a gate; there is no
  confidence interval or "is this distinguishable from coin-flipping" test — the thing the
  project is literally named for ("Skill-vs-luck analytics", `pyproject.toml`).
- **Category concentration.** `top_category` column exists, never populated. Schema comment on
  `tasks.category` admits it "does NOT block copies".

**A4 (P1) — screen and score are never joined.** `screen.py` runs over the `wallets` table;
`skill.py` runs on one address via `polywatch trader <addr>`. No command runs
sweep → screen → score → rank in one pass, and screening verdicts never reach `trader_scores`.

**A5 (P2) — the behavioural screen systematically rejects the most copyable humans.**
`recon_trades` fetches **one** page (1000 trades) per wallet, and `screen.profile_wallet` derives
`span_days` / `active_days` from that page. An active human whose last 1000 trades span three
days is rejected for `active_days 3 < 5` — the span is an artifact of page size, not of the
wallet. Rejections are auditable (`screen_reason`), so this is visible but uncorrected.

**A6 (P2) — MIRROR sizing rests on a number known to be wrong.** `discover.account_value` returns
`/value`, which is open positions only and excludes idle cash, so `trader_usd / account` overstates
the fraction of their book they risked. Documented at `discover.py:54-66` and `task.py:143-157`,
mitigated only by the `mirror_max_frac` cap. A better denominator is already computed and
discarded: `SkillScore.median_stake_usd` ("this trade vs their typical trade").

### B. Trading correctness

**B1 (P0) — partial-exit accounting corrupts the remaining position.**
`engine.book_exit` (`engine.py:448-458`) scales `cost_usd` by the unfilled fraction but leaves
`fees_paid` whole *and adds the partial exit fee to it*. The remainder therefore carries the
entire original entry fee against a fraction of the shares. Consequences: `exits.mark` computes
`cost = cost_usd + fees_paid` → inflated cost → the stop-loss and trailing stop fire against a
loss that is partly fictional; and `store.run_cash` subtracts `cost_usd + fees_paid` → understated
cash → spurious `insufficient_cash` skips.

**B2 (P0) — a live fill can be booked as a rejection.**
`LiveExecutor._filled` (`execution.py:207-222`) reads only `sizeMatched`/`size_matched`; on any
other response shape it returns `(0, 0)` and `buy`/`sell` report
`"accepted but nothing matched"`. The order may in fact have filled. Nothing reconciles
afterwards, so real shares exist that the database does not know about — the one failure mode
this program cannot recover from, and it is unguarded.

**B3 (P0) — no reconciliation against the exchange at all.** In live mode `positions` is derived
purely from what the engine believes it did. `api.positions()` exists and is never called. A
restart, a partial fill, a REDEEM, or a resting take-profit filling while the process is down all
desync silently. `orphan_resting` covers only orders this program itself wrote.

**B4 (P0) — nothing prevents holding both sides of the same market.** If the trader buys NO while
we are long YES on the same `condition_id`, `_portfolio_gates` sees only aggregate exposure and
lets it through. Result: double cost, double fees, net exposure near zero. Pure loss.

**B5 (P1) — `poll_signals` never paginates.** `api.activity(..., limit=ACTIVITY_PAGE=100)`, page 0
only. A trader who does >100 events between polls, or any poll after a stall, silently drops
events while `last_seen_ts` advances past them. No error, no counter.

**B6 (P1) — the watermark advances before the event is handled.**
`poll_signals` (`engine.py:245-249`) sets `s.last_seen_ts = max(...)` and *then* calls
`handle_event`. A DB or parse error mid-list leaves the watermark past events that were never
processed; only `FetchError` is caught, one level up, around the whole poll.

**B7 (P1) — liquidity is never gated, only discovered after the fact.** `exits.entry_gates` gates
on staleness, size, price band, fee, market close and `accepting_orders` — it never looks at the
book. The book is first fetched inside `copy_buy`, *after* the signal has been recorded as
`copied`. So (a) the skip histogram, described as "the main finding of a paper run", undercounts
illiquidity entirely, and (b) there is no `max_spread` or `min_depth_usd` refusal.

**B8 (P1) — slippage is measured against an already-impacted price.**
`bk.limit_price(w.vwap, slippage, ...)` where `w.vwap` is the VWAP of *our own walk*, which has
already eaten through levels. On a thin book the effective worst case is impact + 7%, not 7%.
The honest reference is the touch (`best_ask`) at signal time.

**B9 (P1) — following the trader's exit is all-or-nothing.** `_handle_trader_sell` calls
`close(held, FOLLOW_EXIT)` on the full position regardless of what fraction they sold. A trader
scaling out 25% causes us to dump 100% — and the design note in `task.py:188-198` says following
their exit *is* the edge being copied, so mis-copying it is expensive.

**B10 (P1) — `end_bankroll` excludes open positions.** `Engine.stop` writes
`summary["cash"] = start − tied + realized`. With `--no-flatten`, positions left open have their
whole cost basis subtracted and nothing added back, so the run report reads as a catastrophic
loss that did not happen.

**B11 (P2) — paper resting fills ignore book depth.** `PaperExecutor.resting_fill`
(`execution.py:134-147`) fills the entire order whenever `best_bid >= price`, regardless of the
size at that bid. A 1-share bid books a 40-share fill. This is beyond the documented
queue-position optimism — it is depth optimism, on the exit that carries most of the PnL.

**B12 (P2) — `neg_risk` is stored and never used.** Correlated exposure across the outcomes of a
neg-risk event is invisible to the per-`condition_id` cap.

**B13 (P2) — stake can shrink below the market minimum without a skip reason.**
`_portfolio_gates` tests the uncapped stake; `copy_buy` then mins it against remaining market
headroom and cash, which can drop it under `min_order_size`. The result is an order row with
status `rejected` rather than a skip reason, so it never appears in the histogram.

### C. Speed

**C1 (P1) — the loop is fully serial and does two round trips per position per tick.**
`manage_positions` fetches each position's book, then `check_breakers` → `unrealized()` fetches
**every open position's book a second time**. At 3 positions that is 7 requests per tick where 4
would do. The `ThreadPoolExecutor` pattern to fix it already exists in `ingest._parallel`.

**C2 (P1) — `RateLimiter` is a hard serial spacer.** `MAX_RPS = 10` means a 100 ms floor between
*any* two requests process-wide, so the tick length grows linearly with position count and
directly delays every stop-loss check.

**C3 (P2) — copy latency conflates feed lag with our own.** `seen_ts` is stamped once per poll
before the event loop, so `seen_ts − trader_ts` mixes data-api's cache staleness with our poll
phase. `report.latency_block` then advises tuning the poll interval from a number that cannot
distinguish the two. Recording fetch time separately makes `POLL_INTERVAL_S` tunable from data
rather than from the comment at `config.py:99-104`.

**C4 (P2) — resting take-profit fills are detected a poll late.** The user CLOB websocket does
report our *own* fills (unlike third-party trades, correctly documented as poll-only at
`polymarket.py:93-104`), so live fill detection could be immediate.

### D. Hygiene

**D1 (P3)** — `src/polywatch/web/` is an empty package with a stale `__pycache__`.
**D2 (P3)** — no `README`, no `docs/`. All design rationale lives in module docstrings.
**D3 (P3)** — dead code: `store.signal_seen`, `api.trades_feed`, `api.traded_count`,
`api.tick_size`, `api.market_tags`, `api.positions` are all unreferenced.
**D4 (P3)** — `data/polywatch.db` is 1.06 GB with a 4.8 MB WAL; no retention or vacuum story.

---

## Decisions taken

| # | Decision | Consequence |
|---|---|---|
| D1 | Trader evaluation = live metrics **+ historical lag replay** | Build `replay.py`; measure copier ROI at 15s/30s/60s/5m/15m against the existing `prices` table instead of inferring copyability from hold time |
| D2 | **Live trading is the goal this cycle** | B2/B3/B4 are blocking, not deferrable. No live run until reconciliation exists |
| D3 | Speed = parallel loop **+ CLOB book websocket** | Exit ladder becomes continuous instead of 15s-granular. New optional dep, polling fallback |
| D4 | **Multi-trader portfolio** | One task copies N ranked traders on one bankroll, with per-trader caps, attribution and auto-drop |
| D5 | `STRATEGY.md` / `RULES.md` / `AGENTS.md` are the tracked source of truth | Every decision above and below is recorded there and kept current as work lands |

**Dependency note.** The repo is stdlib-only today, and `pyproject.toml` treats that as a feature
("does this program spend money" is answerable by checking whether the `live` extra is installed).
The websocket therefore goes in a **separate optional `stream` extra** (`websocket-client`), with
polling as the fallback when it is absent or the socket goes stale. The stdlib-only guarantee for
paper mode and every analytic path is preserved.

---

## Phase 0 — Documentation scaffolding (first, so decisions are tracked as they are made)

Four tracked documents, committed before code changes:

- **`STRATEGY.md`** — the trading thesis. What edge is being copied and why quick-flips; the
  measured finding that a take-profit destroys the edge (`task.py:188-198`: +8% target returned
  −$0.68/session, following the trader out returned +$6.59 over 30k windows); the fee arithmetic
  (`2·rate·min(p,1−p)/p` — 10% of stake at 0.50, under 1% at the extremes); what the bot can and
  cannot enforce; the portfolio policy from D4.
- **`RULES.md`** — hard operating rules and invariants. Live-mode gates; the never-do list; the
  invariants the code exists to protect (every observed action lands in `signals` with a reason,
  a finished run is explicable from the DB alone, the stop-loss is enforced only while the process
  runs); risk limits and who may change them.
- **`AGENTS.md`** — how to work in this repo. Module map; **all SQL lives in `db/store.py`**;
  **money is spent in exactly one class, `execution.LiveExecutor`**; how to add a config knob
  (dataclass field in `task.py`, which `from_row` tolerates in old `config_json`); test
  conventions; what must never regress.
- **`docs/decisions/NNNN-*.md`** — one short ADR per decision, starting with D1–D5, plus
  `docs/plans/2026-09-09-copytrade-audit-design.md` recording this audit.

Also: `README.md` (there is none), and delete the empty `src/polywatch/web/` package (D1 hygiene).

---

## Phase 1 — P0 correctness, before anything touches money

**1.1 Partial-exit fee accounting** — `engine.book_exit` (`engine.py:448-458`).
Scale `fees_paid` by the same fraction as `cost_usd`, then add the exit fee:
`fees_paid = fees_paid*(1-frac) + fill.fee`. Fixes the fictional-loss stop-loss and the
understated `run_cash`.

**1.2 Both-sides guard** — new gate in `engine._portfolio_gates`. Refuse a buy when an open
position exists on the same `condition_id` under a different `token_id`. Skip reason
`holds_other_outcome`.

**1.3 Live fill detection cannot report a fill as a rejection** — `execution.LiveExecutor._filled`.
Read every known response shape; when the response is unreadable, return a new
`Fill(status="unknown")` rather than `rejected`. The engine writes the order row and defers the
position to reconciliation instead of guessing.

**1.4 Exchange reconciliation** — new `copytrade/reconcile.py`. On `Engine.start()`, and after any
tick that produced an `unknown` order, read `api.positions()` for our funder address plus the
CLOB's open orders, and diff against `positions` / `resting_orders`. In live mode a mismatch
**stops the run** rather than trading on a wrong book. Paper mode no-ops. This is what finally
uses `api.positions()`.

**1.5 `end_bankroll` must include open positions** — `Engine.stop`. Report `cash` and `equity`
separately; mark open positions to the bid side when not flattening.

**1.6 Paper resting fills must respect depth** — `PaperExecutor.resting_fill`. Walk the bid side
for the order's shares at or above its price instead of filling the whole order off a
one-share bid.

---

## Phase 2 — Signal path and gates (better trades)

**2.1 Paginate `/activity`** — `engine.poll_signals`. Page until an event at or before the
watermark appears, or a page cap is hit; a truncated read is a counted run error, never a silent
drop.

**2.2 Advance the watermark only after the event is handled** — wrap each event, count failures,
and leave `last_seen_ts` behind an unprocessed event so the next poll re-reads it.

**2.3 Gate on liquidity before recording a copy** — fetch the book for signals that clear the
cheap gates, *then* decide. New Task knobs `max_spread_frac`, `min_depth_usd`; new skip reasons
`wide_spread`, `thin_book`. Makes the skip histogram — "the main finding of a paper run" — honest
about illiquidity.

**2.4 Measure slippage against the touch, not our own impact** — `bk.limit_price` takes
`best_ask`/`best_bid` as the reference with impact allowed separately. The walk still sizes the
order.

**2.5 Follow the trader's exit proportionally** — `_handle_trader_sell` sells the same *fraction*
of our position that they sold of theirs, falling back to a full close when their size is unknown.

**2.6 Skip when the final stake cannot meet the market minimum** — compute the capped stake in
`_portfolio_gates`, skip reason `below_min_size`, instead of emitting a rejected order that never
reaches the histogram.

**2.7 Neg-risk exposure grouping** — cap exposure across a neg-risk event, not only per
`condition_id`. `markets.neg_risk` is already stored.

**2.8 Default `tp_kind` to `None`** — the dataclass default (`task.py:52`) contradicts the
measured finding recorded three lines below it. Presets become the only thing that switch a
target on.

---

## Phase 3 — Trader evaluation (D1)

**3.1 `copytrade/sweep.py`** — leaderboard sweep over
`LEADERBOARD_CATEGORIES × LEADERBOARD_PERIODS × LEADERBOARD_ORDERINGS`, paged to
`LEADERBOARD_MAX_OFFSET`, deduped into `wallets` with the combination recorded as `source`.
Volume filters, never ranks — the bias to smaller, copyable wallets is the point. Finally uses the
four config constants that have been dead since Phase 1 of the original build.

**3.2 Fix the screening span artifact (A5)** — `ingest.recon_trades` pages until it covers
`min_span_days` (default 14) or hits a page cap, so `span_days` / `active_days` describe the
wallet instead of the page size.

**3.3 `copytrade/replay.py` — the lag sweep.** For each candidate:
- take their fills from `trades`, match BUY→SELL per token into round trips;
- backfill the price windows those round trips need, reusing the interval algebra that already
  exists (`ingest.merge_intervals` / `subtract` / `chunk`) and `store.record_window`;
- for each lag L ∈ {0, 15, 30, 60, 300, 900}: entry = `price_at(token, t_entry + L)`,
  exit = `price_at(token, t_exit + L)`, both legs charged `bk.fee` at the market's own rate;
- emit `copier_roi_at_lag`, `capture_ratio` (copier ROI at 15s ÷ their ROI), and
  `edge_half_life` (interpolated lag at which copier ROI crosses zero).

New table `trader_replay(address, lag_s, n_round_trips, copier_roi, copier_pnl, ...)`.
This is the module the `prices` table, `price_at()` and `WINDOW_POST_S = 1500` were designed for.

**3.4 `skill.py` additions** — hold-time distribution (p10/p50/p90) from the matched round trips;
fee-adjusted ROI via `bk.round_trip_fee_frac` at each position's entry price and category;
recency-weighted ROI (exponential decay, 30-day half-life); a luck test (bootstrap p-value that
realized PnL > 0 given `n_closed`) — the thing `pyproject.toml` names the project after; category
concentration feeding `top_category` plus a Herfindahl index.

**3.5 `copytrade/rank.py`** — combine the above into `rank_score` ∈ [0,1] with weights in
`config.py`, and score `persona_fit` against a Task persona (hold / style / risk / category).
Populates the three columns that have always been NULL, which makes `store.top_trader_scores`
return rows for the first time.

**3.6 `polywatch discover` CLI** — sweep → screen → score → replay → rank → ranked shortlist.
Flags: `--limit`, `--pages`, `--lags`, `--category`, `--no-replay` (falls back to inferred
copyability), `--json`.

---

## Phase 4 — Multi-trader portfolio (D4)

**4.1 Schema** — `task_traders(task, address, weight, added_ts, active, dropped_reason)`;
`trader` column added to `positions` and `orders` (`signals` already has one). Additive migration
following the existing `PRAGMA table_info` pattern in `store.init_db`.

**4.2 Task** — `traders: list[str]`, `per_trader_usd`, `min_rank_score`, `auto_drop_pnl`.
Backward compatible: an old `config_json` with a single `trader` loads as a one-element list,
which is exactly what `Task.from_row`'s tolerant loader was built for.

**4.3 Engine** — poll each trader's feed in parallel under the shared rate limiter; tag every
signal, order and position with its trader; enforce a per-trader exposure cap alongside the
per-market cap; auto-drop a trader whose run PnL breaches `auto_drop_pnl`.

**4.4 Report** — per-trader attribution block: signals, copies, PnL, exit mix, and whether the
trader was dropped.

---

## Phase 5 — Speed (D3)

**5.1 `copytrade/stream.py`** — CLOB market websocket subscription for held tokens, maintaining an
in-memory book cache. Optional dep `websocket-client` under a `stream` extra. Falls back to
polling when absent or when the socket has been silent longer than a staleness bound.

**5.2 Continuous exit evaluation** — the exit ladder runs on book updates rather than only on the
poll tick, so the stop-loss and trailing stop stop being 15-second-granular. The poll tick keeps
signals and circuit breakers.

**5.3 Kill the double book fetch** — `check_breakers` reuses the marks `manage_positions` already
computed instead of refetching every position's book.

**5.4 Parallelise what still polls** — via the `ThreadPoolExecutor` helper already in
`ingest._parallel`.

**5.5 Split the latency measurement (C3)** — `signals` gains `fetch_ts`; `report.latency_block`
shows feed lag and loop lag as separate numbers, so `POLL_INTERVAL_S` and `MAX_RPS` become
tunable from data rather than from a comment.

---

## Critical files

| File | Change |
|---|---|
| `src/polywatch/copytrade/engine.py` | Phases 1, 2, 4, 5 — the largest single surface |
| `src/polywatch/copytrade/execution.py` | 1.3, 1.6 — fill detection, paper depth |
| `src/polywatch/copytrade/reconcile.py` | new — 1.4 |
| `src/polywatch/copytrade/exits.py` | 2.3, 2.4 — liquidity gates, slippage reference |
| `src/polywatch/copytrade/task.py` | 2.8, 4.2 — defaults, multi-trader config |
| `src/polywatch/copytrade/sweep.py` | new — 3.1 |
| `src/polywatch/copytrade/replay.py` | new — 3.3 |
| `src/polywatch/copytrade/rank.py` | new — 3.5 |
| `src/polywatch/copytrade/skill.py` | 3.4 |
| `src/polywatch/copytrade/stream.py` | new — 5.1 |
| `src/polywatch/db/schema.sql`, `db/store.py` | 3.3, 4.1 — new tables, additive migrations |
| `src/polywatch/cli.py`, `copytrade/commands.py` | `discover` command, new task flags |
| `src/polywatch/ingest.py` | 3.2 — recon paging |

**Reuse, do not rebuild:** `ingest.merge_intervals` / `subtract` / `chunk` (replay backfill),
`ingest._parallel` (5.4), `store.price_at` (3.3), `bk.round_trip_fee_frac` / `bk.fee` (3.3, 3.4),
`screen.profile_wallet` / `verdict` (3.1), `Task.from_row`'s tolerant loader (4.2),
`store.init_db`'s `PRAGMA table_info` migration pattern (3.3, 4.1).

---

## Verification

**Unit** — every new pure function gets tests: replay lag arithmetic against a hand-built price
series, rank scoring, fee-adjusted ROI, hold-time percentiles, partial-exit fee accounting
(1.1, with an explicit assertion that `fees_paid/shares` is unchanged by a partial), the
both-sides guard, activity pagination, watermark-on-failure, resting-fill depth. Golden-file
tests reuse `tests/fixtures/`. The existing 104 tests must stay green throughout.

**Discovery, offline** — `polywatch discover --limit 20 --no-replay` against the live API, then
with replay against the 1 GB DB already on disk. Confirm `top_trader_scores` returns rows for the
first time and the shortlist is not the same handful of whales `OVERALL/ALL/PNL` returns.

**Paper run** — a multi-trader task over a session. Check: the skip histogram now contains
`wide_spread` / `thin_book`; the latency block reports feed lag and loop lag separately;
per-trader attribution appears; a forced partial exit leaves correct fees on the remainder.

**Live** — reconciliation on start against an empty book first. Then one minimum-size trade with
`max_concurrent=1` and a small `max_daily_loss_usd`, verifying the order row, the position, the
resting take-profit, and that `polywatch task orders` finds and cancels it. Live-mode changes are
irreversible once posted, so this ordering is not optional.

---

## Sequencing

Phases are ordered by dependency and by risk: **0 → 1 → 2 → 3 → 4 → 5**. Phase 1 must land and be
green before any live run, per D2. Phase 3 is the largest and is independent of Phases 4–5, so it
can be reviewed on its own. Each phase ends with tests green, the three tracked docs updated with
any decision made during it, and a commit.
