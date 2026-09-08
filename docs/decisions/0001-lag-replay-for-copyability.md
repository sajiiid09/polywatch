# 0001 — Measure copyability by lag replay, not by inference

**Date:** 2026-09-09 · **Status:** accepted

## Context

`skill.py` scored wallets on win rate, ROI, Brier, drawdown and consistency. Every one of those
measures the trader in isolation. None answers the question the bot actually has: *can we capture
any of this, fifteen seconds late, after paying the taker fee on both legs?*

Hold-time distribution is a decent proxy — a 20-minute flip is copyable, a 40-second scalp is not —
but it is a proxy. The repository already contains everything needed to measure the real thing: a
minute-resolution `prices` table, the `price_at()` hot query it was shaped around, and
`WINDOW_POST_S = 1500` with a comment saying it "must exceed the largest lag in the Step 4 sweep".
The sweep was never written.

## Decision

Build `copytrade/replay.py`. Match each candidate's fills into round trips, replay them against the
price history at copy lags of 0/15/30/60/300/900 seconds, charge `bk.fee` on both legs at the
market's own rate, and report what a copier would have captured.

Outputs: `copier_roi` per lag, `capture_ratio` (copier ROI at 15s ÷ the trader's own ROI), and
`edge_half_life` — the interpolated lag at which copier ROI reaches zero.

## Consequences

- A wallet with an excellent ROI and a 30-second edge half-life is now visibly not a candidate.
- Candidates need price-window backfill before they can be replayed, which costs requests. The
  interval algebra to plan that already exists in `ingest.py` and is reused rather than rebuilt.
- Replay is optional (`discover --no-replay`) so a fast pass is still available.

## Alternatives rejected

**Infer copyability from hold time alone.** Cheaper and ships sooner, but it cannot distinguish a
trader whose price keeps moving after they buy from one who buys the top of a move. That
distinction is the entire question.

**Full session backtest.** Replaying whole copy sessions including the exit ladder and circuit
breakers would answer more, but `prices` is minute-resolution with no historical order books, so
fills would be modelled from price alone. Deferred; replay produces its inputs.
