# 0007 — A session hands over information, never state

**Date:** 2026-09-09 · **Status:** accepted

## Context

A run inherits nothing from its predecessor, deliberately. `Engine.start` closes any run left
open as `abandoned` rather than adopting its positions, seeds the poll watermark to *now* rather
than to a stored value, and discards the trailing stop's high-water marks. Each of those has a
comment saying why: inheriting trading state silently would make the next report a fiction.

What is right for trading state is wrong for the operator. Whoever picks the account up next —
a person, or a stateless agent session that has never seen the last one — gets a `task_runs` row
and has to reconstruct the rest: what is held, what is still resting on a real exchange, which
trader was dropped and why, which refusal dominated the histogram. There is no history command,
no notes, and no handoff. Until now the closest thing to a progress log in this repository was a
static plan document.

## Decision

A session ends by writing two artifacts:

- a **`sessions` row** — machine state, in the database because `RULES.md` I1 says a finished run
  must be explicable from there alone, and a handoff that exists only as a file on disk is not;
- an entry at the top of **`docs/PROGRESS.md`** — the briefing, in prose, for whoever is next.

`polywatch session close` writes both, and `task run` calls it at the end of every run, including
the path where the run refused to start. The run that ended badly is exactly the run whose
handoff matters, and it is also the one least likely to have reached the end of `task run`
cleanly — which is why `session close` also exists as a command of its own.

**`polywatch session open` prints the briefing and does nothing else.** It is not a resume.
Nothing restores a watermark, reopens a position or re-arms a stop. The handoff is advisory by
construction, which is what allows it to coexist with the engine's refusal to inherit — and the
printed output says so, because the natural expectation of a command called `open` is that it
restores something.

Entries are newest-first. The reader is an operator with a budget: a person skimming, or an agent
with a context window. Chronological order buries the only entry that matters.

**No scheduler is built.** `session open`, `task run`, `session close` are commands. What invokes
them — cron, launchd, a person, an agent — is outside this repository. The bot does not start
itself, which keeps `STRATEGY.md` §5 true: the stop-loss runs only while the process does, and
nothing here quietly arranges for the process to be running unattended.

## Consequences

- `next_steps` and `briefing` are pure, so the same situation produces the same instruction every
  time. An agent reading the log is entitled to a consistent operator, not a differently-worded
  one each session.
- The dominant skip reason is mapped to the falsification in `STRATEGY.md` §10 that it settles,
  so a run points at the sentence it answers instead of restating its own histogram.
- Open positions and live resting orders are reported as warnings before anything else, in the
  same words `Engine.stop` uses.
- `engine.py` is untouched: `session.close` runs after the engine returns, reading the database
  the engine already wrote.

## Alternatives rejected

**Persist the watermark and open positions so a run resumes.** It is the obvious reading of
"continuity" and it contradicts two load-bearing comments in `engine.py`. A resumed trailing stop
trails off a high-water mark set while nothing was watching.

**Keep the handoff only in the database.** Queryable, and unreadable by the operator who most
needs it — someone opening the repository with no session context at all.

**Install a launchd job so runs fire on a timer.** Unattended runs are unattended stop-losses.
That is a decision for a person to take explicitly, not a side effect of adding a log.
