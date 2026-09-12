# 0008 — Detect the copied wallets' fills on Polygon, not from data-api

**Date:** 2026-09-12 · **Status:** accepted · **Amends:** 0003

## Context

ADR-0003 settled the exit-latency problem and, in passing, declared the entry-latency problem
unsolvable:

> Entry latency is a different problem with a different answer: a third party's fills can only be
> polled. The CLOB market websocket carries no wallet address, and the user websocket reports only
> your own account. That is verified, and it is not fixable.

Both sentences about Polymarket's websockets are correct and were properly verified. The
conclusion drawn from them was not, because it silently assumed Polymarket's API is the only place
a Polymarket fill can be observed. The fills settle on Polygon, and Polygon is not Polymarket's to
gate.

The cost of the assumption was the dominant term in the whole latency budget. Measured across four
real runs (`docs/PROGRESS.md`), against an edge half-life of 51-59s on the quick-swing wallets this
bot is pointed at:

| run | poll | total p50 | feed | loop |
|---|---|---|---|---|
| 82 swing1 | 3s | 9s | 16.5s | 0.0s |
| 81 sim3-100 | 15s | 23s | 20.5s | 0.0s |
| 79 sim1 | — | 11s | 21.3s | 0.2s |
| earlier | — | 14s | 13.0s | 0.4s |

The loop was already at 0.0-0.4s. Every tuning lever the code exposes — `POLL_INTERVAL_S`, the page
size, the worker count — acts on the 0.4s and not on the 15s, which is why `report.latency_block`
had grown a line telling the operator that a shorter poll would not help. It was right, and it was
the wrong problem to be right about.

## What was measured, 2026-09-12

1. **data-api's `timestamp` for a fill *is* the Polygon block timestamp.** Equal, to the second,
   on a captured fill (block 93636810, `trader_ts` 1789160248, delta 0). So the feed lag was never
   the trade being unknowable. It was the indexer's queue, and the run was sitting in it.
2. `/activity` ships `cache-control: public, max-age=15` — the fifteen seconds, in the header.
3. **The maker is an indexed topic** on the exchange's `OrderFilled`, so a node filters to the
   roster before a byte crosses the wire.
4. **The event body reconstructs data-api's record exactly** — token id, shares, USDC and price all
   identical to the share and the cent, against captured fills now frozen in
   `tests/fixtures/orderfilled_log.json` (a buy) and `orderfilled_sell_log.json` (a sell).

   The body is `(side, tokenId, makerAmountFilled, takerAmountFilled, fee, _, _)`. **The first
   word is a side flag, not an asset id** — 0 where the maker paid collateral, 1 where they
   delivered the token — and the taker is the other side of the same trade. This is worth
   stating flatly because the first implementation read it as `makerAssetId` and inferred the
   side from which leg held asset id 0. That is accidentally correct for a buy, where the flag
   is 0, and wrong for every sell, where it is 1 and no leg holds 0. See "What the measurements
   caught" below.
5. **Free public RPC is fast enough and needs no key.** `wss://polygon-bor-rpc.publicnode.com` and
   `wss://polygon.drpc.org` both accept `eth_subscribe` and deliver blocks at, or a shade before,
   the timestamp those blocks claim.
6. **A/B against the live feed, n=329 matched fills over 200s**, both routes watching the same
   transactions (measured before the coverage defects below were found; the latency figures are
   unaffected by them, since they concern which fills arrive, not how fast):

   ```
   chain     p50  -0.3s   (min -1.2s, max  +0.8s)
   data-api  p50  10.8s   (min  0.8s, max 167.2s)
   saved     p50  11.1s
   ```

   The data-api column is if anything flattered by the harness, which sampled four wallets on a
   2s round-robin; the recorded runs above put its real median at 13-21s. The chain column is
   negative because Polygon stamps a block at proposal and we have the logs by the time it lands.

## What the measurements caught

Two defects survived a green test suite and a working end-to-end paper run, and both were found
only by comparing the two routes fill-for-fill against each other on live data. Recording them
because the lesson is about the method, not the bugs.

- **Every sell was silently dropped.** The side-flag misreading above. Coverage against data-api
  over a two-minute window was **52.8%** — and **0%** on a wallet that happened to be selling all
  afternoon. Nothing failed, nothing logged; the poll quietly picked up the remainder thirteen
  seconds later and the run looked fine.
- **A third exchange contract nobody had written down.** The subscription filtered on a list of
  known exchange addresses. Chasing the last 2% of misses turned up
  `0xe111180000d2663c0091e4f400237545b87b996b`, emitting the same event and decoding correctly.
  The address filter is now gone: a log must already name one of the copied wallets to reach us,
  which is filter enough, and subscribing by event signature alone means the next deployment
  cannot cause the same silent loss. `CHAIN_EXCHANGES` survives only so an unrecognised contract
  gets a log line rather than passing unnoticed.

After both fixes, coverage is **100% (243/243)** across three wallets, and the mix in a paper run
inverted — chain 222→485 signals, activity 514→255, blended p50 latency 3s→0s.

