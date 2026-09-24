"""dbwiki CLI.

  dbwiki compact --db cdb1 --date 2026-07-10          # daily digest
  dbwiki compact --all --date 2026-07-10              # all dbs
  dbwiki compact --db cdb1 --from ISO --to ISO        # ad-hoc window
  dbwiki backfill --from 2025-08-24 --to 2026-07-11   # daily digests over a range
  dbwiki dbs [--date YYYY-MM-DD]                      # discover databases
  dbwiki es search|get ...                            # raw ES drill-down (for agents)
  dbwiki health [--json] [--alert]                    # pipeline health
  dbwiki retry [--dry-run] [--db X]                   # re-ingest failed digests
  dbwiki stats [--json] [--task T] [--since 12h] [--by model]  # agent cost/quality
  dbwiki eval sync                                    # publish the fixed digest dataset to Langfuse
  dbwiki eval run [--model M] [--provider P]          # score one model on that dataset
  dbwiki eval history --db cdb1 [--days 14]           # replay a db's days with and without its history
  dbwiki awr --file summary.json [--emit]             # AWR summary -> digest
  dbwiki render-daily [--day YYYY-MM-DD]              # daily HTML summary
  dbwiki analyst [--once] [--kind report]             # analyst node: claim + run one queued request (ADR-0001)
  dbwiki incident list|show|record-action|monitor|extend|resolve|reopen  # operator actions on an incident
  dbwiki portal serve [--bind HOST:PORT]              # the incident workbench at http://127.0.0.1:8765
  dbwiki review [--explain] [--force] [--dry-run]     # weekly attention review -> the workbench inbox

Every command that does work records one run-health event (health.py); only
the read-only ones (`dbs`, `es`, `health`, `stats`, `run --explain`,
`review --explain`) stay silent. That event's `run_id` is threaded into the
agent stages, so it also lands on the ledger entry and in the wiki commit's
`Run-ID:` trailer.
"""

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .config import load_config
from .lock import lock_wait_default as _lock_wait_default

if TYPE_CHECKING:
    from .trigger import TriggerDecision

def _unless(flag: str):
    """The invocation locks unless `flag` makes it read-only."""
    return lambda args: not getattr(args, flag)


#: commands that can mutate the wiki working tree (or, for `health`, the
#: shared alert state) -> a predicate over the parsed args: does this
#: invocation take the lock? Two of these running at once corrupt each
#: other's work, hence lock.py; everything not listed here (`compact`, `dbs`,
#: `es`, `stats`, `render-daily`, `awr`) either only reads or only touches
#: machine output the agents never see. `health` locks only with `--alert`,
#: which rewrites `.state/alerts.json` whole and so would lose findings
#: against a concurrent tick's own dispatch; plain `health` stays lock-free —
#: reading health while a run holds the lock is exactly when it is wanted.
#: Every command listed here gets `--lock-wait` (`main()`).
#:
#: `incident` and `portal` are deliberately absent. `_with_lock` holds the
#: flock around the whole of `args.fn(args)`, which for a server is its
#: lifetime; both take it in `incident_action.publish`, around the commit
#: alone, so the push happens outside it and a hung remote cannot stall a
#: tick. `incident` keeps `--lock-wait`, which reaches `publish`.
LOCKED = {"run": _unless("explain"), "ingest": _unless("dry_run"),
          "report": lambda args: True,
          "lint": _unless("deterministic_only"),
          "research": _unless("dry_run"), "retry": _unless("dry_run"),
          "analyst": lambda args: True, "backfill": lambda args: True,
          "health": lambda args: args.alert}


def _day_window(day: str) -> tuple[str, str]:
    d0 = dt.date.fromisoformat(day)
    return f"{d0}T00:00:00Z", f"{d0 + dt.timedelta(days=1)}T00:00:00Z"


def _iso_day(ts: str) -> str:
    return ts[:10]


def _watermark_after(t1: str, now: str | None = None) -> str:
    """`compact --date <today>` and a backfill ending today compact a window
    that closes at midnight tomorrow; the watermark records how far the log
    has been read, which is never later than now."""
    now = now or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return min(t1, now)


def _rel(cfg, p) -> str:
    try:
        return str(Path(p).relative_to(cfg.root))
    except ValueError:
        return str(p)


def _digest_facts(cfg, digest: dict, jp) -> dict:
    """Per-db facts for a run-health event: counts and identifiers only — no
    log messages, no samples (health.py, "facts only")."""
    from .compactor import Compactor
    return {
        "window": digest["window"],
        "events": digest["totals"]["events"],
        "notable_events": digest["totals"]["notable_events"],
        "notable_groups": digest["totals"]["notable_groups"],
        "notable": digest["notable"],
        "deltas": sorted({d["type"] for d in digest["deltas"]}),
        "digest": _rel(cfg, jp),
        "content_hash": Compactor.content_hash(digest),
    }


def _note_telemetry(rec, orch) -> None:
    """Surface the stage telemetry — and any capture miss — in the run-health
    event. Counts only; the ledger keeps the durable copy."""
    if orch.telemetry_errors:
        rec.note(telemetry_errors=orch.telemetry_errors[:5])
    if orch.last_telemetry:
        rec.note(telemetry={k: v for k, v in orch.last_telemetry.items()
                            if k not in ("run_id", "task")})


def _health_note(cfg) -> list[str] | None:
    """Collection-health lines for a report prompt. Optional by design: a
    probe failure must not stop the report, so it drops the block (None)
    and says so once on stderr."""
    try:
        from .health import assess, health_lines
        return health_lines(assess(cfg))
    except Exception as e:  # noqa: BLE001 — the report runs without the block
        from .observability import warn_once
        warn_once("health-note", f"collection-health block dropped from the "
                                 f"report: {type(e).__name__}: {e}"[:300])
        return None


def _alerts(cfg, run_id: str, *, health=None, es=None) -> dict:
    """Failure-only alerts over a fresh health assessment (alerts.py), and the
    run-health facts they produce. Off unless `alerts.enabled`; best-effort
    like the health recording itself — alerting must never fail the run it
    reports on."""
    from .alerts import dispatch, enabled
    if not enabled(cfg):
        return {}
    try:
        from .health import assess
        return dispatch(cfg, health if health is not None else assess(cfg, es=es),
                        run_id)
    except Exception as e:  # noqa: BLE001 — deliberately swallowed
        print(f"warning: alerting failed: {e}", file=sys.stderr)
        return {}


def _add_lock_wait(p) -> None:
    p.add_argument("--lock-wait", type=float, metavar="SECONDS", default=None,
                   help="seconds to wait for the single-flight lock before "
                        "giving up (default 0 = fail fast; env "
                        "DBWIKI_LOCK_WAIT)")


def _with_lock(args, command: str) -> int:
    """Run one wiki-mutating command holding the single-flight lock. A busy
    lock is a run-health event (`lock_busy`) and exit 1, not a traceback: the
    caller is a cron line or an operator, and both need the holder's identity
    more than a stack."""
    from .lock import LockBusyError, single_flight
    cfg = load_config()
    wait = args.lock_wait if args.lock_wait is not None else _lock_wait_default()
    try:
        with single_flight(cfg.state_dir, command, wait) as lock:
            args.lock_wait_s = lock.lock_wait_s
            args.lock = lock
            return args.fn(args)
    except LockBusyError as e:
        from .health import run_recorder
        print(str(e), file=sys.stderr)
        with run_recorder(cfg.state_dir, command) as rec:
            rec.fail(e)
            rec.note(lock_wait_s=wait)
        return 1


def cmd_compact(args) -> int:
    from .compactor import Compactor
    from .health import run_recorder
    cfg = load_config()
    comp = Compactor(cfg)
    if args.date:
        t0, t1 = _day_window(args.date)
        day, suffix = args.date, ""
    else:
        if not (getattr(args, "from_") and args.to):
            print("need --date or --from/--to", file=sys.stderr)
            return 2
        t0, t1 = args.from_, args.to
        day = _iso_day(t0)
        suffix = f"T{t0[11:13]}{t0[14:16]}-{t1[11:13]}{t1[14:16]}" if len(t0) > 10 else ""
    sources = args.sources.split(",") if args.sources else None
    with run_recorder(cfg.state_dir, "compact") as rec:
        rec.note(sources=sources or sorted(cfg.sources))
        dbs = [args.db] if args.db else comp.discover_dbs(t0, t1, sources)
        if not dbs:
            print("no databases found in window", file=sys.stderr)
            return 1
        for db in dbs:
            before = comp.state.get_watermarks().get(db)
            digest = comp.compact(db, t0, t1, day, sources)
            jp, mp = comp.emit(digest, suffix)
            wm = _watermark_after(t1)
            comp.state.set_watermark(db, wm)
            rec.add_db(db, watermark_before=before, watermark_after=wm,
                       **_digest_facts(cfg, digest, jp))
            flag = "NOTABLE" if digest["notable"] else "routine"
            print(f"{db} {day}{suffix}: {digest['totals']['events']} events -> {jp} [{flag}]")
    return 0


