# AGENTS

How to work in this repository. Read `STRATEGY.md` for what the bot is trying to do and
`RULES.md` for what it may not do. This file is about the code.

---

## Orientation

```
src/polywatch/
  cli.py              argparse only; every command dispatches elsewhere
  config.py           static constants, all of them commented with how they were verified
  ingest.py           leaderboard -> trades -> markets -> price windows, resumable
  screen.py           behavioural wallet screening (pure, over rows already in the DB)
  fetch/
    client.py         HTTP: retries, backoff, rate limit, raw dumps. GET only, no auth
    polymarket.py     endpoint wrappers. Return raw JSON, no field access
  parse/
    fields.py         typed field accessors with context on failure
    records.py        raw JSON -> row dicts. The ONLY place upstream shapes are interpreted
  db/
    schema.sql        the whole schema, heavily commented
    store.py          every SQL statement in the project
  copytrade/
    task.py           the Task dataclass: one trader set, one bankroll, one rule set
    book.py           order-book arithmetic (pure)
    exits.py          the exit ladder and entry gates (pure)
    engine.py         the poll loop. fetch -> decide -> execute
    execution.py      PaperExecutor and LiveExecutor behind one interface
    reconcile.py      exchange-vs-database diff; live safety
    stream.py         CLOB book websocket with polling fallback
    skill.py          per-wallet skill metrics from settled positions (pure)
    replay.py         copy-lag replay: what a copier would actually have captured
    sweep.py          leaderboard sweep across categories/windows/orderings
    rank.py           combine metrics into rank_score / persona_fit
    discover.py       fetch + persist + format one wallet's score
    report.py         what a run did, read back out of the database
    commands.py       `polywatch task ...` argument shuffling
```

## The layering rule

`fetch` knows about sockets and never about the schema. `parse` knows about upstream shapes and
never about SQLite. `store` knows about SQLite and never about HTTP. `copytrade` composes them.

A field name from Polymarket's JSON appearing anywhere outside `parse/records.py` is a layering
violation. So is a SQL string outside `db/store.py`.

## Purity

`book.py`, `exits.py`, `skill.py` and `screen.py` are pure: dicts in, numbers out, no network and
no database. That is what makes the exit ladder testable without a market and the skill metrics
testable without a wallet. Keep them that way — if a function there needs the clock or the network,
the caller passes it in (`now=`, `fee_rate=`, `high_water=`).

`engine.py` takes its connection, executor, client, logger and clock by constructor injection for
the same reason.

## Adding things

**A config knob** → a field on the `Task` dataclass in `task.py`, with a default in `config.py` if
it deserves a name. `Task.from_row` drops unknown keys and falls back to defaults for missing ones,
so old saved tasks keep loading and no migration is needed. Add the CLI flag in `cli.py` and the
mapping in `commands._build`.

**A schema column or table** → `schema.sql` for new databases, *and* an additive `PRAGMA
table_info` block in `store.init_db` for existing ones. `CREATE TABLE IF NOT EXISTS` will not add a
column to a table that already exists. There are 1 GB of existing data; it must keep opening.

**A new skip reason** → a short, coarse string. These become the histogram in `task report`, which
is the main output of a paper run, so a proliferation of near-synonyms destroys the signal. Name it
in `exits.py` beside the others.

**An endpoint** → a wrapper in `fetch/polymarket.py` returning raw JSON, a parser in
`parse/records.py`, and a docstring recording what you verified and when. Every quirk in that file
was probed, not guessed, and the dates matter.

## Testing

- `pytest -q` from the repo root. The suite is fast (sub-second) and must stay that way.
- Pure functions get direct unit tests. Anything touching the network gets a fake client or a
  fixture from `tests/fixtures/`.
- The engine is tested with a fake executor and an injected clock — see
  `tests/test_copytrade_engine.py` for the pattern. No test may hit the live API.
- A bug fix lands with a test that fails before it, especially for accounting: fee and PnL defects
  are silent by nature.

## Style

The comments in this codebase carry reasoning, not narration. `# increment counter` is noise;
`# The watermark starts at "now", not at zero, because otherwise the first poll records days of
history as stale signals and drags the latency percentiles into the tens of thousands` is the
house style. When you make a non-obvious choice, write down the alternative you rejected and why.

Where a number was measured, say what it was measured on and when. Where it was guessed, say that
too — `fee_source` exists as a column precisely so a guess is visible downstream.

## What must never regress

- The read-only guarantee of `fetch/client.py` (GET, no auth, no body, no signing).
- Money spent in exactly one class (`RULES.md` I5).
- Every SQL statement in one file (`RULES.md` I6).
- Every observed trader action recorded with a reason (`RULES.md` I1, I2).
- The existing test suite, green.
- Databases created by earlier versions, still opening.

## Working agreement for agents

- Read `STRATEGY.md` and `RULES.md` before changing trading behaviour. A change that contradicts a
  measured finding needs a new measurement, not an argument.
- Record decisions in `docs/decisions/` as short ADRs. Update `STRATEGY.md` / `RULES.md` in the
  same commit when a decision changes them.
- Prefer editing an existing module to adding one. This codebase is small on purpose.
- Do not add a dependency without a reason that survives `RULES.md` I5. Optional extras exist so
  the core stays standard-library-only: `live` (`py-clob-client`) is what makes the program able
  to spend money, and `stream` (`websocket-client`) only ever reads, which is why they are
  separate — installing `stream` says nothing about whether this program can trade.
