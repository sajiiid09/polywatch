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
```

## Read next

| Document | What it covers |
|---|---|
| [`STRATEGY.md`](STRATEGY.md) | The trading thesis: what edge is copied, the fee arithmetic, why a take-profit destroys the edge, how traders are chosen |
| [`RULES.md`](RULES.md) | Hard invariants, live-mode gates, risk limits, the never-do list |
| [`AGENTS.md`](AGENTS.md) | Module map, layering rules, how to add a knob or a table, test conventions |
| [`docs/decisions/`](docs/decisions/) | One ADR per decision |

## The one thing to understand before running it live

The take-profit is a resting order on the exchange and fills whether or not this program is alive.
**The stop-loss is not.** Polymarket's CLOB has no stop order type, so a stop is a price this
process watches and a market sell it sends. While the bot is not running, the stop is not running.
That is why a run has a session clock and flattens at the end of it.
