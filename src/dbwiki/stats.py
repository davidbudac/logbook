"""`dbwiki stats` — agent cost and quality, read out of the agent-run log.

A pure reader: it opens `.state/agent_runs.jsonl` (one line per attempted
agent stage, written by `health.record_agent_run`), groups the runs by
(task, adapter, model_tier, mode) — or by model instead of tier
(`GROUP_BY`) — and reports counts, latency, cost and defect rates. A
`since`/`until` window keeps only the runs whose `at` falls inside it. No
datastore, no writes, no network.

Two rules run through everything here:

* **unknown is never estimated.** Runs whose adapter reports no price carry
  no usage; they still appear, counted in `n`, with the unknown-bearing
  aggregate reported as the string "unknown" and a `*_known_n` sample size
  beside every average.
* **no verdict without a sample.** The cheap-vs-strong block compares tiers
  only when both sides have at least MIN_SAMPLE runs; below that it says
  `insufficient sample` and shows the counts.

The ingest ledger is the *fallback* source, not the primary one. It is keyed
by digest path, so non-ingest stages (report/lint/research — essentially all
the real spend) have no row in it at all, while the agent-run log has one
line per attempted stage of every task. Ledger entries are therefore read
only for ingests that no agent-run line covers: history from before that log
existed. Agent-run lines are deduped by `event_id` (falling back to
run_id+task+db) so a folded-in analyst result (ADR-0001 `queue.fold_results`)
cannot inflate a count.
"""

import datetime as dt
import re
from collections.abc import Callable
from statistics import median

MIN_SAMPLE = 5      # runs per side below which no tier verdict is offered
UNKNOWN = "unknown"

#: the grouping a report splits its runs by: the default puts the cheap and
#: strong tiers side by side; `model` names what actually ran (Nemotron,
#: codex, sonnet), which the tier label hides.
#: `model_only` is the workbench's Agents table (incident-workbench #31),
#: which carries the tasks, adapters and tiers a model ran under as columns
#: of its own rather than splitting on them; it has no third column for
#: `format_stats`, so the CLI does not offer it.
GROUP_BY = {"tier": ("task", "adapter", "model_tier", "mode"),
            "model": ("task", "adapter", "model", "mode"),
            "model_only": ("model",)}

_DURATION = re.compile(r"(\d+)([mhdw])")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}

# statuses that mean "an agent run was attempted"; a bare trigger-decision
# skip entry has no status and is not a run
_RUN_STATUSES = ("ingested", "failed")


def parse_when(text: str, now: dt.datetime) -> dt.datetime:
    """A window bound as `dbwiki stats --since/--until` takes it: an ISO date
    or instant (a naive one is UTC), or a duration back from `now` —
    `90m`, `12h`, `7d`, `2w`. Always an aware UTC instant."""
    if m := _DURATION.fullmatch(text.strip()):
        return now - dt.timedelta(**{_UNITS[m[2]]: int(m[1])})
    t = _instant(text.strip())
    if t is None:
        raise ValueError(f"not an ISO date/time or a duration like 12h or "
                         f"7d: {text!r}")
    return t


def _instant(v) -> dt.datetime | None:
    """An ISO date or instant as an aware UTC instant (a naive one is UTC),
    or None when it is not one."""
    try:
        t = dt.datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def in_window(at, since: dt.datetime | None = None,
              until: dt.datetime | None = None) -> bool:
    """Does a record stamped `at` fall in [since, until)? With no bound at
    all everything does; with one, a record with no readable `at` does not,
    because it cannot be placed."""
    if since is None and until is None:
        return True
    t = _instant(at)
    return t is not None and (since is None or t >= since) \
        and (until is None or t < until)