def cmd_backfill(args) -> int:
    from .compactor import Compactor
    from .health import run_recorder
    cfg = load_config()
    comp = Compactor(cfg)
    d = dt.date.fromisoformat(args.from_)
    end = dt.date.fromisoformat(args.to)
    notable_days = []
    # one aggregate fact block per db: a year-long backfill must not turn the
    # health event into a per-day log
    with run_recorder(cfg.state_dir, "backfill") as rec:
        rec.note(range=[args.from_, args.to])
        agg: dict[str, dict] = {}
        while d <= end:
            day = d.isoformat()
            t0, t1 = _day_window(day)
            dbs = [args.db] if args.db else comp.discover_dbs(t0, t1)
            for db in dbs:
                digest = comp.compact(db, t0, t1, day)
                if digest["totals"]["events"] == 0 and not digest["notable"]:
                    continue  # nothing logged and no silence delta -> skip file spam
                comp.emit(digest)
                before = comp.state.get_watermarks().get(db)
                wm = _watermark_after(t1)
                comp.state.set_watermark(db, wm)
                a = agg.get(db)
                if a is None:
                    a = agg[db] = rec.add_db(db, digests=0, events=0, notable=0,
                                             watermark_before=before)
                a["digests"] += 1
                a["events"] += digest["totals"]["events"]
                a["notable"] += int(bool(digest["notable"]))
                a["watermark_after"] = wm
                if digest["notable"]:
                    notable_days.append((db, day))
                print(f"{db} {day}: {digest['totals']['events']} events "
                      f"[{'NOTABLE' if digest['notable'] else 'routine'}]")
            d += dt.timedelta(days=1)
    print(f"\nbackfill done; {len(notable_days)} notable (db, day) digests")
    return 0


def cmd_ingest(args) -> int:
    from .health import categorize, run_recorder
    from .orchestrate import Orchestrator, ValidationError
    from .trigger import decide
    cfg = load_config()
    if getattr(args, "adapter", None):
        cfg.agents["adapter"] = args.adapter
    orch = Orchestrator(cfg, lock=getattr(args, "lock", None))
    if args.digest:
        digest_path = cfg.root / args.digest
        db = json.loads(digest_path.read_text())["db"]
    else:
        digest_path = cfg.digest_dir / args.db / f"{args.date}.json"
        db = args.db
    if not digest_path.exists():
        print(f"digest not found: {digest_path}", file=sys.stderr)
        return 1
    digest = json.loads(digest_path.read_text())
    ledger_key = str(digest_path.relative_to(cfg.wiki_repo))
    prior = orch.state.get_ledger().get(ledger_key, {})
    decision = decide(digest, ledger_entry=prior,
                      window_to=digest["window"]["to"], manual=True)
    with run_recorder(cfg.state_dir, "ingest") as rec:
        entry = rec.add_db(db, digest=_rel(cfg, digest_path),
                           window=digest["window"],
                           content_hash=decision.content_hash,
                           decision=decision.outcome,
                           decision_reasons=[r["code"] for r in decision.reasons],
                           model_tier=decision.model_tier,
                           adapter=orch.adapter, dry_run=bool(args.dry_run))
        try:
            result = orch.ingest(db, digest_path, dry_run=args.dry_run,
                                 run_id=rec.run_id, decision=decision)
        except ValidationError as e:
            entry["validation"] = "failed"
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"INGEST FAILED validation: {e}", file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001 — HarnessError etc., rolled back by orchestrator
            entry["error_category"] = categorize(e)
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"INGEST FAILED: {e}", file=sys.stderr)
            return 1
        if not args.dry_run:
            _note_telemetry(rec, orch)
            entry["validation"] = "ok"
            entry["outcome"] = "skipped" if result.get("skipped") else "ingested"
            entry["commit"] = orch.state.get_ledger().get(ledger_key, {}).get("commit")
    if result:
        print(json.dumps(result, indent=1))
    return 0


def cmd_report(args) -> int:
    from .health import run_recorder
    from .orchestrate import Orchestrator, ValidationError
    cfg = load_config()
    orch = Orchestrator(cfg, lock=getattr(args, "lock", None))
    day = args.date or dt.date.today().isoformat()
    t0, t1 = _day_window(day)
    ledger = orch.state.get_ledger()
    ingested = [
        {"db": Path(k).parent.name, "notable": v.get("notable"),
         "summary": v.get("summary", "")}
        for k, v in sorted(ledger.items())
        if v.get("status") == "ingested" and Path(k).stem == day
    ]
    with run_recorder(cfg.state_dir, "report") as rec:
        rec.note(day=day, ingested=len(ingested), adapter=orch.adapter)
        try:
            result = orch.report(day, (t0, t1), ingested,
                                 health=_health_note(cfg), run_id=rec.run_id)
        except ValidationError as e:
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"REPORT FAILED validation: {e}", file=sys.stderr)
            return 1
        _note_telemetry(rec, orch)
        rec.note(report=f"reports/{day}.md")
    print(json.dumps(result, indent=1))
    return 0


def cmd_lint(args) -> int:
    """Deterministic provenance lint over the whole wiki first (no LLM), then
    the agent lint. Mechanical errors are the gate: the agent must not paper
    over them, so it is skipped when any are found."""
    from .health import WikiMissingError, run_recorder
    from .lint import blocking, format_findings, lint_wiki
    from .orchestrate import Orchestrator, ValidationError
    cfg = load_config()
    with run_recorder(cfg.state_dir, "lint") as rec:
        if not cfg.wiki_repo.is_dir():
            rec.fail(WikiMissingError(str(cfg.wiki_repo)))
            print(f"wiki repo not found: {cfg.wiki_repo}", file=sys.stderr)
            return 2
        findings = lint_wiki(cfg.wiki_repo)
        rec.note(deterministic_only=bool(args.deterministic_only),
                 findings=len(findings), blocking=len(blocking(findings)))
        if args.json:
            print(json.dumps([f.to_dict() for f in findings], indent=1))
        else:
            print(format_findings(findings))
        if blocking(findings):
            rec.fail(ValidationError("deterministic lint findings"))
            print("deterministic lint failed; agent lint skipped", file=sys.stderr)
            return 1
        if args.deterministic_only:
            return 0
        orch = Orchestrator(cfg, lock=getattr(args, "lock", None))
        try:
            result = orch.lint(run_id=rec.run_id)
        except ValidationError as e:
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"LINT FAILED validation: {e}", file=sys.stderr)
            return 1
        _note_telemetry(rec, orch)
    print(json.dumps(result, indent=1))
    return 0


