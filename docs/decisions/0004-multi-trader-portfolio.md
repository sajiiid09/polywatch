# 0004 — One task copies several traders

**Date:** 2026-09-09 · **Status:** accepted

## Context

Discovery produces a ranked shortlist. The engine consumes exactly one wallet. That mismatch means
the entire account rides on a single trader's judgement, and the ordinary failure mode of a copy
bot is that one trader tilts and takes the account with them.

## Decision

A task copies N traders on one bankroll, with per-trader exposure caps alongside the per-market
cap, per-trader PnL attribution in the report, and auto-drop when a trader's run PnL breaches a
floor. Weights come from `rank_score`; a wallet below `min_rank_score` is not copied.

Schema: a `task_traders` table, and a `trader` column on `positions` and `orders` — `signals`
already has one. Both additive, through the existing `PRAGMA table_info` migration path.

`Task` gains `traders: list[str]`. An old `config_json` carrying a single `trader` loads as a
one-element list, which is exactly what the tolerant loader in `Task.from_row` was built for.

## Consequences

- Circuit breakers see total risk across traders, which several parallel single-trader processes
  could not.
- The poller watches several activity feeds per tick, under the shared rate limiter.
- Attribution makes a bad wallet identifiable rather than merely suspected.

## Alternatives rejected

**Run several single-trader tasks side by side.** Much smaller change, but bankroll, caps and
breakers stay per-process, so nothing can see or bound total exposure.
