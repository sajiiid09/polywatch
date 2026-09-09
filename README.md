# polywatch

Skill-vs-luck analytics for Polymarket wallets, and a copy-trading bot built on it.

Finds wallets whose record is distinguishable from luck, measures whether a *copier* could have
captured any of it after fees and latency, and then copies the ones that survive — on paper by
default, or live behind an explicit flag and a typed confirmation.

The core runs on the Python standard library alone. Live trading and book streaming are optional
extras, which is what keeps "does this program spend money" answerable by checking whether the
`live` extra is installed.

## Install

```sh
uv venv && uv pip install -e .          # analytics + paper trading
uv pip install -e '.[live]'             # + signed orders on the CLOB
uv pip install -e '.[stream]'           # + websocket book streaming
```

## Use

```sh
polywatch init-db                       # create the schema
polywatch ingest --wallets 50           # leaderboard -> trades -> markets -> prices
polywatch screen                        # re-screen with different thresholds
polywatch discover --limit 20           # sweep, screen, score, replay, rank
polywatch trader 0xabc...               # one wallet's score card

polywatch task create alpha --trader 0xabc... --stake 10
polywatch task run alpha                # paper by default
polywatch task report alpha
polywatch task orders --cancel          # resting GTC orders left on the book

polywatch strategy backfill             # label every wallet already in the DB (offline)
polywatch strategy show                 # what each archetype cost, and what to consider changing
polywatch strategy learn                # ...and write it to docs/STRATEGY_LEARNED.md

polywatch session open alpha            # what the last session left you
polywatch session close alpha           # record this one for whoever is next
```

## Read next

| Document | What it covers |
|---|---|
| [`STRATEGY.md`](STRATEGY.md) | The trading thesis: what edge is copied, the fee arithmetic, why a take-profit destroys the edge, how traders are chosen |
| [`RULES.md`](RULES.md) | Hard invariants, live-mode gates, risk limits, the never-do list |
| [`AGENTS.md`](AGENTS.md) | Module map, layering rules, how to add a knob or a table, test conventions |
| [`docs/STRATEGY_LEARNED.md`](docs/STRATEGY_LEARNED.md) | **Generated.** What each trader archetype actually cost when copied, and proposals — none of them applied |
| [`docs/PROGRESS.md`](docs/PROGRESS.md) | **Generated.** The account's log, newest first: one entry per session, with what it left undone |
| [`docs/decisions/`](docs/decisions/) | One ADR per decision |

## Two documents the program writes to itself

The bot classifies every wallet it scores into a strategy archetype — longshot-hunter,
favourite-grinder, resolution-holder, scalper — and groups every copied position, refusal and
exit by that archetype rather than by wallet address. A wallet goes quiet and takes its record
with it; a pattern accumulates. `docs/STRATEGY_LEARNED.md` is where that accumulates, along with
proposed changes, each carrying the sample size behind it. **Nothing in it is ever applied
automatically** (`RULES.md` I8).

`docs/PROGRESS.md` is the account's log. Because AI agents are stateless and human operators
forget, every session ends by recording the account state and writing down what it left undone,
so the next session can pick the account up as though it had never been put down. It is a
briefing, not a resume: starting a run inherits no positions, no watermark and no trailing stop
from it, by design.

## The one thing to understand before running it live

The take-profit is a resting order on the exchange and fills whether or not this program is alive.
**The stop-loss is not.** Polymarket's CLOB has no stop order type, so a stop is a price this
process watches and a market sell it sends. While the bot is not running, the stop is not running.
That is why a run has a session clock and flattens at the end of it.
