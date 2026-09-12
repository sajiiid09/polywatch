# 0003 — Stream books over websocket; keep polling for signals

**Date:** 2026-09-09 · **Status:** accepted · **Amended by:** [0008](0008-detect-fills-on-chain-not-from-data-api.md)

## Context

The stop-loss, the trailing stop and the time stop are enforced by this process, and the process
evaluates them once per poll — every 15 seconds. Worse, the loop is serial and fetches every open
position's book twice per tick, once in `manage_positions` and again inside `check_breakers` →
`unrealized()`, so tick length and therefore stop-loss latency grow linearly with position count.

Entry latency is a different problem with a different answer: a third party's fills can only be
polled. The CLOB market websocket carries no wallet address, and the user websocket reports only
your own account. That is verified, and it is not fixable.

> **Amended 2026-09-12 by ADR-0008.** The two verified facts above are correct. The conclusion
> drawn from them is not: it holds for Polymarket's API, not for the Polygon logs the fills
> settle into, where the maker is an indexed topic. Measured at 11.1s saved at the median over
> 329 live fills. The decision below stands unchanged — it is about books, and books are still
> best served by the CLOB socket.

## Decision

Subscribe to the CLOB market websocket for the tokens we hold, maintain an in-memory book cache,
and evaluate the exit ladder on book updates rather than on the poll tick. Keep polling `/activity`
for signals and keep the tick for circuit breakers. Deduplicate the double book fetch. Parallelise
whatever still polls, using the `ThreadPoolExecutor` helper already in `ingest._parallel`.

The websocket needs a dependency, and the repository is standard-library-only by design. It goes in
a **separate optional `stream` extra** (`websocket-client`), with polling as the fallback when the
extra is absent or the socket has been silent past a staleness bound.

## Consequences

- Exit latency drops from 15-second granularity to roughly the socket's update rate.
- Book fetches leave the tick almost entirely, so tick length stops scaling with position count.
- Entry latency is unchanged, because it is feed-bound. `signals` gains `fetch_ts` so the feed's
  lag and the loop's own can finally be reported separately and `POLL_INTERVAL_S` tuned from data.
- The stdlib-only guarantee survives for paper mode and every analytic path.

## Alternatives rejected

**Parallelise only.** Real but bounded: it removes the double fetch and the serial stall, and
leaves the exit ladder 15-second-granular, which is the part that actually costs money in a fast
market.