The method worth keeping: a feature whose whole purpose is to be *earlier* cannot be validated by
tests that pass, because its failure mode is being silently later. It has to be A/B'd against the
thing it replaces, on live data, counting both sides.

## Decision

Add `copytrade/tradestream.py`: a `TradeStream` that subscribes to `OrderFilled` on Polygon,
filtered by the roster on both the maker and the taker topic, across two racing endpoints, and
hands the engine the same `/activity`-shaped dict the poller hands it. Both routes converge on the
unchanged `Engine.handle_event`, so every gate, cap, breaker and the whole execution path are
untouched.

**The `/activity` poll stays exactly as it is.** This is an accelerator bolted onto it, not a
replacement, and three properties make it safe to bolt on:

- **Both sockets dead means the run is what it was.** `live` goes False, the 3s poll carries it at
  the old latency. No new single point of failure.
- **A fill it cannot place falls through rather than blocking.** The chain names a fill by token id
  alone; the gates want the condition id. The index is warmed at startup from each trader's
  `/activity` and `/positions` and self-heals from every poll thereafter, but a miss records
  `token_unresolved` and lets the poll copy the same fill seconds later. It never fetches to close
  the gap — a round trip on this path would hand back the latency the path exists to remove.
- **It cannot double-buy.** See below; this is the one thing that had to be got right.

## The hazard this introduces, and what closes it

`signals` is `UNIQUE (run_id, tx_hash, token_id, side, size)` with `size` stored as `REAL`. Two
routes now compute that float differently — one divides a uint256 by 1e6, the other parses
Polymarket's decimal string — and a value like `825.09091` can land one ULP apart, in which case
the constraint does not fire and the run buys the same fill twice. That is precisely the failure
`insert_signal`'s docstring exists to prevent, reintroduced through a side door.

Two guards, both required:

- Both paths round amounts to six places (`records.AMOUNT_DP`), the scale the exchange quotes in,
  making them bit-identical. Tested by `.hex()` comparison, not by `==`.
- A bounded in-memory set on `RunState.handled`, keyed on exact integer micro-units so two genuine
  partial fills in one transaction still count as two.

## Consequences

- Entry detection drops from a ~15s floor to roughly a block. On the replay curve in
  `config.py:107-113` that moves a copied trade from the +8..+11% band to the +16..+27% one.
  Observed in a paper run: chain −0.4s average against data-api's 62.2s for the fills it saw
  first. That 62.2s is a selection effect and should be read as one — once the chain feed takes
  the fast majority, what is left for the poll to see first is disproportionately the slow tail.
- `MAX_SIGNAL_AGE_S = 45` stops spending a third of its budget before the first gate runs.
- `signals` gains a `source` column, and `report.latency_block` breaks latency out by route. The
  blended median now moves with the mix between the two routes as much as with either getting
  faster, so the headline figure alone became misleading and the breakdown is the number to read.
- Roughly 4% of chain fills arrive for a token the local index cannot place and are recorded
  `token_unresolved`, then copied by the poll seconds later. Visible in the skip histogram, which
  is where the decision to build a market crawl should come from if it ever needs building.
- A new failure mode with an old answer: a shallow Polygon reorg could retract a fill we copied.
  Withdrawn logs are dropped before they are enqueued; a reorg after we have acted leaves us
  holding our own position at our own price, which is a bad signal rather than a broken ledger.
  Not otherwise handled, deliberately.
- `websocket-client` was already the `stream` extra's dependency, so this adds no new one.
- **The lesson worth keeping**, given ADR-0003 recorded the opposite as verified fact: "we probed
  the vendor's API and it cannot do this" is a finding about the vendor's API. It is not a finding
  about the world, and it should not be written down as one.

## Rejected

- **Rust, for any of it.** The budget is feed ~15s, loop ~0.0-0.4s, execution ~0.3-0.8s. A rewrite
  buys tens of milliseconds against a thirteen-second problem. The lever was the data source.
- **A mempool watcher**, to beat the block rather than ride it. Sub-second is available and would
  need a paid mempool-capable RPC and calldata decoding of the operator's batched matches. Nothing
  in the measurements says the last ~1s is worth that; revisit only if it ever does.
- **Moving `market_meta` from gamma to `clob /markets/{condition_id}`.** Planned as a cheap win —
  one request instead of gamma's open-then-closed pair — and abandoned on inspection. The CLOB
  market carries `taker_base_fee=1000` where gamma carries the actual schedule
  (`rate 0.05, exponent 1, takerOnly, rebateRate 0.15`), and carries no `feeType` at all, which
  `derive_category` needs. The swap would trade a ~100-300ms saving on a cold cache for a
  degraded fee gate on a path that spends money. It is also a smaller prize than it looked: the
  second request only fires for markets gamma considers closed, which the entry path rejects
  anyway.
- **Replacing the poll.** It is the reconciliation source, it carries SPLIT/MERGE/REDEEM, and it is
  the fallback that makes every failure mode above survivable.
