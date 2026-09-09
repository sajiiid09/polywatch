# 0006 — Classify traders by rule, and let the findings only propose

**Date:** 2026-09-09 · **Status:** accepted

## Context

`skill.py` says whether a wallet is good and `replay.py` says whether it is copyable. Neither
asks what it is *doing*, so a paper run's PnL attributes to an address and stops there. "0xab..
made $4" does not accumulate into anything: the wallet goes quiet, and the finding leaves with
it.

The account is meant to eventually trade on a thesis of its own rather than copy someone else's.
That needs a body of evidence about which *patterns* pay after fees and latency, not a list of
addresses that did.

## Decision

Every wallet gets a strategy archetype — `longshot-hunter`, `favourite-grinder`,
`resolution-holder`, `scalper`, and five others — derived by rule from trades already in the
database. Positions, skips and exits are then grouped by archetype rather than only by address,
and `polywatch strategy learn` writes the accumulated record to a generated
`docs/STRATEGY_LEARNED.md`.

**Rule-based, not clustered and not model-labelled.** A label is only worth accumulating if it is
stable: a wallet must classify the same way twice or an archetype's track record drifts with the
labeller rather than with the traders. Rules are also testable offline and add no dependency.
The cost is that only shapes someone thought of can be found, which is recorded rather than
hidden — `classify` returns every archetype's score and refuses to label when the top two are
close.

**The findings propose and never apply** (`RULES.md` I8). `strategy learn` writes documents and
snapshot rows; it never touches a task, a knob or a roster. `RULES.md` §6 already demands that a
change contradicting a measurement be argued with a new measurement, and a bot that can quietly
loosen `max_fee_frac` after a bad afternoon has decorative risk limits.

**Generated findings live in their own file.** `STRATEGY.md` is hand-written and stays that way;
a machine must not overwrite reasoning it did not derive. §11 of it points at the generated
document and nothing more.

## Consequences

- `trader_strategy` (one row per wallet, rescan overwrites) and `strategy_snapshots` (append-only,
  because the question is a trend and an upsert would erase the drift).
- Classification happens inside `discover.score_trader`, on rows it has already fetched, so it
  costs no requests. `strategy backfill` labels an existing database offline.
- Every proposal carries its sample size; anything under the floor is demoted to an observation
  rather than inflated into a recommendation.
- The `stale` rule checks the latency split before proposing a faster poll. When the feed is the
  bottleneck it proposes nothing and says why — the confidently wrong conclusion this whole
  module is shaped to avoid.

## Alternatives rejected

**Cluster the feature vectors.** Finds structure nobody anticipated, but needs numpy and
scikit-learn, breaking the stdlib-only core, and cluster identity is not stable across reruns —
which is the one property the record depends on.

**Have a model write the labels and the prose.** Richer descriptions, non-deterministic output,
no test can assert it, and it needs credentials in a program whose whole read-only guarantee is
that it has none.

**Let the loop auto-apply high-confidence proposals.** Faster iteration, and it makes the bot
able to widen its own risk limits on the evidence of a run that went badly. Declined.
