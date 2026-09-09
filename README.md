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
polywatch account                       # what the exchange says your own account holds

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

## Going live

Live mode needs the `live` extra, a funded Polymarket account, and four environment variables.
Credentials are read from the environment only — never a config file, the database, the raw dump
or a log line.

| Variable | Required | What it is |
|---|---|---|
| `POLYMARKET_PRIVATE_KEY` | yes | the EOA key that signs orders |
| `POLYMARKET_FUNDER` | yes | the proxy address holding the USDC — your Polymarket address |
| `POLYMARKET_SIGNATURE_TYPE` | no | `1` email/magic login (default), `2` browser wallet, `0` bare EOA |
| `POLYMARKET_API_KEY` / `_SECRET` / `_PASSPHRASE` | no | derived from the key when unset |

```sh
set -a; source .env; set +a             # nothing auto-loads .env; it is gitignored
polywatch account --live-check          # keys, wallet type, CLOB auth — places no order
```

Two things the preflight cannot check: that the account's USDC allowances are approved (they are,
if it has ever traded through the Polymarket UI — otherwise the first order fails on allowance),
and that `POLYMARKET_SIGNATURE_TYPE` matches how the account was created. A mismatch is not
dangerous, but every order comes back rejected at the signature check without saying why.

`--yes` skips the typed `LIVE` confirmation. Outside a terminal it is refused unless
`POLYWATCH_UNATTENDED=1` is set as well, because the same flag that saves a person one keystroke
is, in a cron entry, money moving with nobody watching and no stop-loss running between sessions.

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
