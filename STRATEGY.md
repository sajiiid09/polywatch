# STRATEGY

What edge this bot copies, why it is copyable, and what the arithmetic demands of it.

This document is the trading thesis. `RULES.md` is what the bot may and may not do;
`AGENTS.md` is how to work on the code. Decisions are recorded in `docs/decisions/`.

---

## 1. The edge

We do not forecast. We copy wallets that have demonstrably forecast well, and we try to keep
enough of their return after fees and latency to be worth the exposure.

That framing has one consequence that drives everything below: **the trader's return is not our
return.** Ours is theirs, minus the taker fee on both legs, minus whatever the price moved in the
seconds between their fill and ours. A wallet can be genuinely excellent and still be entirely
uncopyable. Measuring that gap — rather than assuming it away — is what `copytrade/replay.py`
exists for.

## 2. Why quick flips

A position held for twenty minutes is still open when we see the fill fifteen seconds late. A
multi-day thesis position is not copied so much as *joined at a worse price after the move*.

So the default preset (`task.QUICK_FLIP`) is tuned for traders whose edge expresses itself in
minutes: a 45-minute time stop, a 120-second staleness gate, a 15-second poll. The `hours` preset
loosens all of that, but it exists to babysit an idea you formed yourself, not because the poller
is any good at finding one.

## 3. The fee is the strategy

Polymarket's taker fee is proportional to `min(p, 1-p)`, not to notional. It is largest at even
odds and vanishes toward either end. A fixed stake buys `1/p` shares. Multiply those together and
a **round trip costs `2 · rate · min(p, 1-p) / p` of the stake**:

| entry price | round-trip fee (5% category) |
|---|---|
| 0.50 | 10.0% of stake |
| 0.70 | 4.3% |
| 0.85 | 1.8% |
| 0.95 | 0.5% |

Read that table as a constraint, not a curiosity. A 5% take-profit at 0.50 is *arithmetically
incapable* of clearing the round trip. This is a property of the market, not of the trader.

Three knobs follow directly from it, all implemented in `copytrade/book.py` and `copytrade/task.py`:

- `max_fee_frac` — refuse the entry outright when the fee alone would eat this much of the stake.
  No exit rule recovers that.
- `tp_fee_policy` — when a percentage target sits under the fee floor: `widen` it to the floor
  plus `min_edge`, `skip` the trade, or leave it `off`.
- `MIN_ENTRY_PRICE` / `MAX_ENTRY_PRICE` — the band where fees are payable and the book is not a
  desert.

Every exit threshold is evaluated against the **net** exit price — what the bid side would
actually pay after the fee — never against the mid and never against the last trade. A stop
measured on the mid does not fire until the loss is already worse than it says.

## 4. A take-profit destroys the edge we came for

This is the least intuitive finding in the project and it is measured, not assumed.

Measured on 374 matched round trips from a live quick-flip wallet on 2026-09-08: their flips
returned a **median of +15%**, but a **p75 of +68%** and a **p90 of +194%**. A fixed target
truncates exactly the tail that pays for the losers.

Simulated over 30,000 fifteen-minute windows at $10 a copy:

| exit rule | return per session |
|---|---|
| +8% take-profit | **−$0.68** |
| target widened to clear fees | **−$0.57** |
| follow the trader out | **+$6.59** |

The trader's own exit *is* the edge being copied. A take-profit is a different, worse strategy
wearing the same clothes. So `follow_exit` defaults on and `tp_kind` defaults to `None`.

The one case for a target: a resting GTC sell is the only exit that survives this process being
closed. When the alternative is leaving a position unwatched, capping the upside is the right
trade. That is a deliberate choice, made with `--take-profit`, not a default.

## 5. What the bot can and cannot enforce

Stated plainly because it decides how the bot may be used:

- **Take-profit — enforced by the exchange.** Posted as a resting GTC sell, it fills when the
  price comes to it whether or not this process is alive.
- **Stop-loss — enforced by this process only.** The CLOB has **no stop order type**. A stop is a
  price this loop watches and a market sell it sends. *While the bot is not running, the stop is
  not running.*
- **Trailing stop, time stop, circuit breakers — this process only**, for the same reason.

That asymmetry is why a run has a session clock and flattens at the end of it rather than leaving
positions unattended, and why `--no-flatten` is a decision rather than a default.

## 6. Choosing who to copy

A four-stage funnel, cheap to expensive. Nothing in it picks a trader; it produces a ranked
shortlist and stops.

1. **Sweep** (`sweep.py`) — the leaderboard across 11 categories × 4 windows × 2 orderings, paged
   out. `OVERALL/ALL/PNL` alone returns the same handful of whales every time. The deliberate bias
   is toward *smaller* wallets: ranking by PnL sorts by bankroll as much as by ability, and a
   wallet turning $2k into $3k is both more impressive and more copyable at a $100 bankroll than
   one turning $2M into $2.1M. **Volume filters; it never ranks.**
