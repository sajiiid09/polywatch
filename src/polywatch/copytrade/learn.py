"""Turn what happened into what to consider changing -- and stop there.

This is the module that accumulates evidence for the day the account trades on a thesis of its
own rather than copying someone else's. It reads the run record grouped by trader archetype
(`strategy.py` supplies the grouping), and emits two things: findings, which are measurements,
and proposals, which are suggestions.

**A proposal is never applied.** `RULES.md` I8. Nothing here writes a `Task`, a knob, a roster or
a default; `strategy learn` writes snapshot rows and a markdown document and that is the entire
extent of its authority. The reason is the one `RULES.md` §6 already gives for human changes: a
change to a default that contradicts a measured finding needs a new measurement, not an argument
-- and a bot that can quietly loosen its own `max_fee_frac` after a bad afternoon is a bot whose
risk limits are decorative.

The second discipline here matters as much as the first: **a proposal carries its sample size,
and anything under the floor is demoted to an observation.** A rule that fires on four positions
is not a small finding, it is noise with a recommendation attached.

`collect` reads the database. `proposals`, `observations` and `render` are pure, which is what
makes the judgement in them testable without a run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import (DEFAULT_MAX_FEE_FRAC, DEFAULT_MAX_HOLD_S, DEFAULT_MAX_SPREAD_FRAC,
                      LEARNED_DOC, MIN_ARCHETYPE_POSITIONS, MIN_CAPTURE_RATIO,
                      POLL_INTERVAL_S, SKIP_DOMINANCE_FRAC)
from ..db import store
from . import strategy as strategy_mod

# Skip reasons that say something about liquidity rather than about the trader.
LIQUIDITY_SKIPS = ("wide_spread", "thin_book", "no_book")


@dataclass
class Findings:
    taken_at: int
    n_runs: int = 0
    first_run_ts: int | None = None
    last_run_ts: int | None = None
    archetypes: dict = field(default_factory=dict)
    skips: list = field(default_factory=list)      # [(reason, n)] over everything, worst first
    exits: list = field(default_factory=list)      # [(reason, n, pnl)]
    latency: dict = field(default_factory=dict)    # n, p50, p90, feed, loop
    totals: dict = field(default_factory=dict)

    @property
    def n_skips(self) -> int:
        return sum(n for _, n in self.skips)

    def skip_share(self, reason: str) -> float:
        total = self.n_skips
        return next((n for r, n in self.skips if r == reason), 0) / total if total else 0.0


@dataclass
class Proposal:
    scope: str          # 'task' | 'config' | 'roster'
    knob: str
    current: str
    proposed: str
    evidence: str
    n: int
    confidence: str     # 'low' | 'medium' | 'high'

    def as_line(self) -> str:
        return (f"**{self.knob}**  `{self.current}` -> `{self.proposed}`  "
                f"_({self.scope}, confidence {self.confidence}, n={self.n})_\n"
                f"  {self.evidence}")


def _confidence(n: int, floor: int) -> str:
    if n >= floor * 3:
        return "high"
    if n >= floor:
        return "medium"
    return "low"


# --- collection -----------------------------------------------------------------------------


def collect(con, *, task: str | None = None, since: int | None = None) -> Findings:
    """Every aggregate the proposal rules read, in one pass over the database.

    Scoped to a task when given, because two tasks copying different rosters have no business
    pooling their skip histograms.
    """
    f = Findings(taken_at=int(time.time()))
    runs = [r for r in store.all_runs(con, task)
            if since is None or (r["started_at"] or 0) >= since]
    f.n_runs = len(runs)
    if runs:
        f.first_run_ts = runs[0]["started_at"]
        f.last_run_ts = runs[-1]["started_at"]

    wallets = store.archetype_wallets(con)
    pnl = store.archetype_pnl(con)
    signals = store.archetype_signals(con)
    exits = store.archetype_exits(con)

    for name in set(wallets) | set(pnl) | set(signals) | set(exits):
        w, p, g, x = (wallets.get(name, {}), pnl.get(name, {}), signals.get(name, {}),
                      exits.get(name, []))
        f.archetypes[name] = {
            "n_traders": w.get("n_traders", 0),
            "median_rank_score": w.get("median_rank_score"),
            "median_capture_ratio": w.get("median_capture_ratio"),
            "n_positions": p.get("n_positions", 0),
            "closed": p.get("closed", 0),
            "open": p.get("open", 0),
            "realized_pnl": p.get("realized_pnl", 0.0),
            "fees": p.get("fees", 0.0),
            "win_rate": p.get("win_rate"),
            "seen": g.get("seen", 0),
            "copied": g.get("copied", 0),
            "skipped": g.get("skipped", 0),
            "skips": g.get("skips", []),
            "exits": x,
        }

    skips: dict[str, int] = {}
    for d in f.archetypes.values():
        for reason, n in d["skips"]:
            skips[reason] = skips.get(reason, 0) + n
    f.skips = sorted(skips.items(), key=lambda kv: -kv[1])

    ex: dict[str, list] = {}
    for d in f.archetypes.values():
        for reason, n, p in d["exits"]:
            cur = ex.setdefault(reason, [0, 0.0])
            cur[0] += n
            cur[1] += p
    f.exits = sorted(((r, v[0], v[1]) for r, v in ex.items()), key=lambda t: -t[1])

    f.latency = _latency(con, runs)
    f.totals = {
        "signals": sum(d["seen"] for d in f.archetypes.values()),
        "copied": sum(d["copied"] for d in f.archetypes.values()),
        "positions": sum(d["n_positions"] for d in f.archetypes.values()),
        "realized_pnl": sum(d["realized_pnl"] for d in f.archetypes.values()),
        "fees": sum(d["fees"] for d in f.archetypes.values()),
    }
    return f


def _latency(con, runs) -> dict:
    """Copy latency pooled over runs, split into the feed's half and ours.

    Pooled by weighting each run's average by its signal count rather than averaging the
    averages, which would let a three-signal run outvote a three-hundred-signal one.
    """
    stats = store.latency_stats(con)
    n_total, feed, loop = 0, 0.0, 0.0
    for r in runs:
        sp = store.latency_split(con, r["id"])
        if sp.get("n"):
            n_total += sp["n"]
            feed += sp["feed"] * sp["n"]
            loop += sp["loop"] * sp["n"]
    if n_total:
        stats = {**stats, "feed": feed / n_total, "loop": loop / n_total, "split_n": n_total}
    return stats


# --- proposal rules -------------------------------------------------------------------------
# One function per rule so each one gets its own test. Every rule returns a list, because the
# honest answer to most of them most of the time is an empty one.


def _dominant(f: Findings, reason: str) -> bool:
    return f.skip_share(reason) >= SKIP_DOMINANCE_FRAC


def _worst_archetype_for(f: Findings, reason: str) -> tuple[str | None, int]:
    """Which archetype a skip reason concentrates in, and how many of them it accounts for."""
    best, best_n = None, 0
    for name, d in f.archetypes.items():
        n = next((n for r, n in d["skips"] if r == reason), 0)
        if n > best_n:
            best, best_n = name, n
    return best, best_n


def fee_floor_rule(f: Findings, t=None) -> list[Proposal]:
    """`fee_floor` dominating is a verdict on the market, not on the trader.

    A round trip costs `2·rate·min(p,1-p)/p` of the stake -- 10% at even odds -- so when this
    reason dominates, the entries being copied sit in the middle of the price band where the fee
    is largest. Narrowing the band is the change that acts on the cause; lowering `max_fee_frac`
    is the same change stated as a ceiling.
    """
    if not _dominant(f, "fee_floor"):
        return []
    n = next((n for r, n in f.skips if r == "fee_floor"), 0)
    where, where_n = _worst_archetype_for(f, "fee_floor")
    current = getattr(t, "max_fee_frac", DEFAULT_MAX_FEE_FRAC)
    detail = f" It concentrates in {where} ({where_n} of them)." if where else ""
    return [Proposal(
        scope="task", knob="max_fee_frac", current=f"{current}",
        proposed=f"{round(current * 0.75, 3)}",
        evidence=(f"fee_floor is {f.skip_share('fee_floor'):.0%} of all skips ({n}). The round "
                  f"trip costs more than the exit rule can win back at the prices being copied, "
                  f"so refusing earlier costs nothing that was ever winnable.{detail}"),
        n=n, confidence=_confidence(n, 20))]


def stale_rule(f: Findings, t=None) -> list[Proposal]:
    """`stale` dominating means we saw the fill too late -- but not necessarily that we polled
    too slowly.

    This is the rule most likely to reach a confidently wrong conclusion, so it is guarded
    explicitly. `signals` records `trader_ts`, `fetch_ts` and `seen_ts` precisely so the delay
    can be split into the feed's half and ours. If data-api's cache was already stale when it
    answered, a shorter poll interval buys nothing but rate-limit risk -- the same verdict
    `report.latency_block` prints. In that case this rule proposes nothing at all, and says why.
    """
    if not _dominant(f, "stale"):
        return []
    n = next((n for r, n in f.skips if r == "stale"), 0)
    feed, loop = f.latency.get("feed"), f.latency.get("loop")
    if feed is not None and loop is not None and feed > max(loop * 2, 2):
        return []          # handled as an observation; see `observations`
    current = getattr(t, "poll_interval_s", POLL_INTERVAL_S)
    return [Proposal(
        scope="task", knob="poll_interval_s", current=f"{current}",
        proposed=f"{round(current / 2, 1)}",
        evidence=(f"stale is {f.skip_share('stale'):.0%} of all skips ({n}), and the latency "
                  f"split attributes the delay to this loop rather than to the feed "
                  f"(loop {loop:.1f}s vs feed {feed:.1f}s). Halving the interval is the change "
                  f"that acts on the half we control."
                  if feed is not None else
                  f"stale is {f.skip_share('stale'):.0%} of all skips ({n}). No latency split "
                  f"was recorded, so this proposal rests on the skip count alone."),
        n=n, confidence=_confidence(n, 20))]


def liquidity_rule(f: Findings, t=None) -> list[Proposal]:
    """Wide spreads and thin books mean their entries sit where the book is not.

    That is not fixed by trading more carefully in those markets; it is fixed by not shortlisting
    the wallets that trade in them. So the proposal names the archetype, not only the knob.
    """
    n = sum(next((k for r, k in f.skips if r == reason), 0) for reason in LIQUIDITY_SKIPS)
    total = f.n_skips
    if not total or n / total < SKIP_DOMINANCE_FRAC:
        return []
    where, where_n = _worst_archetype_for(f, "wide_spread")
    current = getattr(t, "max_spread_frac", DEFAULT_MAX_SPREAD_FRAC)
    out = [Proposal(
        scope="task", knob="max_spread_frac", current=f"{current}",
        proposed=f"{round(current * 0.75, 3)}",
        evidence=(f"{n} skips ({n / total:.0%}) are wide_spread, thin_book or no_book. The "
                  f"spread is a cost paid before the trade is right about anything, and a book "
                  f"this wide is not one a copier can flip in."),
        n=n, confidence=_confidence(n, 20))]
    if where and where_n >= MIN_ARCHETYPE_POSITIONS:
        out.append(Proposal(
            scope="roster", knob=f"stop shortlisting {where}",
            current="copied", proposed="excluded",
            evidence=(f"{where_n} of the wide_spread skips come from {where} wallets: "
                      f"{strategy_mod.describe(where)}."),
            n=where_n, confidence=_confidence(where_n, MIN_ARCHETYPE_POSITIONS)))
    return out


def max_hold_rule(f: Findings, t=None) -> list[Proposal]:
    """`max_hold` dominating the exit mix means the time stop, not the trader, is closing trades.

    That is a statement that their edge is slower than the task assumes. It is proposed per
    archetype rather than globally because a scalper and a resolution-holder need opposite
    answers and a single number serves neither.
    """
    out = []
    current = getattr(t, "max_hold_s", DEFAULT_MAX_HOLD_S)
    for name, d in f.archetypes.items():
        closed = sum(n for _, n, _ in d["exits"])
        if closed < MIN_ARCHETYPE_POSITIONS:
            continue
        n = sum(n for r, n, _ in d["exits"] if r == "max_hold")
        if n / closed < 0.5:
            continue
        out.append(Proposal(
            scope="task", knob=f"max_hold_s (for {name} rosters)", current=f"{current}",
            proposed=f"{current * 2}",
            evidence=(f"{n} of {closed} {name} positions ({n / closed:.0%}) were closed by the "
                      f"time stop rather than by the trader or the ladder. Their edge is slower "
                      f"than {current}s assumes, so the stop is cutting trades that had not "
                      f"finished."),
            n=closed, confidence=_confidence(closed, MIN_ARCHETYPE_POSITIONS)))
    return out


def archetype_pnl_rule(f: Findings, t=None) -> list[Proposal]:
    """An archetype that loses money over enough positions should stop being shortlisted.

    Scoped to the roster, not to a risk limit: the answer to "this kind of trader does not pay"
    is to copy a different kind, not to trade this one with a tighter stop.
    """
    out = []
    for name, d in sorted(f.archetypes.items(), key=lambda kv: kv[1]["realized_pnl"]):
        if name in ("unknown", strategy_mod.UNCLASSIFIED):
            continue
        closed = d["closed"]
        if closed < MIN_ARCHETYPE_POSITIONS or d["realized_pnl"] >= 0:
            continue
        per = d["realized_pnl"] / closed
        out.append(Proposal(
            scope="roster", knob=f"drop {name} from future rosters",
            current="copied", proposed="excluded",
            evidence=(f"{closed} closed {name} positions returned ${d['realized_pnl']:,.2f} "
                      f"(${per:,.2f} each) after ${d['fees']:,.2f} of fees. "
                      f"{strategy_mod.describe(name)}"),
            n=closed, confidence=_confidence(closed, MIN_ARCHETYPE_POSITIONS)))
    return out


def capture_rule(f: Findings, t=None) -> list[Proposal]:
    """An archetype whose median capture ratio is under the floor is structurally uncopyable.

    `STRATEGY.md` §6 says this about individual wallets -- a great ROI with a 30-second edge
    half-life is not a candidate. This is the same sentence one level up: when the median wallet
    of a whole shape fails that test, the shape is the problem, and a better wallet of the same
    shape will fail it too.
    """
    out = []
    for name, d in f.archetypes.items():
        cap = d["median_capture_ratio"]
        if cap is None or cap >= MIN_CAPTURE_RATIO or d["n_traders"] < 3:
            continue
        out.append(Proposal(
            scope="roster", knob=f"stop shortlisting {name}",
            current="candidate", proposed="excluded",
            evidence=(f"median capture ratio {cap:.2f} across {d['n_traders']} {name} wallets, "
                      f"below the {MIN_CAPTURE_RATIO} floor -- their edge does not survive being "
                      f"copied, however good the wallets themselves are."),
            n=d["n_traders"], confidence=_confidence(d["n_traders"], 5)))
    return out


RULES = (fee_floor_rule, stale_rule, liquidity_rule, max_hold_rule, archetype_pnl_rule,
         capture_rule)


def proposals(f: Findings, task=None) -> list[Proposal]:
    """Every rule's output, most-evidenced first. Pure."""
    out: list[Proposal] = []
    for rule in RULES:
        out.extend(rule(f, task))
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda p: (order[p.confidence], -p.n))


