# Strategy learning and agent continuity — phases 6 and 7

**Status:** accepted 2026-09-09, implemented. Continues
[`2026-09-09-copytrade-audit-design.md`](2026-09-09-copytrade-audit-design.md) (phases 0–5).

## Context

Two gaps, both about what survives the end of a run.

**The bot copied traders without ever asking what they were doing.** `skill.py` measured whether a
wallet was good, `replay.py` whether it was copyable, `rank.py` combined the two. Nothing recorded
*what kind* of trader it was. A paper run's PnL attributed to an address and stopped there, so
when the wallet went quiet the finding left with it. The account is meant to eventually trade on
a thesis of its own; that needs evidence about which patterns pay after fees and latency, not a
list of addresses that did.

**Nothing survived the end of a run for the operator.** That is deliberate for trading state:
`Engine.start` closes an abandoned run rather than adopting it, seeds the watermark to *now*, and
discards the trailing high-water marks, each with a comment explaining why. But it meant whoever
picked the account up next — a person, or a stateless agent session — got a `task_runs` row and
had to reconstruct the rest. There was no history command, no notes, no handoff. The closest
thing to a progress log in the repository was a static plan document.

## Decisions

Recorded as [ADR 0006](../decisions/0006-strategy-archetypes-are-rule-based.md) and
[ADR 0007](../decisions/0007-sessions-hand-over-information-not-state.md).

| # | Decision |
|---|---|
| D6 | Archetypes are **rule-based over stored trades** — deterministic, testable offline, no new dependency. A label is only worth accumulating if a wallet classifies the same way twice |
| D7 | Generated findings live in `docs/STRATEGY_LEARNED.md`, written whole. `STRATEGY.md` stays hand-written and gains one pointer section |
| D8 | The learning loop **proposes and never applies** — `RULES.md` I8 |
| D9 | A session hands over **information, never state** — `RULES.md` I9. `session open` prints; it does not resume |
| D10 | No scheduler. What invokes the commands is outside this repository |

## What was built

**Phase 6 — strategy learning**

- `copytrade/strategy.py` (pure) — features from stored trades: entry-price percentiles, hold
  times, pre-entry price drift (the momentum-versus-fade axis, and the reason `WINDOW_PRE_S` is
  300), the share of buys never sold, scale-in behaviour, the win/loss size ratio, category
  concentration, and cadence borrowed from `screen.profile_wallet`. Eight coarse archetypes, each
  scored in [0,1] through the `_ramp` helpers already in `rank.py`.

  Two disciplines carry the module. An archetype **gated on the feature that defines it** scores a
  flat 0.5 when that feature is missing, rather than inheriting a high score from components it
  shares with a neighbour — without this, `momentum-chaser` and `fade-the-move` tied at the top of
  every wallet with no price history and buried whatever the wallet actually was. And a wallet
  whose top two archetypes are within `ARCHETYPE_MARGIN_SCALE` comes back `unclassified`: an
  honest refusal beats a coin flip, because the label is the grouping key of every finding
  downstream.

- `trader_strategy` and `strategy_snapshots` tables; `archetype` / `strategy_confidence`
  denormalised onto `trader_scores` through the existing `PRAGMA table_info` migration.
  Snapshots append rather than upsert, because the question they answer is a trend.

- Three aggregators in `store.py` group positions, signals and exits by archetype. Positions
  written before `positions.trader` existed are recovered from the run's task when it names
  exactly one wallet, and left unattributed when it names several — a wrong attribution moves PnL
  onto an archetype that did not earn it, which is worse than a missing one.

- `copytrade/learn.py` — six proposal rules, each a named function with its own test. Every
  proposal carries `n` and a confidence; anything under the floor is demoted to an observation.
  `stale_rule` is the worked example of restraint: it reads the latency split first and proposes
  **nothing** about the poll interval when the delay belongs to the feed, because a shorter poll
  cannot recover time already spent before the response arrived.

- `strategy backfill` classifies an existing database offline, from trades already stored.

**Phase 7 — agent continuity**

- `sessions` table carrying the briefing text itself, because `RULES.md` I1 says a finished run
  must be explicable from the database alone.
- `copytrade/session.py` — `account_state` composes readers that already existed; `warnings`,
  `next_steps` and `briefing` are pure, so the same situation produces the same instruction every
  time. An agent reading the log is entitled to a consistent operator.
- `docs/PROGRESS.md`, newest first: the reader is an operator with a budget.
- `task run` closes its own session, including on the path where the run refused to start — the
  run that ended badly is the one whose handoff matters. A failed handoff never fails the run.
- `engine.py` is untouched. `session.close` runs after the engine returns.

## Results on the existing database

2,026 wallets classified offline in twelve seconds from 1.1M stored fills: 1,147
resolution-holders, 455 unclassified, 169 longshot-hunters, 99 market-makers, and the rest spread
thin. Across 78 recorded runs and 2,043 positions, resolution-holders returned $840.57 and
longshot-hunters $388.13, against $212.53 of fees in total.

The unclassified group is mostly a genuine two-way tie between `market-maker` and
`resolution-holder` — wallets that accumulate on machine cadence and exit by settlement. Both
labels are true and both are disqualifiers, so the tie costs no decision and forcing a pick would
be false precision.

No proposals cleared their thresholds, which is the correct early state and is said so in the
document rather than padded.

## Verification

301 tests, sub-second, none touching the network. The two new files cover: feature derivation
against hand-built trade lists and a dict `price_at`; one canonical vector per archetype
classifying to itself; the ambiguity refusal; determinism; the gating fix; migration from the
previous `schema.sql`; one test per proposal rule including the negative that a feed-dominated
latency must **not** propose a faster poll; newest-first ordering; and the invariant that a run
started after a handoff inherits no positions.

Manually: `strategy backfill` / `show` / `learn` and `session open` / `close` / `log` against both
the 1 GB database and a seeded scratch one, confirming that `strategy learn` leaves `STRATEGY.md`
byte-identical.
