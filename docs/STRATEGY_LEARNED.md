# STRATEGY (learned)

**This file is generated. Do not edit it — `polywatch strategy learn` overwrites it whole.**

Generated 2026-09-09 10:38 UTC over all tasks: 78 run(s) between 2026-05-09 23:12 and 2026-09-06 07:53.

`STRATEGY.md` is the hand-written thesis and nothing here edits it. This document is the evidence accumulating underneath it: what kinds of trader have been copied, what that cost, and what the numbers suggest changing. **Nothing below has been applied.** A proposal is a suggestion with its sample size attached, and `RULES.md` I8 is what keeps it one.

---

## Observed archetypes

| archetype | wallets | median rank | median capture | what it means for a copier |
|---|---|---|---|---|
| `resolution-holder` | 1147 | 0.50 | - | exits by settlement, so there is no exit to follow out |
| `unclassified` | 455 | 0.51 | - | no shape stood out far enough from the next one to name |
| `longshot-hunter` | 169 | 0.55 | - | cheap entries, small fees, a few large winners paying for many losers |
| `market-maker` | 99 | 0.32 | - | edge is latency, not judgement; gone by the time the fill is visible |
| `favourite-grinder` | 60 | 0.50 | - | expensive entries, high hit rate, thin margins the fee eats first |
| `momentum-chaser` | 39 | 0.50 | - | buys into a move already underway; a late copy buys the move, not it |
| `scalper` | 35 | 0.47 | - | flips inside a poll interval -- over before a copier could join |
| `fade-the-move` | 15 | 0.47 | - | buys weakness; a late copy gets a better price, not a worse one |
| `event-specialist` | 7 | 0.54 | - | concentrated in one category; worth copying there and nowhere else |

## Copy performance by archetype

Positions are attributed to the archetype of the trader whose signal opened them. Fees are counted separately from PnL because the fee is the part no exit rule can win back.

| archetype | positions | closed | open | realized pnl | fees | win rate | top exit |
|---|---|---|---|---|---|---|---|
| `resolution-holder` | 1434 | 1434 | 0 | $840.57 | $143.48 | 34% | settled_loss |
| `longshot-hunter` | 600 | 600 | 0 | $388.13 | $67.90 | 24% | mirror_sell |
| `unclassified` | 6 | 6 | 0 | $-12.00 | $0.57 | 0% | settled_loss |
| `favourite-grinder` | 3 | 3 | 0 | $-12.00 | $0.57 | 0% | settled_loss |

## Where copies are refused

The skip histogram is the main finding of a paper run. Split by archetype it stops being a verdict on the market and becomes one about which kind of wallet to stop shortlisting.

| reason | total | concentrated in |
|---|---|---|
| `below_min_order_size` | 13554 (49%) | `resolution-holder` (13123) |
| `no_position_to_exit` | 3531 (13%) | `longshot-hunter` (2232) |
| `price_above_momentum_band` | 3354 (12%) | `resolution-holder` (2502) |
| `market_cap_reached` | 2208 (8%) | `resolution-holder` (2093) |
| `settlement_undatable` | 2175 (8%) | `resolution-holder` (2169) |
| `market_never_resolved_in_data` | 747 (3%) | `longshot-hunter` (522) |
| `price_below_momentum_band` | 732 (3%) | `longshot-hunter` (597) |
| `max_concurrent_positions` | 717 (3%) | `resolution-holder` (585) |
| `market_unknown` | 387 (1%) | `resolution-holder` (195) |

## Latency

30681 signals, median 0s behind the trader, p90 0s.
Split: 0.0s is the feed answering with stale data, 0.0s is this loop. Only the second half is ours to fix.

## Trend

Each `strategy learn` appends a row per archetype, so drift is visible rather than overwritten.