def observations(f: Findings, task=None) -> list[str]:
    """What was seen but is not evidence enough to propose anything on. Pure.

    This list exists so that a thin signal is reported as thin rather than either inflated into a
    recommendation or dropped silently -- the habit `skill.py` follows when it reports a metric
    as missing instead of as a flattering default.
    """
    out = []
    feed, loop = f.latency.get("feed"), f.latency.get("loop")
    if _dominant(f, "stale") and feed is not None and loop is not None and feed > max(loop * 2, 2):
        out.append(
            f"`stale` is {f.skip_share('stale'):.0%} of all skips, but the delay is the feed's: "
            f"{feed:.1f}s of it arrives before we see the response and only {loop:.1f}s is this "
            f"loop. A shorter poll interval cannot recover time that was already spent, so no "
            f"cadence change is proposed. The copyable conclusion is about the traders -- these "
            f"wallets are too fast to copy at any interval this feed supports.")
    for name, d in sorted(f.archetypes.items()):
        if name == "unknown":
            continue
        if 0 < d["closed"] < MIN_ARCHETYPE_POSITIONS:
            out.append(
                f"`{name}`: {d['closed']} closed position(s) returning "
                f"${d['realized_pnl']:,.2f}. Under the {MIN_ARCHETYPE_POSITIONS}-position floor, "
                f"so this is recorded and not acted on.")
        elif d["n_traders"] and not d["n_positions"]:
            out.append(f"`{name}`: {d['n_traders']} wallet(s) profiled, none copied yet.")
    if not f.n_runs:
        out.append("No runs recorded yet. Everything above describes wallets, not outcomes.")
    return out