def _run_caveats(args, cfg, orch, wiki) -> int:
    """`dbwiki research --caveats`: the practitioner-caveat pass and nothing
    else (orchestrate.Orchestrator.caveats).

    Off unless `research.caveats.enabled` says otherwise, because this is the
    one stage that talks to the web from this box and the one that spends
    paid tokens. `--dry-run` prints the candidates and the exact prompt the
    first of them would send, which is the whole of what leaves the box, and
    makes no call and no write."""
    from .health import categorize, run_recorder
    from .orchestrate import ValidationError
    from .readmodel import parse_research
    from .redact import RedactionLeak
    from .research_caveats import (approved_fetchable_sources,
                                   outbound_caveats_prompt,
                                   select_caveat_candidates)
    ccfg = cfg.research.get("caveats") or {}
    if not ccfg.get("enabled"):
        print("research --caveats is off: set research.caveats.enabled: true "
              "in config/dbwiki.yaml (this is the one stage that talks to the "
              "web)", file=sys.stderr)
        return 1
    limit = args.limit if args.limit is not None else ccfg.get("limit", 2)
    pages = [str(p.relative_to(wiki))
             for p in select_caveat_candidates(wiki, limit, dt.date.today())]
    if not pages:
        if not args.dry_run:
            # a pass with nothing due is the stage working: record it, or a
            # fully reviewed wiki reads as `research_caveats` stale in health
            with run_recorder(cfg.state_dir, "research") as rec:
                rec.note(pages=0, mode="caveats", nothing_due=True)
        print("nothing to review: no researched error page is due a caveat pass")
        return 0
    if args.dry_run:
        print("Task: research (caveats).\nError pages due a caveat pass:\n"
              + "\n".join(f"- {p}" for p in pages))
        rel = pages[0]
        found = parse_research(Path(rel).stem, rel, (wiki / rel).read_text())
        try:
            prompt, _ = outbound_caveats_prompt(cfg, Path(rel).stem, found.cause,
                                                found.action,
                                                approved_fetchable_sources(wiki))
        except RedactionLeak as e:
            print(f"{rel}: LEAK — would not be sent: {e}", file=sys.stderr)
            return 2
        print(prompt)
        return 0
    with run_recorder(cfg.state_dir, "research") as rec:
        rec.note(pages=len(pages), mode="caveats")
        try:
            result = orch.caveats(pages, run_id=rec.run_id)
        except ValidationError as e:
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"RESEARCH FAILED validation: {e}", file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001 — HarnessError etc., rolled back by orchestrator
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"RESEARCH FAILED: {categorize(e)}: {e}", file=sys.stderr)
            return 1
        _note_telemetry(rec, orch)
    print(json.dumps(result, indent=1))
    return 0


def _run_history(args, cfg, orch) -> int:
    """`dbwiki research --history`: regenerate the `## Past fixes` table on
    every error page from our own incident corpus
    (orchestrate.Orchestrator.past_fixes).

    On unless `research.history.enabled` says otherwise, the opposite default
    from `--caveats`: this stage runs no model, opens no socket and sends
    nothing anywhere, so there is nothing for an operator to consent to.
    `--dry-run` prints the section each page would get and writes nothing."""
    from .health import categorize, run_recorder
    from .orchestrate import ValidationError
    hcfg = cfg.research.get("history") or {}
    if not hcfg.get("enabled", True):
        print("research --history is off: set research.history.enabled: true "
              "in config/dbwiki.yaml", file=sys.stderr)
        return 1
    with run_recorder(cfg.state_dir, "research") as rec:
        rec.note(mode="history", dry_run=bool(args.dry_run))
        try:
            result = orch.past_fixes(dry_run=args.dry_run, run_id=rec.run_id)
        except ValidationError as e:
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"RESEARCH FAILED validation: {e}", file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001 — rolled back by the orchestrator
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"RESEARCH FAILED: {categorize(e)}: {e}", file=sys.stderr)
            return 1
        _note_telemetry(rec, orch)
    if result:
        print(json.dumps(result, indent=1))
    return 0


def cmd_research(args) -> int:
    """Research external cause/fix knowledge for stale error pages, and review
    source pages that are past their review interval. Scheduled separately from
    `dbwiki run` — see README."""
    from .health import categorize, run_recorder
    from .orchestrate import Orchestrator, ValidationError
    from .research import select_error_candidates, sources_due_review
    if args.caveats and (args.sources_only or args.review_sources):
        print("research --caveats reviews error pages only: drop "
              "--sources-only / --review-sources", file=sys.stderr)
        return 2
    if args.history and (args.caveats or args.sources_only
                         or args.review_sources):
        print("research --history reads the incident corpus only: drop "
              "--caveats / --sources-only / --review-sources", file=sys.stderr)
        return 2
    cfg = load_config()
    orch = Orchestrator(cfg, lock=getattr(args, "lock", None))
    wiki = cfg.wiki_repo
    if args.history:
        return _run_history(args, cfg, orch)
    if args.caveats:
        return _run_caveats(args, cfg, orch, wiki)
    limit = args.limit if args.limit is not None else cfg.research.get("limit", 3)
    if args.sources_only:
        pages = []
    else:
        pages = [str(p.relative_to(wiki)) for p in select_error_candidates(wiki, limit)]
    if args.review_sources:
        sources = sorted(str(p.relative_to(wiki)) for p in (wiki / "sources").glob("*.md"))
    else:
        sources = [str(p.relative_to(wiki))
                   for p in sources_due_review(wiki, dt.date.today())]
    offload = cfg.research.get("mode") == "offload"
    if not pages and not sources and not offload:
        # a run with nothing due is the stage working: record it, or a fully
        # researched wiki reads as `research` stale in health (issue 17)
        with run_recorder(cfg.state_dir, "research") as rec:
            rec.note(pages=0, sources=0, dry_run=bool(args.dry_run),
                     nothing_due=True)
        print("nothing to research: no stale error pages, no sources due for review")
        return 0
    # offload mode always runs: results the researcher pushed fold in even
    # when there is nothing new to enqueue (ADR-0002)
    with run_recorder(cfg.state_dir, "research") as rec:
        rec.note(pages=len(pages), sources=len(sources),
                 dry_run=bool(args.dry_run))
        try:
            result = orch.research(pages, sources, dry_run=args.dry_run,
                                   run_id=rec.run_id)
        except ValidationError as e:
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"RESEARCH FAILED validation: {e}", file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001 — HarnessError etc., rolled back by orchestrator
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"RESEARCH FAILED: {categorize(e)}: {e}", file=sys.stderr)
            return 1
        _note_telemetry(rec, orch)
    if result:
        print(json.dumps(result, indent=1))
    return 0


def cmd_review(args) -> int:
    """The weekly attention review: select what needs a human's attention from
    the read model, the monitoring facts and the alert state, narrate it once,
    and publish it to the inbox. Scheduled Monday 10:00 — see
    docs/scheduling.md.

    Takes no lock and writes nothing outside `.state/review/`, which is why it
    is absent from LOCKED: it reads the wiki at a pinned head and never touches
    the working tree.

    `--explain` short-circuits before the recorder: `review.selection_for` is
    the same select stage `run` uses, so what it prints is what a run would
    select, and it costs no model call and no write."""
    from .health import categorize, run_recorder
    from .review import explain, load_review, run, selection_for
    from .validate import review_id as valid_review_id
    cfg = load_config()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")
    if args.explain:
        print(explain(selection_for(cfg, now=now)), end="")
        return 0
    review_id = getattr(args, "review_id", None)
    if review_id is not None:
        try:
            if not args.deliver_only:
                raise ValueError("--review-id only applies to --deliver-only")
            valid_review_id(review_id)
            if load_review(cfg.state_dir, review_id) is None:
                raise ValueError(f"no published review {review_id} under "
                                 f".state/review/reviews/")
        except ValueError as e:
            print(f"review: {e}", file=sys.stderr)
            return 2
    with run_recorder(cfg.state_dir, "review") as rec:
        try:
            out = run(cfg, now=now, force=args.force,
                      deliver_only=args.deliver_only, dry_run=args.dry_run,
                      **({"review_id": review_id} if review_id else {}))
        except Exception as e:  # noqa: BLE001 — any failure is a categorized exit 1
            rec.fail(e)
            print(f"REVIEW FAILED {categorize(e)}: {e}", file=sys.stderr)
            return 1
        rec.note(**out.counts())
    if out.synthesis_ok:
        synthesis = "synthesis ok"
    elif out.synthesis_error:
        synthesis = f"synthesis failed ({out.synthesis_error})"
    else:
        synthesis = "no synthesis"
    print(f"{out.review_id}: {out.selected} selected, "
          f"{'published' if out.published else f'not published ({out.skipped})'}"
          f", {synthesis}")
    if out.failed:
        # published (or already there), but somebody did not get it: exit 1
        # so a wrapper notices; `dbwiki health` keeps saying so until retried
        print(f"{out.review_id}: {out.failed} delivery attempt(s) failed "
              f"(see .state/review/log.jsonl); retry: dbwiki review "
              f"--deliver-only --review-id {out.review_id}", file=sys.stderr)
        return 1
    return 0


