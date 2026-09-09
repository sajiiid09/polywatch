"""The handoff between one operator of the account and the next.

A run inherits nothing from its predecessor, and that is deliberate. `Engine.start` closes any
run left open as `abandoned` rather than adopting its positions, seeds the poll watermark to
*now* rather than to a stored value, and throws away the trailing stop's high-water marks --
each with a comment saying why. Inheriting trading state silently would make the next report a
fiction.

What is right for trading state is wrong for the operator. Whoever picks the account up next --
a person, or a stateless agent session that has never seen this conversation -- gets a
`task_runs` row and has to reconstruct everything else: what was held, what was left resting on a
real exchange, which trader was dropped and why, which refusal dominated the histogram, and what
to do about any of it.

So a session ends by writing two things:

  * a `sessions` row, which is machine state and lives in the database because `RULES.md` I1 says
    a finished run must be explicable from there alone;
  * an entry at the top of `docs/PROGRESS.md`, which is the briefing, in prose, for whoever is
    next.

And `polywatch session open` **prints that briefing and does nothing else**. It is not a resume.
Nothing here restores a watermark, re-opens a position or re-arms a stop. The handoff is
advisory by construction, which is what lets it exist alongside the engine's refusal to inherit.

`next_steps` and `briefing` are pure so the judgement in them is testable without a run.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import PROGRESS_DOC
from ..db import store
from . import learn as learn_mod
from . import report as report_mod

PREAMBLE = """# PROGRESS

The account's log, newest first. One entry per session, written by `polywatch session close`.

AI agents are stateless and human operators forget, so this file is how the account stays one
continuous operator across sessions that share nothing else. Read the top entry before doing
anything; it says what is held, what is still live on the exchange, and what was left undone.

It is generated. Add a human note with `polywatch session close <task> --note "..."` rather than
editing an entry: the same text is stored in the `sessions` table, and only one of the two copies
gets edited by hand.

**Nothing in here is applied automatically.** The next steps are instructions for an operator,
not a queue the bot drains.