# --- snapshots and rendering ----------------------------------------------------------------


def snapshot_rows(f: Findings) -> list[dict]:
    rows = []
    for name, d in sorted(f.archetypes.items()):
        rows.append({
            "taken_at": f.taken_at, "archetype": name,
            "n_traders": d["n_traders"], "n_positions": d["n_positions"],
            "n_signals": d["seen"], "n_copied": d["copied"],
            "realized_pnl": d["realized_pnl"], "fees": d["fees"], "win_rate": d["win_rate"],
            "median_capture_ratio": d["median_capture_ratio"],
            "median_rank_score": d["median_rank_score"],
            "top_skip_reason": d["skips"][0][0] if d["skips"] else None,
            "top_exit_reason": d["exits"][0][0] if d["exits"] else None,
        })
    return rows


def _dt(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _num(x, fmt="{:.2f}") -> str:
    return "-" if x is None else fmt.format(x)


def render(f: Findings, props: list[Proposal], obs: list[str], *,
           task: str | None = None, snapshots: list | None = None) -> str:
    """The generated document. Pure: findings in, markdown out."""
    scope = f"task `{task}`" if task else "all tasks"
    lines = [
        "# STRATEGY (learned)",
        "",
        "**This file is generated. Do not edit it — `polywatch strategy learn` overwrites it "
        "whole.**",
        "",
        f"Generated {_dt(f.taken_at)} UTC over {scope}: {f.n_runs} run(s) between "
        f"{_dt(f.first_run_ts)} and {_dt(f.last_run_ts)}.",
        "",
        "`STRATEGY.md` is the hand-written thesis and nothing here edits it. This document is the "
        "evidence accumulating underneath it: what kinds of trader have been copied, what that "
        "cost, and what the numbers suggest changing. **Nothing below has been applied.** A "
        "proposal is a suggestion with its sample size attached, and `RULES.md` I8 is what keeps "
        "it one.",
        "",
        "---",
        "",
        "## Observed archetypes",
        "",
    ]
    if f.archetypes:
        lines += ["| archetype | wallets | median rank | median capture | what it means for a "
                  "copier |", "|---|---|---|---|---|"]
        for name, d in sorted(f.archetypes.items(), key=lambda kv: -kv[1]["n_traders"]):
            if not d["n_traders"]:
                continue
            lines.append(f"| `{name}` | {d['n_traders']} | "
                         f"{_num(d['median_rank_score'])} | {_num(d['median_capture_ratio'])} | "
                         f"{strategy_mod.describe(name)} |")
    else:
        lines.append("_No wallets profiled yet. Run `polywatch discover`._")

    lines += ["", "## Copy performance by archetype", "",
              "Positions are attributed to the archetype of the trader whose signal opened them. "
              "Fees are counted separately from PnL because the fee is the part no exit rule can "
              "win back.", ""]
    copied = {k: v for k, v in f.archetypes.items() if v["n_positions"]}
    if copied:
        lines += ["| archetype | positions | closed | open | realized pnl | fees | win rate | "
                  "top exit |", "|---|---|---|---|---|---|---|---|"]
        for name, d in sorted(copied.items(), key=lambda kv: -kv[1]["realized_pnl"]):
            top_exit = d["exits"][0][0] if d["exits"] else "-"
            wr = "-" if d["win_rate"] is None else f"{d['win_rate']:.0%}"
            lines.append(f"| `{name}` | {d['n_positions']} | {d['closed']} | {d['open']} | "
                         f"${d['realized_pnl']:,.2f} | ${d['fees']:,.2f} | {wr} | {top_exit} |")
    else:
        lines.append("_No positions copied yet._")

    lines += ["", "## Where copies are refused", "",
              "The skip histogram is the main finding of a paper run. Split by archetype it stops "
              "being a verdict on the market and becomes one about which kind of wallet to stop "
              "shortlisting.", ""]
    if f.skips:
        lines += ["| reason | total | concentrated in |", "|---|---|---|"]
        for reason, n in f.skips:
            where, where_n = _worst_archetype_for(f, reason)
            place = f"`{where}` ({where_n})" if where else "-"
            lines.append(f"| `{reason}` | {n} ({f.skip_share(reason):.0%}) | {place} |")
    else:
        lines.append("_No skips recorded._")

    if f.latency.get("n"):
        lines += ["", "## Latency", "",
                  f"{f.latency['n']} signals, median {f.latency.get('p50', 0)}s behind the "
                  f"trader, p90 {f.latency.get('p90', 0)}s."]
        if "feed" in f.latency:
            lines.append(f"Split: {f.latency['feed']:.1f}s is the feed answering with stale data, "
                         f"{f.latency['loop']:.1f}s is this loop. Only the second half is ours to "
                         f"fix.")

    if snapshots:
        lines += ["", "## Trend", "",
                  "Each `strategy learn` appends a row per archetype, so drift is visible rather "
                  "than overwritten.", "",
                  "| taken | archetype | positions | realized pnl | win rate |", "|---|---|---|---|---|"]
        for r in snapshots[-40:]:
            wr = "-" if r["win_rate"] is None else f"{r['win_rate']:.0%}"
            lines.append(f"| {_dt(r['taken_at'])} | `{r['archetype']}` | {r['n_positions']} | "
                         f"${(r['realized_pnl'] or 0):,.2f} | {wr} |")

    lines += ["", "## Proposed changes (UNAPPLIED)", ""]
    if props:
        lines.append("Ranked by how much evidence stands behind them. None of these has been "
                     "applied to any task, knob or roster; applying one is a human decision and "
                     "belongs in the same commit as an update to `STRATEGY.md` or `RULES.md`.")
        lines.append("")
        for i, p in enumerate(props, 1):
            lines.append(f"{i}. {p.as_line()}")
            lines.append("")
    else:
        lines.append("_Nothing has enough evidence behind it to propose. That is the expected "
                     "state early on and is not a failure._")

    lines += ["", "## Observations (below the sample floor)", ""]
    lines += [f"- {o}" for o in obs] if obs else ["_None._"]

    lines += ["", "## What would falsify this", "",
              "Carried from `STRATEGY.md` §10, restated per archetype so a run can settle them:",
              "",
              "- An archetype whose skips are mostly `stale` is too fast to copy at this cadence, "
              "whatever its rank score says.",
              "- An archetype whose skips are mostly `fee_floor` trades where the round trip "
              "costs more than the exit rule can win back — a verdict on the market it chose.",
              "- An archetype whose exits are mostly `max_hold` has an edge slower than the task "
              "assumes.",
              "- An archetype whose median capture ratio sits under "
              f"{MIN_CAPTURE_RATIO} at one poll interval has nothing in it for a copier, however "
              "good the individual wallets are.",
              ""]
    return "\n".join(lines)


def learn(con, *, task: str | None = None, since: int | None = None, write: bool = True,
          path=None) -> tuple[Findings, list[Proposal], list[str]]:
    """Collect, propose, snapshot, and regenerate the document.

    `write=False` is `strategy show`: the same numbers, no snapshot row and no file touched.
    """
    f = collect(con, task=task, since=since)
    t = None
    if task:
        row = store.get_task(con, task)
        if row is not None:
            from .task import Task
            t = Task.from_row(row)
    props = proposals(f, t)
    obs = observations(f, t)
    if write:
        store.insert_strategy_snapshot(con, snapshot_rows(f))
        doc = render(f, props, obs, task=task, snapshots=store.strategy_snapshots(con))
        target = path or LEARNED_DOC
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(doc)
    return f, props, obs


def format_findings(f: Findings, props: list[Proposal], obs: list[str]) -> str:
    """Terminal rendering for `strategy show`, which is the same content without the file."""
    out = [f"  {f.n_runs} run(s), {f.totals.get('signals', 0)} signals, "
           f"{f.totals.get('copied', 0)} copied, {f.totals.get('positions', 0)} positions, "
           f"${f.totals.get('realized_pnl', 0):,.2f} realized on "
           f"${f.totals.get('fees', 0):,.2f} of fees", ""]
    if f.archetypes:
        out.append(f"  {'archetype':20} {'wallets':>7} {'pos':>5} {'pnl':>10} {'top skip':>16}")
        out.append("  " + "-" * 62)
        for name, d in sorted(f.archetypes.items(), key=lambda kv: -kv[1]["realized_pnl"]):
            skip = d["skips"][0][0] if d["skips"] else "-"
            out.append(f"  {name:20} {d['n_traders']:>7} {d['n_positions']:>5} "
                       f"{d['realized_pnl']:>10,.2f} {skip:>16}")
    out += ["", f"  {len(props)} proposal(s), none applied:"]
    for p in props:
        out.append(f"    [{p.confidence:>6}, n={p.n}] {p.knob}: {p.current} -> {p.proposed}")
        out.append(f"             {p.evidence}")
    if obs:
        out.append("")
        out.append("  observations:")
        out += [f"    - {o}" for o in obs]
    return "\n".join(out)