def cmd_redact(args) -> int:
    """Print exactly what would leave the box for one or more error pages
    (ADR-0002): the redacted research request JSON on stdout, the pseudonym
    mapping and vocabulary summary on stderr. Nothing is enqueued or written
    to `.state/`; a leak-check hit prints the offending tokens and exits 2."""
    from .incidents import by_error_code, load_incidents
    from .redact import RedactionLeak, build_vocabulary
    from .research import select_error_candidates
    from .research_offload import (build_research_request,
                                   build_source_review_request)
    cfg = load_config()
    wiki = cfg.wiki_repo
    vocab = build_vocabulary(cfg, wiki, cfg.state_dir)
    incident_index = by_error_code(load_incidents(wiki))
    print(f"vocabulary: {vocab.summary()}", file=sys.stderr)
    if getattr(args, "fuzz", False):
        from .redact import fuzz, print_fuzz
        return print_fuzz(fuzz(vocab), sys.stdout, vocab)
    pages: list[Path] = []
    if args.all:
        pages = sorted((wiki / "errors").glob("*.md"))
    elif args.candidates:
        pages = select_error_candidates(wiki, cfg.research.get("limit", 3))
    for p in args.pages or []:
        pp = Path(p)
        if not pp.is_absolute():
            pp = (wiki / p) if (wiki / p).exists() else (Path.cwd() / p)
        pages.append(pp)
    rc = 0
    for page in pages:
        try:
            req, red = build_research_request(cfg, wiki, page, "show",
                                              vocab=vocab,
                                              incident_index=incident_index,
                                              state_dir=cfg.state_dir)
        except RedactionLeak as e:
            print(f"{page.name}: LEAK — {e}", file=sys.stderr)
            rc = 2
            continue
        print(json.dumps(req, indent=1, ensure_ascii=False))
        print(f"{page.name}: mapping {json.dumps(red.mapping, ensure_ascii=False)}",
              file=sys.stderr)
    for slug in args.source or []:
        sp = wiki / "sources" / f"{slug}.md"
        req = build_source_review_request(wiki, sp, "show", vocab=vocab)
        from .redact import Redactor
        hits = Redactor(vocab).leak_check(req)
        if hits:
            print(f"{sp.name}: LEAK — {hits}", file=sys.stderr)
            rc = 2
            continue
        print(json.dumps(req, indent=1, ensure_ascii=False))
    if not pages and not args.source:
        print("nothing to show: pass error pages, --all, --candidates or --source",
              file=sys.stderr)
    return rc


def _print_decision(decision) -> None:
    d = decision.to_dict()
    print(f"{d['db']}: {d['outcome']} [{d['model_tier']}]")
    for r in d["reasons"]:
        print(f"  - {r['code']}: {json.dumps(r['evidence'], default=str)}")
    print(f"  {d['explanation']}")


def cmd_analyst(args) -> int:
    """Claim and run one queued analyst request (ADR-0001). `--once` is the
    only mode in v1 and is the default and sole behavior — the flag is
    accepted (and is a no-op) so a cron line or a future watch-loop wrapper
    can pass it unconditionally. `--kind` is reserved for phase 2 (`lint`,
    `research`); v1 only ever queues `report`.

    Runs on the analyst node: cloud-LLM credentials and git credentials, no
    ES credentials, no `.state/` role beyond this command's own run-health
    event — the queue is the only channel back to the on-prem node."""
    import socket
    from .health import categorize, run_recorder
    from .orchestrate import Orchestrator, ValidationError
    from .queue import claim
    cfg = load_config()
    orch = Orchestrator(cfg, lock=getattr(args, "lock", None))
    push = bool(cfg.report.get("push"))
    claimed_by = socket.gethostname()
    # the same budget health flags a claim as stale by, so a claim health
    # calls stale is the one the next claim() puts back in pending/
    stale = {}
    if (stale_hours := cfg.analyst.get("stale_hours")) is not None:
        stale["stale_hours"] = float(stale_hours)
    with run_recorder(cfg.state_dir, "analyst") as rec:
        request = claim(cfg.wiki_repo, claimed_by, lock=args.lock,
                        kind=args.kind, push=push, **stale)
        if request is None:
            rec.note(claimed=False)
            print("nothing to claim")
            return 0
        rec.note(claimed=True, run_id=request["run_id"], day=request["day"],
                 suffix=request["suffix"], kind=request["kind"])
        try:
            result = orch.run_queued_report(request)
        except ValidationError as e:
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"ANALYST FAILED validation: {e}", file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001 — rolled back/requeued by run_queued_report
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"ANALYST FAILED: {categorize(e)}: {e}", file=sys.stderr)
            return 1
        _note_telemetry(rec, orch)
    print(json.dumps(result, indent=1))
    return 0


def _utcnow() -> dt.datetime:
    """The tick's clock; a seam for tests."""
    return dt.datetime.now(dt.timezone.utc)


