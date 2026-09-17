"""`dbwiki stats` — agent cost and quality, read out of the agent-run log.

A pure reader: it opens `.state/agent_runs.jsonl` (one line per attempted
agent stage, written by `health.record_agent_run`), groups the runs by
(task, adapter, model_tier, mode), and reports counts, latency, cost and
defect rates. No datastore, no writes, no network.

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

from statistics import median

MIN_SAMPLE = 5      # runs per side below which no tier verdict is offered
UNKNOWN = "unknown"

# statuses that mean "an agent run was attempted"; a bare trigger-decision
# skip entry has no status and is not a run
_RUN_STATUSES = ("ingested", "failed")


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


def collect(ledger: dict, task: str | None = None,
            events: list[dict] | None = None) -> dict:
    """Agent-run events — plus the ledger for the ingests they do not cover —
    to a stats report. `events` comes from `health.read_agent_runs` and is the
    primary source; an empty one means only pre-telemetry ledger history is
    available. `task` filters to one task name."""
    rows = _event_rows(events or [])
    covered = {(e.get("run_id"), e.get("task"), e.get("db"))
               for e in (events or []) if isinstance(e, dict)}
    rows += _ledger_rows(ledger or {}, covered)
    if task:
        rows = [r for r in rows if r["task"] == task]
    key_of = (lambda r: (r["task"], r["adapter"], r["model_tier"], r["mode"]))
    groups = []
    for key in sorted({key_of(r) for r in rows}):
        sel = [r for r in rows if key_of(r) == key]
        groups.append({"task": key[0], "adapter": key[1], "model_tier": key[2],
                       "mode": key[3], **_agg(sel)})
    tasks = sorted({r["task"] for r in rows})
    return {
        "runs": len(rows),
        "task_filter": task,
        "min_sample": MIN_SAMPLE,
        "groups": groups,
        "totals": _agg(rows),
        "comparison": {t: _compare([r for r in rows if r["task"] == t])
                       for t in tasks},
    }


def _cell(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def format_stats(s: dict) -> str:
    """Compact human table: one row per (task, adapter, tier, mode), then the
    tier comparison. Sample sizes are printed, not implied."""
    filt = f" task={s['task_filter']}" if s["task_filter"] else ""
    out = [f"agent telemetry — {s['runs']} attempted run(s){filt}", ""]
    if not s["runs"]:
        out.append("  (no attempted agent runs recorded)")
        return "\n".join(out)

    head = ("task", "adapter", "tier", "mode", "n", "ok", "fail", "rollback",
            "med_s", "cost", "med_cost", "cost_n", "pages", "lint_defect",
            "att")
    rows = [head] + [
        (g["task"], g["adapter"], g["model_tier"], g["mode"], str(g["n"]),
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