"""

# Which falsification in STRATEGY.md §10 a dominant skip reason settles. The mapping is here
# rather than in the document so that a run's own histogram points at the sentence it answers.
FALSIFIES = {
    "stale": ("the traders on this roster are too fast to copy at this cadence -- and if the "
              "latency split blames the feed, at any cadence"),
    "fee_floor": ("the round trip costs more than the exit rule can win back at the prices being "
                  "copied. That is a verdict on the markets they choose, not on them"),
    "wide_spread": "their entries sit where the book is not",
    "thin_book": "their entries sit where the book is not",
    "no_book": "the markets being copied had no readable book when the signal arrived",
    "max_market_usd": "the per-market cap is the binding constraint, not the trader",
    "insufficient_cash": "the bankroll is fully deployed; the cap or the stake is mis-sized",
    "holds_other_outcome": ("the roster is copying both sides of the same market -- the gate is "
                            "working, but the roster is fighting itself"),
}


@dataclass
class Handoff:
    task: str
    run_id: int | None
    state: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    note: str = ""


# --- reading the account --------------------------------------------------------------------


def account_state(con, task: str, run_id: int | None = None) -> dict:
    """Everything the briefing quotes, composed from readers that already exist.

    Deliberately reads the *database* rather than an engine, so it works identically after a
    clean stop, after a crash, and hours later from a different process.
    """
    run = (store.get_run(con, run_id) if run_id is not None
           else store.last_run(con, task))
    state: dict = {"task": task, "run_id": run["id"] if run else None,
                   "mode": run["mode"] if run else None}
    if run is None:
        state.update({"summary": {}, "open_positions": [], "skips": [], "exits": [],
                      "traders": [], "dropped": [], "resting": [], "orphans": [],
                      "latency": {}, "archetypes": {}, "n_proposals": 0})
        return state

    rid = run["id"]
    summary = store.run_summary(con, rid)
    state.update({
        "started_at": run["started_at"],
        "stopped_at": run["stopped_at"],
        "stop_reason": run["stop_reason"],
        "start_bankroll": run["start_bankroll"],
        "end_bankroll": run["end_bankroll"],
        "cash": summary.get("cash"),
        "summary": summary,
        "open_positions": [dict(r) for r in store.open_positions(con, rid)],
        "skips": store.skip_reasons(con, rid),
        "exits": report_mod.exit_mix(con, rid),
        "traders": [dict(r) for r in store.task_traders(con, task)],
        "resting": [dict(r) for r in store.open_resting(con, rid)],
        "orphans": [dict(r) for r in store.orphan_resting(con, "live")],
        "latency": {**store.latency_stats(con, rid), **store.latency_split(con, rid)},
        "archetypes": store.archetype_pnl(con, rid),
        "trader_pnl": store.trader_pnl(con, rid),
    })
    state["dropped"] = [t for t in state["traders"] if not t["active"]]
    # A standing pointer, not a recomputation of the whole learning pass: the next operator needs
    # to know unapplied proposals exist, and `strategy show` is where they read them.
    findings = learn_mod.collect(con, task=task)
    state["n_proposals"] = len(learn_mod.proposals(findings))
    return state


# --- judgement (pure) -----------------------------------------------------------------------


def _top(pairs) -> tuple[str | None, int]:
    return (pairs[0][0], pairs[0][1]) if pairs else (None, 0)


def warnings(state: dict) -> list[str]:
    """What is unsafe or unattended right now. These come first in the briefing."""
    out = []
    n_open = len(state.get("open_positions") or [])
    if n_open:
        out.append(
            f"{n_open} position(s) are still open and NO STOP-LOSS IS RUNNING ON THEM. The CLOB "
            f"has no stop order type, so a stop is a price this process watches; while nothing "
            f"is running, nothing is watching. Either start a run to manage them or close them "
            f"by hand.")
    live_resting = [r for r in (state.get("resting") or []) if r.get("mode") == "live"]
    if live_resting:
        out.append(
            f"{len(live_resting)} live GTC sell order(s) are still resting on the exchange. They "
            f"will fill whether or not anything is running, which is the feature working -- but "
            f"the next run must be told rather than discovering the shares are gone.")
    orphans = [r for r in (state.get("orphans") or [])
               if r.get("run_id") != state.get("run_id")]
    if orphans:
        out.append(f"{len(orphans)} live resting order(s) survive from earlier runs. "
                   f"`polywatch task orders --cancel` clears them.")
    if state.get("stop_reason") == "circuit_breaker":
        out.append("This run was stopped by a circuit breaker, not by the clock. The loss limit "
                   "or the drawdown limit was reached; the next run starts from the bankroll "
                   "figure below, not from the original one.")
    if state.get("stop_reason") == "reconcile_failed":
        out.append("This run refused to trade: the exchange's view of our positions disagreed "
                   "with the database. Resolve that before starting another run -- trading on a "
                   "book we cannot verify is what RULES.md L5 exists to prevent.")
    return out


def next_steps(state: dict) -> list[str]:
    """What the next operator should actually do, in order. Pure.

    Rule-driven rather than free-form so that the same situation produces the same instruction
    every time -- an agent reading this file is entitled to a consistent operator, not a
    differently-worded one each session.
    """
    steps = []
    if len(state.get("open_positions") or []):
        steps.append("Decide about the open positions first: run the task to resume managing "
                     "them, or flatten them by hand. Nothing is watching them meanwhile.")
    if [r for r in (state.get("resting") or []) if r.get("mode") == "live"]:
        steps.append("Review the resting sell orders with `polywatch task orders`; cancel with "
                     "`--cancel` if the thesis behind them has expired.")
    for t in state.get("dropped") or []:
        steps.append(f"Trader {t['address'][:12]} was auto-dropped ({t['dropped_reason']}). The "
                     f"drop persists in `task_traders`; re-add them deliberately or leave them "
                     f"out, but do not assume the next run reconsiders it.")
    reason, n = _top(state.get("skips") or [])
    if reason and reason in FALSIFIES:
        steps.append(f"`{reason}` was the top refusal ({n}). STRATEGY.md §10: {FALSIFIES[reason]}. "
                     f"If it dominates again next session, act on it rather than logging it "
                     f"twice.")
    exits = state.get("exits") or []
    if exits and exits[0][0] == "max_hold":
        steps.append("Most positions were closed by the time stop rather than by the trader or "
                     "the ladder, which says the roster's edge is slower than `max_hold_s` "
                     "assumes.")
    if state.get("n_proposals"):
        steps.append(f"{state['n_proposals']} unapplied proposal(s) are waiting in "
                     f"`docs/STRATEGY_LEARNED.md` (`polywatch strategy show`). They are "
                     f"suggestions with sample sizes, not a queue -- applying one is a decision.")
    if not steps:
        steps.append("Nothing outstanding. Run `polywatch strategy learn` to fold this session "
                     "into the record, then start the next run.")
    return steps


def _dt(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "-"


def briefing(h: Handoff) -> str:
    """The markdown entry, and the same text stored on the `sessions` row. Pure."""
    s = h.state
    sm = s.get("summary") or {}
    equity = s.get("end_bankroll")
    if equity is None:
        equity = s.get("cash")
    start = s.get("start_bankroll")
    lines = [f"## {_dt(s.get('stopped_at') or int(time.time()))} UTC — task `{h.task}`"
             f"{' (run ' + str(h.run_id) + ')' if h.run_id else ''}", ""]
    if h.run_id is None:
        lines += ["No run recorded for this task. Nothing was traded and nothing is held.", ""]
    else:
        delta = (equity - start) if (equity is not None and start is not None) else None
        lines += [
            f"- **mode** {s.get('mode')} · **stopped** {s.get('stop_reason') or '-'} · "
            f"**ran** {_dt(s.get('started_at'))} → {_dt(s.get('stopped_at'))}",
            f"- **equity** ${start:,.2f} → ${equity:,.2f}"
            f"{f' ({delta:+,.2f})' if delta is not None else ''}"
            if (start is not None and equity is not None) else "- **equity** unknown",
            f"- **realized** ${sm.get('realized_pnl', 0):,.2f} after "
            f"${sm.get('fees_paid', 0):,.2f} of fees",
            f"- **signals** {sm.get('signals_seen', 0)} seen, {sm.get('copied', 0)} copied, "
            f"{sm.get('skipped', 0)} skipped",
            f"- **positions** {sm.get('positions_closed', 0)} closed, "
            f"{sm.get('positions_open', 0)} open",
        ]
        lat = s.get("latency") or {}
        if lat.get("n"):
            split = (f", feed {lat['feed']:.1f}s vs loop {lat['loop']:.1f}s"
                     if "feed" in lat else "")
            lines.append(f"- **latency** median {lat.get('p50', 0)}s behind the trader{split}")
        lines.append("")

    if h.warnings:
        lines.append("**Attention**")
        lines += [f"- {w}" for w in h.warnings]
        lines.append("")

    skips = s.get("skips") or []
    if skips:
        lines.append("**Why copies were refused** — "
                     + ", ".join(f"`{r}` {n}" for r, n in skips[:6]))
        lines.append("")
    exits = s.get("exits") or []
    if exits:
        lines.append("**How positions closed** — "
                     + ", ".join(f"`{r}` {n} (${p:,.2f})" for r, n, p in exits[:6]))
        lines.append("")
    arche = {k: v for k, v in (s.get("archetypes") or {}).items() if v.get("n_positions")}
    if arche:
        lines.append("**By trader archetype** — "
                     + ", ".join(f"`{k}` {v['n_positions']} pos ${v['realized_pnl']:,.2f}"
                                 for k, v in sorted(arche.items(),
                                                    key=lambda kv: -kv[1]["realized_pnl"])))
        lines.append("")
    tp = s.get("trader_pnl") or {}
    if len(tp) > 1:
        lines.append("**By trader** — "
                     + ", ".join(f"`{a[:10]}` ${d.get('realized_pnl', 0):,.2f}"
                                 for a, d in sorted(tp.items(),
                                                    key=lambda kv: -kv[1].get("realized_pnl", 0))))
        lines.append("")
    if h.note:
        lines += [f"**Note** — {h.note}", ""]

    lines.append("**Next steps**")
    lines += [f"{i}. {t}" for i, t in enumerate(h.steps, 1)]
    lines.append("")
    return "\n".join(lines)


# --- writing --------------------------------------------------------------------------------


def append_progress(path, h: Handoff, text: str | None = None) -> None:
    """Prepend the entry, so the top of the file is always the current state.

    Newest first because the reader is usually an operator with a budget -- a person skimming or
    an agent with a context window. Chronological order would put the one entry that matters at
    the bottom of a file that only grows.
    """
    entry = text if text is not None else briefing(h)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    if not existing:
        path.write_text(PREAMBLE + entry)
        return
    idx = existing.find("\n## ")
    if idx == -1:
        path.write_text(existing.rstrip() + "\n\n" + entry)
        return
    head, rest = existing[:idx + 1], existing[idx + 1:]
    path.write_text(head + entry + "\n" + rest)


def close(con, task: str, run_id: int | None = None, *, note: str = "",
          operator: str = "agent", path=None, write_doc: bool = True) -> Handoff:
    """End a session: record the state, derive the instructions, hand both over.

    Called at the end of `task run` and available on its own, because the run that ended badly is
    exactly the one whose handoff matters and it is also the one least likely to have reached the
    end of `task run` cleanly.
    """
    state = account_state(con, task, run_id)
    h = Handoff(task=task, run_id=state.get("run_id"), state=state, note=note)
    h.warnings = warnings(state)
    h.steps = next_steps(state)
    text = briefing(h)

    sm = state.get("summary") or {}
    skip_reason, _ = _top(state.get("skips") or [])
    exits = state.get("exits") or []
    store.insert_session(con, {
        "task": task,
        "run_id": h.run_id,
        "mode": state.get("mode"),
        "operator": operator,
        "opened_at": state.get("started_at"),
        "closed_at": state.get("stopped_at") or int(time.time()),
        "start_equity": state.get("start_bankroll"),
        "end_equity": state.get("end_bankroll") if state.get("end_bankroll") is not None
        else state.get("cash"),
        "realized_pnl": sm.get("realized_pnl"),
        "fees": sm.get("fees_paid"),
        "signals_seen": sm.get("signals_seen"),
        "copied": sm.get("copied"),
        "skipped": sm.get("skipped"),
        "positions_open": sm.get("positions_open"),
        "positions_closed": sm.get("positions_closed"),
        "top_skip_reason": skip_reason,
        "top_exit_reason": exits[0][0] if exits else None,
        "stop_reason": state.get("stop_reason"),
        "resting_open": len(state.get("resting") or []),
        "dropped_traders": ",".join(t["address"] for t in state.get("dropped") or []) or None,
        "briefing_md": text,
        "next_steps_json": json.dumps(h.steps),
        "note": note or None,
    })
    if write_doc:
        append_progress(path or PROGRESS_DOC, h, text)
    return h


# --- reading back ---------------------------------------------------------------------------


def format_open(con, task: str) -> str:
    """What `polywatch session open` prints.

    A briefing, never a resume. The reminder at the bottom is there because the natural
    expectation of a command called `open` is that it restores something, and it does not.
    """
    row = store.last_session(con, task)
    head = [f"  session briefing for task `{task}`", ""]
    if row is None:
        head += ["  No previous session recorded. Nothing is held, nothing is resting, and the",
                 "  first run starts from the task's configured bankroll.", ""]
    else:
        head.append(f"  last session closed {_dt(row['closed_at'])} UTC"
                    + (f", operator {row['operator']}" if row["operator"] else ""))
        head.append("")
        head += ["  " + ln for ln in (row["briefing_md"] or "").splitlines()]
        head.append("")
    head += ["  This is a briefing, not a resume. Starting a run inherits no positions, no poll",
             "  watermark and no trailing high-water mark from the session above -- by design.",
             "  Verify anything it says is still true before acting on it."]
    return "\n".join(head)


def format_log(rows) -> str:
    if not rows:
        return "  no sessions recorded."
    head = (f"  {'closed':16} {'task':12} {'run':>5} {'mode':6} {'equity':>10} {'pnl':>9} "
            f"{'open':>5}  stopped")
    out = [head, "  " + "-" * (len(head) - 2)]
    for r in rows:
        out.append(f"  {_dt(r['closed_at']):16} {r['task'][:12]:12} "
                   f"{(r['run_id'] or 0):>5} {(r['mode'] or '-'):6} "
                   f"{(r['end_equity'] or 0):>10,.2f} {(r['realized_pnl'] or 0):>+9,.2f} "
                   f"{(r['positions_open'] or 0):>5}  {r['stop_reason'] or '-'}")
    return "\n".join(out)