| taken | archetype | positions | realized pnl | win rate |
|---|---|---|---|---|
| 2026-09-09 10:36 | `event-specialist` | 0 | $0.00 | - |
| 2026-09-09 10:36 | `fade-the-move` | 0 | $0.00 | - |
| 2026-09-09 10:36 | `favourite-grinder` | 3 | $-12.00 | 0% |
| 2026-09-09 10:36 | `longshot-hunter` | 600 | $388.13 | 24% |
| 2026-09-09 10:36 | `market-maker` | 0 | $0.00 | - |
| 2026-09-09 10:36 | `momentum-chaser` | 0 | $0.00 | - |
| 2026-09-09 10:36 | `resolution-holder` | 1434 | $840.57 | 34% |
| 2026-09-09 10:36 | `scalper` | 0 | $0.00 | - |
| 2026-09-09 10:36 | `unclassified` | 6 | $-12.00 | 0% |
| 2026-09-09 10:36 | `unknown` | 0 | $0.00 | - |
| 2026-09-09 10:38 | `event-specialist` | 0 | $0.00 | - |
| 2026-09-09 10:38 | `fade-the-move` | 0 | $0.00 | - |
| 2026-09-09 10:38 | `favourite-grinder` | 3 | $-12.00 | 0% |
| 2026-09-09 10:38 | `longshot-hunter` | 600 | $388.13 | 24% |
| 2026-09-09 10:38 | `market-maker` | 0 | $0.00 | - |
| 2026-09-09 10:38 | `momentum-chaser` | 0 | $0.00 | - |
| 2026-09-09 10:38 | `resolution-holder` | 1434 | $840.57 | 34% |
| 2026-09-09 10:38 | `scalper` | 0 | $0.00 | - |
| 2026-09-09 10:38 | `unclassified` | 6 | $-12.00 | 0% |
| 2026-09-09 10:38 | `unknown` | 0 | $0.00 | - |
| 2026-09-09 10:38 | `event-specialist` | 0 | $0.00 | - |
| 2026-09-09 10:38 | `fade-the-move` | 0 | $0.00 | - |
| 2026-09-09 10:38 | `favourite-grinder` | 3 | $-12.00 | 0% |
| 2026-09-09 10:38 | `longshot-hunter` | 600 | $388.13 | 24% |
| 2026-09-09 10:38 | `market-maker` | 0 | $0.00 | - |
| 2026-09-09 10:38 | `momentum-chaser` | 0 | $0.00 | - |
| 2026-09-09 10:38 | `resolution-holder` | 1434 | $840.57 | 34% |
| 2026-09-09 10:38 | `scalper` | 0 | $0.00 | - |
| 2026-09-09 10:38 | `unclassified` | 6 | $-12.00 | 0% |
| 2026-09-09 10:38 | `unknown` | 0 | $0.00 | - |

## Proposed changes (UNAPPLIED)

_Nothing has enough evidence behind it to propose. That is the expected state early on and is not a failure._

## Observations (below the sample floor)

- `event-specialist`: 7 wallet(s) profiled, none copied yet.
- `fade-the-move`: 15 wallet(s) profiled, none copied yet.
- `favourite-grinder`: 3 closed position(s) returning $-12.00. Under the 15-position floor, so this is recorded and not acted on.
- `market-maker`: 99 wallet(s) profiled, none copied yet.
- `momentum-chaser`: 39 wallet(s) profiled, none copied yet.
- `scalper`: 35 wallet(s) profiled, none copied yet.
- `unclassified`: 6 closed position(s) returning $-12.00. Under the 15-position floor, so this is recorded and not acted on.

## What would falsify this

Carried from `STRATEGY.md` §10, restated per archetype so a run can settle them:

- An archetype whose skips are mostly `stale` is too fast to copy at this cadence, whatever its rank score says.
- An archetype whose skips are mostly `fee_floor` trades where the round trip costs more than the exit rule can win back — a verdict on the market it chose.
- An archetype whose exits are mostly `max_hold` has an edge slower than the task assumes.
- An archetype whose median capture ratio sits under 0.3 at one poll interval has nothing in it for a copier, however good the individual wallets are.