def _stamp(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v):
    """The value if it is a real number, else None. Bools are not numbers."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _flag(v):
    """The value if it is a real bool, else None (absent != False)."""
    return v if isinstance(v, bool) else None


def _cost(entry: dict):
    u = entry.get("usage")
    return _num(u.get("cost_usd")) if isinstance(u, dict) else None


def _ledger_usage_known(entry: dict) -> bool:
    """The definition `record_agent_run` writes into the log: token counts
    known. Cost alone does not count — a free local model reports 0.0."""
    u = entry.get("usage")
    return isinstance(u, dict) and any(
        _num(u.get(k)) is not None for k in ("input_tokens", "output_tokens"))


def _row(*, task, adapter, model, model_tier, mode, accepted, duration_s,
         cost_usd, pages_touched, rolled_back, lint_findings, attempts,
         usage_known, digest=None) -> dict:
    """One attempted run, whichever source it came from."""
    return {"task": task or UNKNOWN, "adapter": adapter or UNKNOWN,
            "model": model or UNKNOWN, "model_tier": model_tier or UNKNOWN,
            "mode": mode or UNKNOWN, "accepted": accepted,
            "duration_s": duration_s, "cost_usd": cost_usd,
            "pages_touched": pages_touched, "rolled_back": rolled_back,
            "lint_findings": lint_findings, "attempts": attempts,
            "usage_known": usage_known, "digest": digest}


def _event_rows(events: list[dict]) -> list[dict]:
    """One row per attempted stage, deduped. `event_id` is the identity a
    folded-in analyst result carries (health.known_agent_event_ids); a line
    written before that field existed falls back to run_id+task+db."""
    seen: dict = {}
    for e in events:
        if not isinstance(e, dict):
            continue
        key = e.get("event_id") or (e.get("run_id"), e.get("task"), e.get("db"))
        if key in seen:
            continue
        seen[key] = _row(
            task=e.get("task"), adapter=e.get("adapter"), model=e.get("model"),
            model_tier=e.get("model_tier"), mode=e.get("mode"),
            # a stage counts as accepted when it validated and was kept
            accepted=bool(e.get("validation_ok")) and not e.get("rolled_back"),
            duration_s=_num(e.get("duration_s")),
            cost_usd=_num(e.get("cost_usd")),
            pages_touched=_num(e.get("pages_touched")),
            rolled_back=_flag(e.get("rolled_back")),
            lint_findings=_num(e.get("lint_findings")),
            attempts=_num(e.get("attempts")),
            usage_known=bool(e.get("usage_known")))
    return list(seen.values())


def _ledger_rows(ledger: dict, covered: set) -> list[dict]:
    """Ingests no agent-run line covers — history from before that log
    existed. Old entries keep every telemetry field as `unknown` rather than
    being dropped."""
    rows = []
    for rel, e in sorted(ledger.items()):
        if e.get("status") not in _RUN_STATUSES:
            continue
        db = rel.rsplit("/", 2)[-2] if "/" in rel else None
        if e.get("run_id") and (e.get("run_id"), e.get("task"), db) in covered:
            continue
        rows.append(_row(
            task=e.get("task"), adapter=e.get("adapter"), model=e.get("model"),
            model_tier=e.get("model_tier"), mode=e.get("mode"),
            accepted=e.get("status") == "ingested",
            duration_s=_num(e.get("duration_s")), cost_usd=_cost(e),
            pages_touched=_num(e.get("pages_touched")),
            rolled_back=_flag(e.get("rolled_back")),
            lint_findings=_num(e.get("lint_findings")),
            attempts=_num(e.get("attempts")),
            usage_known=_ledger_usage_known(e), digest=rel))
    return rows


def _rate(hits: int, known: int):
    return round(hits / known, 3) if known else UNKNOWN


def _agg(rows: list[dict]) -> dict:
    """Aggregate one group of runs. Every average names the sample it was
    computed over, so a mostly-unknown group cannot read as a measurement."""
    durations = [r["duration_s"] for r in rows if r["duration_s"] is not None]
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    pages = [r["pages_touched"] for r in rows if r["pages_touched"] is not None]
    rolled = [r["rolled_back"] for r in rows if r["rolled_back"] is not None]
    lints = [r["lint_findings"] for r in rows if r["lint_findings"] is not None]
    tries = [r["attempts"] for r in rows if r["attempts"] is not None]
    accepted = sum(1 for r in rows if r["accepted"])
    return {
        "n": len(rows),
        "accepted": accepted,
        "failed": len(rows) - accepted,
        "rollback_rate": _rate(sum(1 for v in rolled if v), len(rolled)),
        "rollback_known_n": len(rolled),
        "median_duration_s": round(median(durations), 3) if durations else UNKNOWN,
        "duration_known_n": len(durations),
        "total_cost_usd": round(sum(costs), 6) if costs else UNKNOWN,
        "median_cost_usd": round(median(costs), 6) if costs else UNKNOWN,
        "cost_known_n": len(costs),
        "cost_per_accepted_usd": round(sum(costs) / accepted, 6)
                                 if costs and accepted else UNKNOWN,
        "mean_pages_touched": round(sum(pages) / len(pages), 2) if pages else UNKNOWN,
        "pages_known_n": len(pages),
        "lint_defect_rate": _rate(sum(1 for v in lints if v > 0), len(lints)),
        "lint_known_n": len(lints),
        # a stage that always needs its retry costs twice what its per-run
        # averages suggest, so the attempt count is reported beside them
        "mean_attempts": round(sum(tries) / len(tries), 2) if tries else UNKNOWN,
        "max_attempts": max(tries) if tries else UNKNOWN,
        "attempts_known_n": len(tries),
        # how much of the group is measured spend rather than absent usage
        "usage_known_rate": _rate(sum(1 for r in rows if r["usage_known"]),
                                  len(rows)),
    }


def _compare(task_rows: list[dict]) -> dict:
    """cheap versus strong for one task. Reports the sample sizes always and a
    verdict only when both tiers clear MIN_SAMPLE — a model comparison off two
    runs is noise, not evidence."""
    sides = {t: _agg([r for r in task_rows if r["model_tier"] == t])
             for t in ("cheap", "strong")}
    small = [f"{t} n={sides[t]['n']}" for t in ("cheap", "strong")
             if sides[t]["n"] < MIN_SAMPLE]
    out = {"cheap": sides["cheap"], "strong": sides["strong"],
           "min_sample": MIN_SAMPLE}
    if small:
        out["verdict"] = (f"insufficient sample ({', '.join(small)} < "
                          f"{MIN_SAMPLE})")
        return out
    ok = {t: _rate(sides[t]["accepted"], sides[t]["n"]) for t in sides}
    parts = [f"acceptance cheap={ok['cheap']} strong={ok['strong']}"]
    parts += [f"{label} cheap={sides['cheap'][key]} strong={sides['strong'][key]}"
              for label, key in (("cost/accepted", "cost_per_accepted_usd"),
                                 ("median duration", "median_duration_s"),
                                 ("lint defect rate", "lint_defect_rate"))]
    out["verdict"] = (f"n=cheap {sides['cheap']['n']} / strong "
                      f"{sides['strong']['n']}; " + "; ".join(parts))
    return out


def group(rows: list, by: str = "tier", *,
          agg: Callable[[list], dict] = _agg) -> list[dict]:
    """Rows to one aggregate per `GROUP_BY[by]` key, sorted by key: the one
    grouping for the CLI table and anything else that rolls agent runs up
    (the workbench's Agents section, incident-workbench #31).

    `agg` measures one group's rows and defaults to this module's measure. A
    caller whose rows carry a measure of their own — the workbench's token
    and wall-time totals — passes its own, so the partition is shared even
    where the numbers drawn from it are not."""
    fields = GROUP_BY[by]

    def key_of(r):
        return tuple(r[f] for f in fields)

    held: dict[tuple, list] = {}
    for r in rows:
        held.setdefault(key_of(r), []).append(r)
    return [{**dict(zip(fields, key)), **agg(held[key])}
            for key in sorted(held)]


def collect(ledger: dict, task: str | None = None,
            events: list[dict] | None = None, *,
            since: dt.datetime | None = None, until: dt.datetime | None = None,
            by: str = "tier") -> dict:
    """Agent-run events — plus the ledger for the ingests they do not cover —
    to a stats report. `events` comes from `health.read_agent_runs` and is the
    primary source; an empty one means only pre-telemetry ledger history is
    available. `task` filters to one task name; `since`/`until` keep the runs
    stamped inside [since, until), on both sources, before any row is built;
    `by` picks the grouping (`GROUP_BY`). The report names its window and a
    non-default grouping; without them its shape is the one it always had."""
    events = events or []
    # coverage reads every line: a ledger ingest whose agent-run line falls
    # outside the window is still covered, not pre-telemetry history
    covered = {(e.get("run_id"), e.get("task"), e.get("db"))
               for e in events if isinstance(e, dict)}
    if since is not None or until is not None:
        events = [e for e in events if isinstance(e, dict)
                  and in_window(e.get("at"), since, until)]
        ledger = {k: e for k, e in (ledger or {}).items()
                  if isinstance(e, dict) and in_window(e.get("at"), since, until)}
    rows = _event_rows(events)
    rows += _ledger_rows(ledger or {}, covered)
    if task:
        rows = [r for r in rows if r["task"] == task]
    tasks = sorted({r["task"] for r in rows})
    out = {
        "runs": len(rows),
        "task_filter": task,
        "min_sample": MIN_SAMPLE,
        "groups": group(rows, by),
        "totals": _agg(rows),
        "comparison": {t: _compare([r for r in rows if r["task"] == t])
                       for t in tasks},
    }
    if since is not None:
        out["since"] = _stamp(since)
    if until is not None:
        out["until"] = _stamp(until)
    if by != "tier":
        out["by"] = by
    return out


def _cell(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def format_stats(s: dict) -> str:
    """Compact human table: one row per (task, adapter, tier, mode) — model
    in place of tier under `by: model` — then the tier comparison. Sample
    sizes are printed, not implied."""
    filt = f" task={s['task_filter']}" if s["task_filter"] else ""
    filt += "".join(f" {k}={s[k]}" for k in ("since", "until") if k in s)
    out = [f"agent telemetry — {s['runs']} attempted run(s){filt}", ""]
    if not s["runs"]:
        out.append("  (no attempted agent runs recorded)")
        return "\n".join(out)

    by = s.get("by", "tier")
    third = GROUP_BY[by][2]
    head = ("task", "adapter", by, "mode", "n", "ok", "fail", "rollback",
            "med_s", "cost", "med_cost", "cost_n", "pages", "lint_defect",
            "att")
    rows = [head] + [
        (g["task"], g["adapter"], g[third], g["mode"], str(g["n"]),
         str(g["accepted"]), str(g["failed"]), _cell(g["rollback_rate"]),
         _cell(g["median_duration_s"]), _cell(g["total_cost_usd"]),
         _cell(g["median_cost_usd"]), str(g["cost_known_n"]),
         _cell(g["mean_pages_touched"]), _cell(g["lint_defect_rate"]),
         _cell(g["mean_attempts"]))
        for g in s["groups"]]
    t = s["totals"]
    rows.append(("ALL", "-", "-", "-", str(t["n"]), str(t["accepted"]),
                 str(t["failed"]), _cell(t["rollback_rate"]),
                 _cell(t["median_duration_s"]), _cell(t["total_cost_usd"]),
                 _cell(t["median_cost_usd"]), str(t["cost_known_n"]),
                 _cell(t["mean_pages_touched"]), _cell(t["lint_defect_rate"]),
                 _cell(t["mean_attempts"])))
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    for i, r in enumerate(rows):
        out.append("  " + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
        if i == 0:
            out.append("  " + "  ".join("-" * w for w in widths))

    out += ["", "cheap vs strong"]
    for task, cmp in sorted(s["comparison"].items()):
        out.append(f"  {task}: {cmp['verdict']}")
    return "\n".join(out)
