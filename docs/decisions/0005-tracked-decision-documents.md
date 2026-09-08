# 0005 — STRATEGY, RULES and AGENTS are the tracked source of truth

**Date:** 2026-09-09 · **Status:** accepted

## Context

Every design decision in this project lived in module docstrings. They are unusually good
docstrings — the fee arithmetic, the measured finding that a take-profit destroys the edge, the
reason `closed-positions` must be sorted by timestamp — but they are scattered, invisible to
anyone not already reading that file, and there was no README at all.

## Decision

Three tracked documents at the repository root, plus ADRs:

- `STRATEGY.md` — the trading thesis, the fee arithmetic, how traders are chosen, what would
  falsify the approach.
- `RULES.md` — invariants, live-mode gates, risk limits, the never-do list.
- `AGENTS.md` — module map, layering rules, how to extend, what must never regress.
- `docs/decisions/` — one short ADR per decision.

They are kept current as work lands: a change to trading behaviour updates `STRATEGY.md` in the
same commit, a change to a limit or a gate updates `RULES.md`, and a reversal of a recorded
decision supersedes its ADR rather than editing it away.

## Consequences

- Module docstrings stay, and stay authoritative on *how a module works*. The tracked documents own
  *why the system behaves as it does*.
- A default that contradicts a measured finding is now a visible contradiction rather than a
  buried one.
