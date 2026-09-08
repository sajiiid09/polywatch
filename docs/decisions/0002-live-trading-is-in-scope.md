# 0002 — Live trading is the goal, so live defects are blocking

**Date:** 2026-09-09 · **Status:** accepted

## Context

The audit found three P0 defects on the live path: an order response the executor cannot read is
reported as a rejection when it may have filled; there is no reconciliation against the exchange at
all; and nothing prevents holding both outcomes of the same market.

All three are harmless in paper mode. All three are unrecoverable with real money — a fill the
database does not know about cannot be found again by this program.

## Decision

Treat live as the goal of this cycle, and therefore treat the P0s as blocking rather than
deferrable. No live run happens until reconciliation exists.

Specifically: an undeterminable fill becomes `Fill(status="unknown")` and is resolved by
reconciliation before a position is booked; `reconcile.py` diffs the exchange's view of positions
and open orders against the database on start and after any tick that produced an `unknown`, and
stops the run on a mismatch; and the both-sides guard becomes an entry gate with its own skip
reason.

## Consequences

- Roughly one extra work block before anything else can be trusted with money.
- `api.positions()`, present and unused since Phase 1 of the original build, gets its purpose.
- `RULES.md` gains gates L5 and L6, which are enforced in code rather than by convention.

## Alternatives rejected

**Paper-only this cycle.** Ships the evaluation work sooner, but leaves a live path that looks
complete and is not. A partially-safe live mode that is present but must not be used is worse than
one that refuses.