2. **Screen** (`screen.py`) — behavioural, one recon pass. A market maker's edge is latency, and
   by the time a copier sees the fill it is gone. Sub-minute trade spacing, bursts, and
   thousands-per-day cadence are all machine signatures.
3. **Score** (`skill.py`) — evidential, on settled positions. Win rate, ROI, Brier, drawdown,
   consistency, hold-time distribution, fee-adjusted ROI, recency-weighted form, a luck test, and
   category concentration.
4. **Replay** (`replay.py`) — the only stage that answers the actual question. Replay their fills
   against minute-resolution price history at copy lags of 0/15/30/60/300/900 seconds, charging
   the fee on both legs, and report what a copier would have captured. Outputs `copier_roi` per
   lag, `capture_ratio`, and `edge_half_life` — the lag at which their edge reaches zero.

A wallet with a great ROI and a 30-second edge half-life is not a candidate. That sentence is the
whole reason stage 4 exists.

## 7. Metrics that matter, and why

- **Brier score** — the one metric that measures judgement rather than outcome. A trader can post
  a fine ROI on a handful of lucky longshots; they cannot post a good Brier across many markets
  without being calibrated. 0.25 is what you get guessing 0.5 every time, so at or above that the
  entry prices carry no information whatever the PnL says.
- **Win rate on realized PnL, not on outcomes.** Those differ, and the difference is the point: a
  trader who buys at 0.95 and is right nine times in ten still loses money.
- **Consistency** (share of months in profit) — a trader up nine months in ten is a different
  proposition from one whose entire record is a single enormous month, at identical total PnL.
  Since the target is small wins over time, this is weighted heavily.
- **Max drawdown against capital staked**, not against a running peak. This curve starts at zero,
  so a $100 fall from a $50 peak would read as 200% — arithmetically correct and useless. It is an
  optimistic measure (it cannot see paper losses they sat through), so it disqualifies rather than
  endorses.
- **Luck test** — a bootstrap p-value on whether the record is distinguishable from chance given
  its sample size. The project is named for this question.

## 8. Portfolio policy

One task copies **N ranked traders** on one bankroll rather than betting the account on one wallet.
The failure mode this exists to prevent is the ordinary one: a single trader tilts, and takes the
whole account with them.

- Per-trader exposure cap alongside the per-market cap.
- Per-trader PnL attribution in the run report, so a wallet can be judged on its own record.
- Auto-drop a trader whose run PnL breaches a floor — stop copying, keep the position management.
- Weights come from `rank_score`; a trader below `min_rank_score` is not copied at all.

## 9. Latency budget

Entry and exit latency are different problems with different answers, and conflating them is how
a bot ends up polling faster to fix something polling cannot fix.

**Entry latency is bounded by the feed.** A third party's fills can only be polled — the CLOB
market websocket carries no wallet address, and the user websocket reports only your own account.
So the floor is however stale data-api's `/activity` cache is when it answers, and no poll
interval gets under it. `signals` records `trader_ts`, `fetch_ts` and `seen_ts` on every row so
the report can say which half of the delay is the feed's and which is ours. Tune
`POLL_INTERVAL_S` from that table, never from a comment.

**Exit latency is entirely ours.** The stop-loss, the trailing stop and the time stop are enforced
by this process and by nothing else, so the interval between checks *is* the resolution of every
protection a run has. Checked once per poll, a fifteen-second gap in a fast market is a
fifteen-second option written against us for free.

With the optional `stream` extra the CLOB book channel pushes updates and the exit ladder runs on
every one of them. Measured against the live feed on 2026-09-09: twenty tokens produced 88 book
deltas in 35 seconds and 30 distinct top-of-book moves, where a 15-second poll would have looked
twice. Absent the extra, or when the socket drops, everything falls back to polling — a stale
price is far more dangerous here than a slow one, so a cached book past `MAX_BOOK_AGE_S` is
treated as absent rather than trusted.

Books are also fetched together rather than one after another, and the circuit breakers reuse the
marks the position sweep just computed instead of refetching every book to recompute them. A
serial sweep made the gap between stop-loss checks grow with the number of positions held, which
is exactly backwards: the more exposure a run has, the faster it should be looking at it.

## 10. What would falsify this

Stated in advance so a paper run can settle it:

- If `stale` dominates the skip histogram, the trader is too fast to copy at this cadence.
- If `fee_floor` dominates, the round trip costs more than the exit rule can win back — a verdict
  on the market, not the trader.
- If `wide_spread` / `thin_book` dominate, their entries sit where the book is not.
- If `max_hold` dominates the exit mix, their edge is slower than the task assumes.
- If replay shows `capture_ratio` below ~0.3 at 15 seconds, there is nothing here for a copier
  regardless of how good the wallet is.