def _catchup_day(watermark: str | None, day: str) -> str | None:
    """The day before `day` when a db's watermark stops short of `day`'s
    midnight — the first tick after midnight. The day's last tick runs at
    23:30, so without this its final half hour lands in no digest (and the
    registry never learns a code first logged at 23:45). Only yesterday: a
    watermark older than that means the tick was down for days or someone
    re-compacted a past day, and a multi-day catch-up with an agent call per
    day is the operator's call (`dbwiki backfill`, `dbwiki retry`)."""
    from .state import instant_key
    if not watermark or instant_key(watermark) >= instant_key(f"{day}T00:00:00Z"):
        return None
    return (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()


def _catch_up(cfg, comp, orch, db: str, yday: str, watermark: str, *,
              run_id: str, decide) -> tuple[dict, Exception | None]:
    """Recompact `yday`'s full day for `db`, emit it, and ingest it if the
    trigger says so. Idempotent: the full-day digest replaces the partial one
    under the same ledger key, `decide` skips a window already ingested at
    this `window_to` or with this content hash, and `orch.ingest` dedupes on
    the hash again. Never raises: returns the facts for the run-health event
    and the exception, if any, so the caller can hold the watermark back and
    let the next tick retry."""
    from .health import categorize
    from .state import instant_key
    y0, y1 = _day_window(yday)
    facts: dict = {"day": yday}
    if instant_key(watermark) < instant_key(y0):
        # said, not fixed: see _catchup_day
        facts["uncovered_from"] = watermark
        print(f"{db}: watermark {watermark} is older than {yday}; only {yday} "
              f"is caught up — backfill the days before it by hand",
              file=sys.stderr)
    try:
        digest = comp.compact(db, y0, y1, yday)
        jp, _ = comp.emit(digest)
        key = str(jp.relative_to(cfg.wiki_repo))
        decision = decide(digest, ledger_entry=orch.state.get_ledger().get(key, {}),
                          window_to=y1)
        facts.update(decision=decision.outcome,
                     decision_reasons=[r["code"] for r in decision.reasons],
                     events=digest["totals"]["events"],
                     content_hash=decision.content_hash)
        if decision.outcome == "skip":
            orch.record_decision(jp, decision)
            print(f"{db}: catch-up {yday}: skip "
                  f"({', '.join(r['code'] for r in decision.reasons)})")
        else:
            result = orch.ingest(db, jp, run_id=run_id, decision=decision)
            facts["outcome"] = "skipped" if result.get("skipped") else "ingested"
            print(f"{db}: catch-up {yday}: {facts['outcome']}")
    except Exception as e:  # noqa: BLE001 — today's work for this db still runs
        facts["error_category"] = categorize(e)
        facts["error"] = str(e)[:300]
        print(f"{db}: catch-up {yday} failed: {e}", file=sys.stderr)
        return facts, e
    return facts, None


@dataclass
class DbRunResult:
    """How one database's share of a `run` tick ended. `stage` is where it
    got to (`compact` covers the catch-up, the compaction and the trigger
    decision; `ingest` the agent stage); `error` is the db's own failure,
    `catchup_error` a failed catch-up of yesterday, which does not stop
    today's digest. `ingested` is the db's line for the report."""
    stage: str
    facts: dict
    decision: "TriggerDecision | None" = None
    error: Exception | None = None
    catchup_error: Exception | None = None
    ingested: dict | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None or self.catchup_error is not None


def _db_failed(rec, db: str, e: Exception, *, stage: str, facts: dict | None,
               catchup: dict | None, **result) -> DbRunResult:
    """The contained end of one database's failure: its run-health facts,
    the tick's outcome, one stderr line. The tick goes on to the next db."""
    from .health import categorize
    from .orchestrate import ValidationError
    if facts is None:
        facts = rec.add_db(db, **({"catchup": catchup} if catchup else {}))
    if isinstance(e, ValidationError):
        facts["validation"] = "failed"
    facts["error_category"] = categorize(e)
    facts["error"] = str(e)[:300]
    rec.fail(e)
    print(f"{db}: {stage} failed: {e}", file=sys.stderr)
    return DbRunResult(stage, facts, error=e, **result)


def run_one_db(cfg, comp, orch, rec, db: str, *, day: str, t0_day: str,
               t1: str, consolidation: bool) -> DbRunResult:
    """One database's share of a tick: catch up yesterday when the watermark
    stops short of today, compact today so far, decide, and ingest what the
    trigger woke. Never raises: a failure is recorded against this db and
    returned, so one database's dead adapter cannot end the tick."""
    from .trigger import decide
    catchup = catchup_error = decision = facts = None
    try:
        before = comp.state.get_watermarks().get(db)
        wm_after = t1
        if (yday := _catchup_day(before, day)):
            catchup, catchup_error = _catch_up(cfg, comp, orch, db, yday, before,
                                               run_id=rec.run_id, decide=decide)
            if catchup_error is not None:
                rec.fail(catchup_error)
                wm_after = before  # held back: the next tick retries
        # the daily digest covers 00:00 -> now, regenerated each tick
        digest = comp.compact(db, t0_day, t1, day)
        jp, _ = comp.emit(digest)
        if wm_after == t1:
            comp.state.set_watermark(db, t1)
        ledger_key = str(jp.relative_to(cfg.wiki_repo))
        entry = orch.state.get_ledger().get(ledger_key, {})
        decision = decide(digest, ledger_entry=entry, window_to=t1,
                          consolidation=consolidation)
        facts = rec.add_db(db, watermark_before=before,
                           watermark_after=wm_after,
                           decision=decision.outcome,
                           decision_reasons=[r["code"] for r in decision.reasons],
                           model_tier=decision.model_tier,
                           **({"catchup": catchup} if catchup else {}),
                           **_digest_facts(cfg, digest, jp))
        if decision.outcome == "skip":
            print(f"{db}: skip ({', '.join(r['code'] for r in decision.reasons)})")
            orch.record_decision(jp, decision)
            return DbRunResult("compact", facts, decision,
                               catchup_error=catchup_error)
    except Exception as e:  # noqa: BLE001 — one db must not end the tick
        if decision is not None:
            orch.record_decision(jp, decision)
        return _db_failed(rec, db, e, stage="compact", facts=facts,
                          catchup=catchup, decision=decision,
                          catchup_error=catchup_error)
    try:
        result = orch.ingest(db, jp, run_id=rec.run_id, decision=decision)
    except Exception as e:  # noqa: BLE001 — one db must not end the tick
        res = _db_failed(rec, db, e, stage="ingest", facts=facts,
                         catchup=catchup, decision=decision,
                         catchup_error=catchup_error)
    else:
        facts["validation"] = "ok"
        facts["outcome"] = "skipped" if result.get("skipped") else "ingested"
        facts["commit"] = orch.state.get_ledger().get(ledger_key, {}).get("commit")
        res = DbRunResult("ingest", facts, decision, catchup_error=catchup_error,
                          ingested={"db": db, "notable": result.get("notable"),
                                    "summary": result.get("summary", ""),
                                    "trigger": decision.explanation})
    facts["telemetry"] = dict(orch.last_telemetry)
    return res


def _explain_run(args, cfg, comp, orch, dbs: list[str], *, day: str,
                 t0_day: str, t1: str) -> int:
    """`run --explain`: the decisions a tick would make, catch-up included.
    No agent call, no watermark or ledger writes, no digest emit."""
    from .trigger import decide
    ledger = orch.state.get_ledger()
    watermarks = comp.state.get_watermarks()
    decisions = []
    for db in dbs:
        if (yday := _catchup_day(watermarks.get(db), day)):
            y0, y1 = _day_window(yday)
            digest = comp.compact(db, y0, y1, yday, persist=False)
            jp, _ = comp.digest_paths(db, yday)
            entry = ledger.get(str(jp.relative_to(cfg.wiki_repo)), {})
            decisions.append(decide(digest, ledger_entry=entry, window_to=y1))
        digest = comp.compact(db, t0_day, t1, day, persist=False)
        jp, _ = comp.digest_paths(db, day)
        entry = ledger.get(str(jp.relative_to(cfg.wiki_repo)), {})
        decisions.append(decide(digest, ledger_entry=entry, window_to=t1,
                                consolidation=args.consolidate))
    if getattr(args, "json", False):
        print(json.dumps([d.to_dict() for d in decisions], indent=1))
    else:
        for d in decisions:
            _print_decision(d)
    return 0


def _fold_analyst(cfg, rec, push: bool) -> None:
    """ADR-0001 fold-in: pull what the analyst pushed, then fold the results
    it left since the last tick, both before this tick's own compaction and
    writes. Only a tick that pushes, with the analyst on, has a remote the
    analyst writes to. Contained: a failure is a run-health note, not a tick
    that died before it could record anything; a failed pull is fully undone
    and the tick folds whatever is already local."""
    from . import queue as queue_mod
    from .health import categorize
    if push and cfg.analyst.get("enabled"):
        try:
            if (n := queue_mod.pull_before_fold(cfg.wiki_repo)):
                rec.note(analyst_pulled=n)
        except Exception as e:  # noqa: BLE001 — the tick itself still runs
            rec.note(analyst_pull_error=f"{categorize(e)}: {e}"[:300])
            print(f"analyst pull failed (folding local state only): {e}",
                  file=sys.stderr)
    try:
        if (n := queue_mod.fold_results(cfg.wiki_repo, cfg.state_dir,
                                        push=push)):
            rec.note(analyst_results_folded=n)
    except Exception as e:  # noqa: BLE001 — the tick itself still runs
        rec.note(fold_error=f"{categorize(e)}: {e}"[:300])
        print(f"analyst fold-in failed: {e}", file=sys.stderr)


def _report_run(cfg, orch, rec, ingested: list[dict], *, day: str,
                window: tuple[str, str], suffix: str) -> bool:
    """The tick's report. False when it failed — contained, because the
    render and the alerts after it are how that failure gets noticed."""
    from .health import categorize
    try:
        orch.report(day, window, ingested, suffix,
                    health=_health_note(cfg), run_id=rec.run_id)
        rec.note(report=f"reports/{day}{suffix}.md")
        return True
    except Exception as e:  # noqa: BLE001 — see the docstring
        rec.fail(e)
        rec.note(report_error_category=categorize(e),
                 report_error=str(e)[:300])
        print(f"report failed: {categorize(e)}: {e}", file=sys.stderr)
        return False


def _monitor_run(cfg, comp, rec, t1: str) -> None:
    """Incident monitoring verdicts for the tick. Contained: a failure is a
    run-health note."""
    from .health import categorize
    from .monitoring import ERROR, evaluate_all
    try:
        facts = evaluate_all(cfg, cfg.wiki_repo, cfg.state_dir, now=t1,
                             es=comp.es)
        rec.note(monitoring=len(facts))
        if broken := [f.incident for f in facts if f.verdict == ERROR]:
            rec.note(monitoring_error="no verdict for "
                                      + ", ".join(broken)[:280])
    except Exception as e:  # noqa: BLE001
        rec.note(monitoring_error=f"{categorize(e)}: {e}"[:300])


def cmd_run(args) -> int:
    """One adaptive scheduler tick: compact since watermark; ingest digests
    that are notable (or all, at the daily consolidation hour); report.
    --explain computes trigger decisions only — no agent call, no watermark
    or ledger writes, no digest emit, and no run-health event.

    The first tick after midnight first catches up yesterday (`_catch_up`):
    the full day, recompacted and ingested like any tick's digest. If that
    fails, the db's watermark stays where it was so the next tick retries.

    Every per-db failure is contained (`run_one_db`): one database's dead
    adapter or timeout must not skip the databases after it, nor the report,
    nor — the reason this matters — the render and the alerts, which are
    precisely what makes an outage visible. The tick still exits non-zero
    so cron mail and `$?` see it."""
    from .compactor import Compactor
    from .health import run_recorder
    from .orchestrate import Orchestrator
    cfg = load_config()
    comp = Compactor(cfg)
    orch = Orchestrator(cfg, lock=getattr(args, "lock", None))
    now = _utcnow()
    t1 = now.strftime("%Y-%m-%dT%H:%M:00Z")
    day = t1[:10]
    consolidation = args.consolidate
    t0_day, _ = _day_window(day)
    dbs = comp.discover_dbs(t0_day, t1)
    if getattr(args, "explain", False):
        return _explain_run(args, cfg, comp, orch, dbs, day=day, t0_day=t0_day,
                            t1=t1)

    push = bool(cfg.report.get("push"))
    with run_recorder(cfg.state_dir, "run") as rec:
        _fold_analyst(cfg, rec, push)
        rec.note(consolidation=bool(consolidation), adapter=orch.adapter,
                 dbs=len(dbs))
        if (waited := getattr(args, "lock_wait_s", None)):
            rec.note(lock_wait_s=waited)
        results = [run_one_db(cfg, comp, orch, rec, db, day=day, t0_day=t0_day,
                              t1=t1, consolidation=consolidation)
                   for db in dbs]
        failed = any(r.failed for r in results)
        ingested = [r.ingested for r in results if r.ingested]
        if ingested and (any(i["notable"] for i in ingested) or consolidation):
            suffix = "" if consolidation else f"-{now.strftime('%H%M')}"
            if not _report_run(cfg, orch, rec, ingested, day=day,
                               window=(t0_day, t1), suffix=suffix):
                failed = True
        _monitor_run(cfg, comp, rec, t1)
        # rendered every tick so today's summary is never stale, including
        # ticks where every db skipped or failed
        rec.note(html=orch.render_html(day))
        if orch.telemetry_errors:
            rec.note(telemetry_errors=orch.telemetry_errors[:5])
        # alerts last: they judge the state this tick leaves behind
        rec.note(**_alerts(cfg, rec.run_id, es=comp.es))
    return 1 if failed else 0


def cmd_render_daily(args) -> int:
    """Render the deterministic daily HTML summary into the wiki (no LLM, no
    ES, no git). Silent on success, like every other write command here."""
    from .daily_html import DEFAULT_LINK_BASE, write_daily
    from .deeplink import Resolver
    from .state import StateStore
    cfg = load_config()
    if not cfg.wiki_repo.is_dir():
        print(f"wiki repo not found: {cfg.wiki_repo}", file=sys.stderr)
        return 2
    day = args.day or dt.datetime.now(dt.timezone.utc).date().isoformat()
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        print(f"--day must be YYYY-MM-DD, got {day!r}", file=sys.stderr)
        return 2
    write_daily(cfg.wiki_repo, day, state=StateStore(cfg.state_dir),
                link_base=cfg.report.get("link_base", DEFAULT_LINK_BASE),
                links=Resolver.from_config(cfg.portal.get("links") or {}))
    return 0


def cmd_health(args) -> int:
    """Read-only pipeline health. Exit 0 healthy, 1 failure/blocker present,
    2 unusable state (ES unreachable and no local state to fall back on).
    `--alert` additionally evaluates the assessment for alerts (writing
    `.state/alerts.json`) — the one way this command is not read-only."""
    from .health import assess, format_health, new_run_id
    cfg = load_config()
    h = assess(cfg)
    print(json.dumps(h, indent=1, default=str) if args.json else format_health(h))
    if getattr(args, "alert", False):
        from .alerts import enabled
        if not enabled(cfg):
            print("alerts: disabled (alerts.enabled is false)", file=sys.stderr)
        else:
            f = _alerts(cfg, new_run_id(), health=h)
            print(f"alerts: {f.get('alerted', 0)} sent, "
                  f"{f.get('recovered', 0)} recovered", file=sys.stderr)
    return h["exit_code"]


def cmd_retry(args) -> int:
    """Re-ingest failed digests through the normal `orch.ingest` path — which
    dedupes, validates, rolls back on bad output, and never touches unrelated
    changes. Nothing is deleted and no watermark moves."""
    from .health import categorize, plan_retries, run_recorder
    from .orchestrate import Orchestrator
    cfg = load_config()
    orch = Orchestrator(cfg, lock=getattr(args, "lock", None))
    plan, skipped = plan_retries(cfg, orch.state, db=args.db)
    for s in skipped:
        print(f"skip {s['digest']}: {s['reason']}")
    if not plan:
        print("nothing retryable")
        return 0
    if args.dry_run:
        for p in plan:
            print(f"retry {p['digest']} (db {p['db']}, failed {p['at']}, "
                  f"{p['category']}, hash {p['content_hash']})")
        return 0
    rc = 0
    with run_recorder(cfg.state_dir, "retry") as rec:
        rec.note(planned=len(plan), skipped=len(skipped), adapter=orch.adapter)
        for p in plan:
            facts = rec.add_db(p["db"], digest=p["digest"],
                               content_hash=p["content_hash"],
                               retry_of=p["category"], adapter=orch.adapter)
            try:
                result = orch.ingest(p["db"], cfg.wiki_repo / p["digest"],
                                     run_id=rec.run_id)
            except Exception as e:  # noqa: BLE001 — per-digest, keep going
                facts["outcome"] = "failed"
                facts["error_category"] = categorize(e)
                facts["telemetry"] = dict(orch.last_telemetry)
                rec.fail(e)
                rc = 1
                print(f"retry FAILED {p['digest']}: {categorize(e)}: {e}",
                      file=sys.stderr)
                continue
            facts["telemetry"] = dict(orch.last_telemetry)
            led = orch.state.get_ledger().get(p["digest"], {})
            facts["outcome"] = "skipped" if result.get("skipped") else "ingested"
            facts["validation"] = "ok"
            facts["commit"] = led.get("commit")
            print(f"retry ok {p['digest']} -> {led.get('commit', '(no commit)')}")
        if orch.telemetry_errors:
            rec.note(telemetry_errors=orch.telemetry_errors[:5])
    return rc


def cmd_stats(args) -> int:
    """Read-only agent cost/quality summary over `.state/agent_runs.jsonl`
    (every attempted stage) with the ingest ledger behind it for pre-telemetry
    ingests. No agent call, no ES probe, no writes — see stats.py for the
    unknown-handling and minimum-sample rules. `--since`/`--until` bound the
    window (exit 2 on a bound it cannot read); `--by model` groups by model
    instead of tier."""
    from .health import read_agent_runs
    from .state import StateStore
    from .stats import collect, format_stats, parse_when
    now = dt.datetime.now(dt.timezone.utc)
    bounds = {}
    for flag in ("since", "until"):
        if (text := getattr(args, flag)) is not None:
            try:
                bounds[flag] = parse_when(text, now)
            except ValueError as e:
                print(f"stats --{flag}: {e}", file=sys.stderr)
                return 2
    cfg = load_config()
    s = collect(StateStore(cfg.state_dir).get_ledger(), task=args.task,
                events=read_agent_runs(cfg.state_dir), by=args.by, **bounds)
    print(json.dumps(s, indent=1, default=str) if args.json else format_stats(s))
    return 0


def _eval_items(args):
    """The fixed dataset, optionally narrowed to `--only`. An id that matches
    no fixture is an error, not an empty run: a typo would otherwise read as
    a model that scored nothing."""
    from .evaluate import dataset_items
    items = dataset_items(Path(args.fixtures) if args.fixtures else None)
    if not args.only:
        return items
    known = {i["id"] for i in items}
    if unknown := [o for o in args.only if o not in known]:
        raise SystemExit(f"unknown dataset item(s): {', '.join(unknown)}")
    return [i for i in items if i["id"] in args.only]


def cmd_eval_sync(args) -> int:
    """Publish the fixture dataset to Langfuse. Idempotent: item ids are the
    fixture names, so re-running upserts instead of duplicating. Reads
    fixtures and writes nothing but the dataset."""
    from .evaluate import sync
    cfg = load_config()
    n = sync(_eval_items(args), cfg)
    print(f"published {n} dataset item(s)")
    return 0 if n else 1


def cmd_eval_run(args) -> int:
    """Score one model on the fixed dataset: prompt, propose, apply, in a
    throwaway wiki per item. No wiki, no ledger, no state is touched — the
    only side effect is the Langfuse experiment run, when it is enabled."""
    from .evaluate import DATASET_NAME, run
    cfg = load_config()
    pi = (cfg.agents.get("pi") or {})
    model = args.model or pi.get("cheap")
    if not model:
        print("no model: pass --model or set agents.pi.cheap", file=sys.stderr)
        return 2
    run(_eval_items(args), cfg, model=model,
        provider=args.provider or pi.get("provider"),
        name=args.name or DATASET_NAME)
    return 0


def cmd_eval_history(args) -> int:
    """Replay one database's last `--days` digests through the ingest chain
    twice, once with the database-history block and once without, and write
    the comparison report. Every arm runs on a throwaway copy of the wiki:
    the wiki itself, the ledger and the state dir are only read, and the
    report is the one file this command writes."""
    from .history_eval import run, wiki_items
    cfg = load_config()
    pi = (cfg.agents.get("pi") or {})
    model = args.model or pi.get("cheap")
    if not model:
        print("no model: pass --model or set agents.pi.cheap", file=sys.stderr)
        return 2
    wiki = Path(args.wiki) if args.wiki else cfg.wiki_repo
    today = dt.date.today()
    items = wiki_items(wiki, args.db, args.days, today=today)
    if not items:
        print(f"no digests for {args.db} in the {args.days} day(s) before "
              f"{today} under {wiki}", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else \
        cfg.state_dir / "eval" / f"history-{args.db}-{today}.md"
    run(items, cfg, model=model, provider=args.provider or pi.get("provider"),
        days=args.days, out=out)
    print(out)
    return 0


def cmd_awr(args) -> int:
    """Validate one AWR summary file against the v1 contract (docs/awr-input.md)
    and print the digest it produces. `--emit` also writes it under the digest
    dir. Read-only otherwise: no Oracle connection, no ES, no agent."""
    from .awr import AwrContractError, awr_digest, emit_awr_digest, load_awr_summary
    from .health import run_recorder
    cfg = load_config()
    try:
        summary = load_awr_summary(args.file)
        digest = awr_digest(summary)
    except AwrContractError as e:
        print(f"invalid AWR summary: {e}", file=sys.stderr)
        return 2
    if not args.emit:
        print(json.dumps(digest, indent=1))
        return 0
    if not cfg.wiki_repo.exists():
        print(f"wiki repo not found at {cfg.wiki_repo}; refusing to emit",
              file=sys.stderr)
        return 1
    with run_recorder(cfg.state_dir, "awr") as rec:
        jp, mp = emit_awr_digest(cfg, digest)
        rec.add_db(digest["db"], window=digest["window"],
                   rows=digest["totals"]["events"], digest=_rel(cfg, jp),
                   content_hash=_awr_hash(digest))
    print(f"{digest['db']} {digest['window']['day']}: "
          f"{digest['totals']['events']} AWR rows -> {jp}")
    print(json.dumps(digest, indent=1))
    return 0


def _awr_hash(digest: dict) -> str:
    from .compactor import Compactor
    return Compactor.content_hash(digest)


def cmd_dbs(args) -> int:
    from .compactor import Compactor
    cfg = load_config()
    comp = Compactor(cfg)
    day = args.date or dt.date.today().isoformat()
    t0, _ = _day_window(day)
    t1 = f"{day}T23:59:59Z"
    for db in comp.discover_dbs(t0, t1):
        print(db)
    return 0


def cmd_scores(args) -> int:
    """Post the human-feedback scores the incident CLI, the portal and the
    review inbox queued (`scores.send`). Silent when there is nothing to do,
    and without Langfuse config; exit 1 when a score could not be handed
    over (it stays queued, and the next run re-posts it idempotently)."""
    from .scores import send
    out = send(load_config())
    if out.skipped == "no_keys":
        print(f"scores send: langfuse.enabled but no LANGFUSE_PUBLIC_KEY/"
              f"LANGFUSE_SECRET_KEY; {out.pending} score(s) stay queued",
              file=sys.stderr)
    elif out.sent or out.failed:
        print(f"scores send: {out.sent} sent, {out.failed} failed, "
              f"{out.pending} pending")
    return 1 if out.failed else 0


def cmd_langfuse(args) -> int:
    """Push what code owns into Langfuse: the prompt versions every stage runs
    under and the model price rows that turn tokens into cost. Idempotent, and
    nothing here runs during a pipeline tick — an unreachable Langfuse fails
    this command loudly and no run at all."""
    from .promptreg import sync_models, sync_prompts
    cfg = load_config()
    try:
        for line in sync_prompts(cfg):
            print(line)
        for line in sync_models(cfg.langfuse or {}):
            print(line)
    except RuntimeError as e:
        print(f"langfuse sync: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_es(args) -> int:
    from .es import ES
    from .normalize import canon_service_db, db_of
    cfg = load_config()
    es = ES(cfg.es_url, cfg.es_user, cfg.es_password)
    if args.es_cmd == "get":
        print(json.dumps(es.get_doc(args.index, args.id), indent=1))
        return 0
    if args.es_cmd == "trace":
        return _es_trace(cfg, es, args)
    # search
    scfg = cfg.source(args.source)
    extra = []
    if args.db:
        extra.extend(cfg.db_filter(args.source, args.db))
    if args.query:
        extra.append({"query_string": {"query": args.query, "default_field": "*"}})
    q = ES.window_query(scfg["timestamp_field"], args.from_, args.to, extra=extra)
    body = {"size": args.size, "query": q, "sort": [{scfg["timestamp_field"]: "asc"}]}
    res = es.search(",".join(scfg["index_patterns"]), body)
    svc_field = scfg.get("db_service_field", "")
    for h in res["hits"]["hits"]:
        src = h["_source"]
        db = db_of(src, cfg.db_value_fields(args.source))
        if not db and svc_field:
            db = canon_service_db(db_of(src, [svc_field]))
        out = {"index": h["_index"], "id": h["_id"], "ts": src.get("@timestamp"),
               "db": db or None}
        for k in ("oracle", "listener", "dataguard", "message"):
            if k in src:
                out[k] = src[k]
        print(json.dumps(out, default=str))
    return 0


def _es_trace(cfg, es, args) -> int:
    tl = cfg.trace_lookup
    missing = [k for k in ("index_patterns", "path_field") if not tl.get(k)]
    if missing:
        print(f"config/dbwiki.yaml `trace_lookup:` is unusable "
              f"({'absent' if not tl else 'missing ' + ', '.join(missing)}); "
              f"see config/dbwiki.yaml.example", file=sys.stderr)
        return 2
    ts_field = tl.get("timestamp_field", "@timestamp")
    filters: list[dict] = [{"term": {tl["path_field"]: args.path}}]
    span = {k: v for k, v in (("gte", args.from_), ("lt", args.to)) if v}
    if span:
        filters.append({"range": {ts_field: span}})
    res = es.search(",".join(tl["index_patterns"]),
                    {"size": args.size, "query": {"bool": {"filter": filters}},
                     "sort": [{"log.offset": {"order": "asc",
                                              "unmapped_type": "long"}},
                              {ts_field: "asc"}]})
    for h in res["hits"]["hits"]:
        print(json.dumps({"index": h["_index"], "id": h["_id"], **h["_source"]},
                         default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dbwiki")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compact", help="compact a (db, window) into a digest")
    c.add_argument("--db")
    c.add_argument("--all", action="store_true")
    c.add_argument("--date", help="calendar day YYYY-MM-DD (full-day window)")
    c.add_argument("--from", dest="from_", help="ISO start (ad-hoc window)")
    c.add_argument("--to", help="ISO end")
    c.add_argument("--sources", help="comma list, default all")
    c.set_defaults(fn=cmd_compact)

    b = sub.add_parser("backfill", help="daily digests over a date range")
    b.add_argument("--from", dest="from_", required=True)
    b.add_argument("--to", required=True)
    b.add_argument("--db")
    b.set_defaults(fn=cmd_backfill)

    i = sub.add_parser("ingest", help="LLM-ingest a digest into the wiki")
    i.add_argument("--digest", help="path to digest .json (relative to project root)")
    i.add_argument("--db")
    i.add_argument("--date")
    i.add_argument("--dry-run", action="store_true", help="print the prompt only")
    i.add_argument("--adapter", choices=["codex", "claude", "pi"],
                   help="override agents.adapter for this run")
    i.set_defaults(fn=cmd_ingest)

    r = sub.add_parser("report", help="generate the fleet report for a day")
    r.add_argument("--date")
    r.set_defaults(fn=cmd_report)

    l = sub.add_parser("lint", help="deterministic provenance lint, then the lint agent")
    l.add_argument("--deterministic-only", action="store_true",
                   help="mechanical checks only; no agent call")
    l.add_argument("--json", action="store_true",
                   help="emit deterministic findings as JSON")
    l.set_defaults(fn=cmd_lint)

    rs = sub.add_parser("research", help="research error causes from approved sources")
    rs.add_argument("--limit", type=int, help="error pages this run (default: research.limit)")
    rs.add_argument("--review-sources", action="store_true",
                    help="review every source page, not just the ones due")
    rs.add_argument("--sources-only", action="store_true",
                    help="only review sources; skip error-page research")
    rs.add_argument("--caveats", action="store_true",
                    help="practitioner-caveat pass over researched error "
                         "pages (needs research.caveats.enabled; the one "
                         "stage that talks to the web)")
    rs.add_argument("--history", action="store_true",
                    help="regenerate the `## Past fixes` table on every error "
                         "page from our own incident corpus; no model, no web")
    rs.add_argument("--dry-run", action="store_true", help="print the prompt only")
    rs.set_defaults(fn=cmd_research)

    rv = sub.add_parser("review", help="weekly attention review")
    rv.add_argument("--force", action="store_true",
                    help="republish this week's review even if one exists")
    rv.add_argument("--deliver-only", action="store_true",
                    help="retry delivery for this week's review; select nothing")
    rv.add_argument("--review-id", metavar="YYYY-Www",
                    help="with --deliver-only: retry an earlier week's review")
    rv.add_argument("--dry-run", action="store_true",
                    help="select, pack and synthesize but publish nothing")
    rv.add_argument("--explain", action="store_true",
                    help="print the selection and exit; no model call, no writes")
    rv.set_defaults(fn=cmd_review)

    rx = sub.add_parser("redact", help="show the anonymized research request "
                        "that would leave the box (ADR-0002); nothing is sent")
    rx.add_argument("pages", nargs="*", help="errors/<CODE>.md paths (wiki-relative)")
    rx.add_argument("--all", action="store_true", help="every errors/*.md page")
    rx.add_argument("--candidates", action="store_true",
                    help="the pages `dbwiki research` would pick now")
    rx.add_argument("--source", action="append", metavar="SLUG",
                    help="also show the source-review request for sources/<SLUG>")
    rx.add_argument("--show", action="store_true", help="(default; accepted for clarity)")
    rx.add_argument("--fuzz", action="store_true",
                    help="acceptance probe: known leak shapes, filled with the live "
                         "vocabulary, must redact clean and public text survive")
    rx.set_defaults(fn=cmd_redact)

    an = sub.add_parser("analyst", help="claim and run one queued analyst "
                        "report request (ADR-0001)")
    an.add_argument("--once", action="store_true",
                    help="the only mode in v1; default and sole behavior, "
                         "accepted as a no-op")
    an.add_argument("--kind", default="report",
                    help="request kind to claim (reserved; v1 only queues "
                         "'report')")
    an.set_defaults(fn=cmd_analyst)

    u = sub.add_parser("run", help="one adaptive scheduler tick")
    u.add_argument("--consolidate", action="store_true",
                   help="daily consolidation: ingest routine digests too")
    u.add_argument("--explain", action="store_true",
                   help="print trigger decisions only; no agent call or writes")
    u.add_argument("--json", action="store_true",
                   help="with --explain, emit decisions as JSON")
    u.set_defaults(fn=cmd_run)

    rd = sub.add_parser(
        "render-daily", help="render the daily HTML summary into wiki/html/",
        description="Render html/<day>.html plus html/index.html from the "
                    "day's digests, the ingest ledger and the open incidents. "
                    "Deterministic, offline, silent on success. This command "
                    "only writes the files — it never commits; the cron `run` "
                    "flow renders and commits them on every tick, so use this "
                    "for manual checks and backfills.")
    rd.add_argument("--day", help="calendar day YYYY-MM-DD (default: today UTC)")
    rd.set_defaults(fn=cmd_render_daily)

    h = sub.add_parser("health", help="pipeline health, failures, and backlog")
    h.add_argument("--json", action="store_true")
    h.add_argument("--alert", action="store_true",
                   help="also evaluate failure-only alerts (needs alerts.enabled)")
    h.set_defaults(fn=cmd_health)

    ry = sub.add_parser("retry", help="re-ingest failed digests (non-destructive)")
    ry.add_argument("--db", help="limit to one database")
    ry.add_argument("--dry-run", action="store_true", help="print the plan only")
    ry.set_defaults(fn=cmd_retry)

    st = sub.add_parser("stats", help="agent cost/quality telemetry from the ledger")
    st.add_argument("--json", action="store_true")
    st.add_argument("--task", help="limit to one task (ingest, report, ...)")
    st.add_argument("--since", metavar="ISO|DURATION",
                    help="only runs at or after this: an ISO date/time, or a "
                         "duration back from now (90m, 12h, 7d, 2w)")
    st.add_argument("--until", metavar="ISO|DURATION",
                    help="only runs before this (same forms as --since)")
    st.add_argument("--by", choices=["tier", "model"], default="tier",
                    help="group by model tier (default) or by model")
    st.set_defaults(fn=cmd_stats)

    ev = sub.add_parser("eval", help="score a model on the fixed digest dataset")
    evsub = ev.add_subparsers(dest="eval_cmd", required=True)
    ey = evsub.add_parser("sync", help="publish the dataset to Langfuse")
    ey.set_defaults(fn=cmd_eval_sync)
    er = evsub.add_parser("run", help="run one model over the dataset")
    er.add_argument("--model", help="model id (default: agents.pi.cheap)")
    er.add_argument("--provider", help="pi provider (default: agents.pi.provider)")
    er.add_argument("--name", help="experiment name (default: the dataset name)")
    er.set_defaults(fn=cmd_eval_run)
    for sp in (ey, er):
        sp.add_argument("--only", action="append", metavar="ITEM",
                        help="limit to this dataset item (repeatable)")
        sp.add_argument("--fixtures",
                        help="fixture root (default: the repo's tests/fixtures)")
    eh = evsub.add_parser("history", help="replay one database's days with "
                                          "and without its wiki history")
    eh.add_argument("--db", required=True, help="database to replay")
    eh.add_argument("--days", type=int, default=14,
                    help="days of digests to replay (default: 14)")
    eh.add_argument("--wiki", help="wiki checkout to read (default: wiki_repo)")
    eh.add_argument("--model", help="model id (default: agents.pi.cheap)")
    eh.add_argument("--provider", help="pi provider (default: agents.pi.provider)")
    eh.add_argument("--out", help="report path (default: "
                                  "<state_dir>/eval/history-<db>-<today>.md)")
    eh.set_defaults(fn=cmd_eval_history)

    aw = sub.add_parser("awr", help="validate an AWR summary file into a digest")
    aw.add_argument("--file", required=True, help="AWR summary JSON (contract v1)")
    aw.add_argument("--emit", action="store_true",
                    help="also write the digest under the digest dir")
    aw.set_defaults(fn=cmd_awr)

    d = sub.add_parser("dbs", help="discover databases")
    d.add_argument("--date")
    d.set_defaults(fn=cmd_dbs)

    lf = sub.add_parser("langfuse", help="push prompts and model prices to Langfuse")
    lfsub = lf.add_subparsers(dest="langfuse_cmd", required=True)
    lfs = lfsub.add_parser("sync", help="register every prompt version and "
                                        "upsert langfuse.models (idempotent)")
    lfs.set_defaults(fn=cmd_langfuse)

    sc = sub.add_parser("scores", help="human-feedback scores for Langfuse")
    scsub = sc.add_subparsers(dest="scores_cmd", required=True)
    scs = scsub.add_parser("send", help="post .state/pending_scores.jsonl "
                                        "(idempotent; no-op without Langfuse)")
    scs.set_defaults(fn=cmd_scores)

    e = sub.add_parser("es", help="raw ES drill-down")
    esub = e.add_subparsers(dest="es_cmd", required=True)
    s = esub.add_parser("search")
    s.add_argument("--source", required=True)
    s.add_argument("--db")
    s.add_argument("--from", dest="from_", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--query", help="ES query_string syntax")
    s.add_argument("--size", type=int, default=20)
    s.set_defaults(fn=cmd_es)
    g = esub.add_parser("get")
    g.add_argument("--index", required=True)
    g.add_argument("--id", required=True)
    g.set_defaults(fn=cmd_es)
    tr = esub.add_parser("trace", help="raw documents for one trace file path")
    tr.add_argument("--path", required=True, help="exact trace file path")
    tr.add_argument("--from", dest="from_", help="ISO start (default: unbounded)")
    tr.add_argument("--to", help="ISO end (default: unbounded)")
    tr.add_argument("--size", type=int, default=20)
    tr.set_defaults(fn=cmd_es)

    from .incident_cli import add_incident_parser
    add_incident_parser(sub, _add_lock_wait)
    from .portal import add_portal_parser
    add_portal_parser(sub)

    for name in LOCKED:
        _add_lock_wait(sub.choices[name])

    args = p.parse_args(argv)
    if args.cmd in LOCKED and LOCKED[args.cmd](args):
        return _with_lock(args, args.cmd)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
