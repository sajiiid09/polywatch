# PROGRESS

The account's log, newest first. One entry per session, written by `polywatch session close`.

AI agents are stateless and human operators forget, so this file is how the account stays one
continuous operator across sessions that share nothing else. Read the top entry before doing
anything; it says what is held, what is still live on the exchange, and what was left undone.

It is generated. Add a human note with `polywatch session close <task> --note "..."` rather than
editing an entry: the same text is stored in the `sessions` table, and only one of the two copies
gets edited by hand.

**Nothing in here is applied automatically.** The next steps are instructions for an operator,
not a queue the bot drains.

## 2026-09-09 10:44 UTC — task `alpha` (run 1)

- **mode** paper · **stopped** session_end · **ran** 2026-09-09 10:44 → 2026-09-09 10:44
- **equity** $100.00 → $100.00 (+0.00)
- **realized** $0.00 after $0.10 of fees
- **signals** 0 seen, 0 copied, 0 skipped
- **positions** 0 closed, 1 open

**Attention**
- 1 position(s) are still open and NO STOP-LOSS IS RUNNING ON THEM. The CLOB has no stop order type, so a stop is a price this process watches; while nothing is running, nothing is watching. Either start a run to manage them or close them by hand.

**By trader archetype** — `unknown` 1 pos $0.00

**Next steps**
1. Decide about the open positions first: run the task to resume managing them, or flatten them by hand. Nothing is watching them meanwhile.

## 2026-09-09 10:33 UTC — task `alpha` (run 1)

- **mode** paper · **stopped** session_end · **ran** 2026-09-09 10:33 → 2026-09-09 10:33
- **equity** $100.00 → $97.40 (-2.60)
- **realized** $-6.40 after $5.10 of fees
- **signals** 20 seen, 7 copied, 13 skipped
- **positions** 16 closed, 2 open
- **latency** median 1788950033s behind the trader, feed 1788950033.0s vs loop 0.0s

**Attention**
- 2 position(s) are still open and NO STOP-LOSS IS RUNNING ON THEM. The CLOB has no stop order type, so a stop is a price this process watches; while nothing is running, nothing is watching. Either start a run to manage them or close them by hand.
- 1 live GTC sell order(s) are still resting on the exchange. They will fill whether or not anything is running, which is the feature working -- but the next run must be told rather than discovering the shares are gone.

**Why copies were refused** — `fee_floor` 7, `stale` 6

**How positions closed** — `max_hold` 8 ($-12.00), `follow_exit` 8 ($5.60)

**By trader archetype** — `longshot-hunter` 9 pos $5.60, `scalper` 9 pos $-12.00

**By trader** — `0xaaa` $5.60, `0xbbb` $-12.00

**Next steps**
1. Decide about the open positions first: run the task to resume managing them, or flatten them by hand. Nothing is watching them meanwhile.
2. Review the resting sell orders with `polywatch task orders`; cancel with `--cancel` if the thesis behind them has expired.
3. `fee_floor` was the top refusal (7). STRATEGY.md §10: the round trip costs more than the exit rule can win back at the prices being copied. That is a verdict on the markets they choose, not on them. If it dominates again next session, act on it rather than logging it twice.
4. Most positions were closed by the time stop rather than by the trader or the ladder, which says the roster's edge is slower than `max_hold_s` assumes.
5. 1 unapplied proposal(s) are waiting in `docs/STRATEGY_LEARNED.md` (`polywatch strategy show`). They are suggestions with sample sizes, not a queue -- applying one is a decision.

## 2026-09-09 10:39 UTC — task `alpha` (run 1)

- **mode** paper · **stopped** session_end · **ran** 2026-09-09 10:39 → 2026-09-09 10:39
- **equity** $100.00 → $100.00 (+0.00)
- **realized** $0.00 after $0.10 of fees
- **signals** 0 seen, 0 copied, 0 skipped
- **positions** 0 closed, 1 open

**Attention**
- 1 position(s) are still open and NO STOP-LOSS IS RUNNING ON THEM. The CLOB has no stop order type, so a stop is a price this process watches; while nothing is running, nothing is watching. Either start a run to manage them or close them by hand.

**By trader archetype** — `unknown` 1 pos $0.00

**Next steps**
1. Decide about the open positions first: run the task to resume managing them, or flatten them by hand. Nothing is watching them meanwhile.

## 2026-09-09 10:38 UTC — task `alpha` (run 1)

- **mode** paper · **stopped** session_end · **ran** 2026-09-09 10:38 → 2026-09-09 10:38
- **equity** $100.00 → $100.00 (+0.00)
- **realized** $0.00 after $0.10 of fees
- **signals** 0 seen, 0 copied, 0 skipped
- **positions** 0 closed, 1 open

**Attention**
- 1 position(s) are still open and NO STOP-LOSS IS RUNNING ON THEM. The CLOB has no stop order type, so a stop is a price this process watches; while nothing is running, nothing is watching. Either start a run to manage them or close them by hand.

**By trader archetype** — `unknown` 1 pos $0.00

**Next steps**
1. Decide about the open positions first: run the task to resume managing them, or flatten them by hand. Nothing is watching them meanwhile.

## 2026-09-09 10:38 UTC — task `alpha` (run 1)

- **mode** paper · **stopped** session_end · **ran** 2026-09-09 10:38 → 2026-09-09 10:38
- **equity** $100.00 → $100.00 (+0.00)
- **realized** $0.00 after $0.10 of fees
- **signals** 0 seen, 0 copied, 0 skipped
- **positions** 0 closed, 1 open

**Attention**
- 1 position(s) are still open and NO STOP-LOSS IS RUNNING ON THEM. The CLOB has no stop order type, so a stop is a price this process watches; while nothing is running, nothing is watching. Either start a run to manage them or close them by hand.

**By trader archetype** — `unknown` 1 pos $0.00

**Next steps**
1. Decide about the open positions first: run the task to resume managing them, or flatten them by hand. Nothing is watching them meanwhile.
