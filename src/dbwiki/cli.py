"""dbwiki CLI.

  dbwiki compact --db cdb1 --date 2026-07-10          # daily digest
  dbwiki compact --all --date 2026-07-10              # all dbs
  dbwiki compact --db cdb1 --from ISO --to ISO        # ad-hoc window
  dbwiki backfill --from 2025-08-24 --to 2026-07-11   # daily digests over a range
  dbwiki dbs [--date YYYY-MM-DD]                      # discover databases
  dbwiki es search|get ...                            # raw ES drill-down (for agents)
  dbwiki health [--json] [--alert]                    # pipeline health
  dbwiki retry [--dry-run] [--db X]                   # re-ingest failed digests
  dbwiki stats [--json] [--task T]                    # agent cost/quality
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
from pathlib import Path

from .config import load_config

#: commands that mutate the wiki working tree -> the flag (if any) that makes
#: the invocation read-only and so lock-free. Two of these running at once
#: corrupt each other's work, hence lock.py; everything not listed here
#: (`compact`, `dbs`, `es`, `health`, `stats`, `render-daily`, `awr`) either
#: only reads or only touches machine output the agents never see. One
#: exception is handled in `main()`: `health --alert` rewrites
#: `.state/alerts.json` and so takes the lock too.
#:
#: `incident` and `portal` are deliberately absent. `_with_lock` holds the
#: flock around the whole of `args.fn(args)`, which for a server is its
#: lifetime; both take it in `incident_action.publish`, around the commit
#: alone, so the push happens outside it and a hung remote cannot stall a
#: tick. `incident` keeps `--lock-wait`, which reaches `publish`.
LOCKED = {"run": "explain", "ingest": "dry_run", "report": None,
          "lint": "deterministic_only", "research": "dry_run",
          "retry": "dry_run", "analyst": None, "backfill": None}


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
    probe failure must not stop the report."""
    try:
        from .health import assess, health_lines
        return health_lines(assess(cfg))
    except Exception:  # noqa: BLE001
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


def _lock_wait_default() -> float:
    """`--lock-wait`'s default: `DBWIKI_LOCK_WAIT` if the environment sets one
    (so a whole crontab can opt into waiting without editing every line), else
    0 — fail fast, which is what an operator at a terminal wants."""
    try:
        return float(os.environ.get("DBWIKI_LOCK_WAIT") or 0)
    except ValueError:
        return 0.0


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
    # `health --alert` is locked without carrying a --lock-wait flag of its own
    wait = getattr(args, "lock_wait", None)
    wait = wait if wait is not None else _lock_wait_default()
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
    # decide() runs against the ledger as it stands *before* this attempt —
    # orch.ingest() below will mutate it, and its own set_ledger_entry write
    # must not be clobbered, so the decision is merged in only afterward.
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
                                 run_id=rec.run_id)
        except ValidationError as e:
            if not args.dry_run:
                orch.state.merge_ledger_entry(ledger_key, {"last_decision": decision.to_dict()})
            entry["validation"] = "failed"
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"INGEST FAILED validation: {e}", file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001 — HarnessError etc., rolled back by orchestrator
            if not args.dry_run:
                orch.state.merge_ledger_entry(ledger_key, {"last_decision": decision.to_dict()})
            entry["error_category"] = categorize(e)
            _note_telemetry(rec, orch)
            rec.fail(e)
            print(f"INGEST FAILED: {e}", file=sys.stderr)
            return 1
        if not args.dry_run:
            orch.state.merge_ledger_entry(ledger_key, {"last_decision": decision.to_dict()})
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
    from .research_caveats import (approved_fetchable_sources,
                                   build_caveats_prompt,
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
        print("nothing to review: no researched error page is due a caveat pass")
        return 0
    if args.dry_run:
        print("Task: research (caveats).\nError pages due a caveat pass:\n"
              + "\n".join(f"- {p}" for p in pages))
        rel = pages[0]
        found = parse_research(Path(rel).stem, rel, (wiki / rel).read_text())
        print(build_caveats_prompt(Path(rel).stem, found.cause, found.action,
                                   approved_fetchable_sources(wiki)))
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
    from .review import explain, run, selection_for
    cfg = load_config()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")
    if args.explain:
        print(explain(selection_for(cfg, now=now)), end="")
        return 0
    with run_recorder(cfg.state_dir, "review") as rec:
        try:
            out = run(cfg, now=now, force=args.force,
                      deliver_only=args.deliver_only, dry_run=args.dry_run)
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
        req = build_source_review_request(wiki, sp, "show")
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
    with run_recorder(cfg.state_dir, "analyst") as rec:
        request = claim(cfg.wiki_repo, claimed_by, lock=args.lock,
                        kind=args.kind, push=push)
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


def cmd_run(args) -> int:
    """One adaptive scheduler tick: compact since watermark; ingest digests
    that are notable (or all, at the daily consolidation hour); report.
    --explain computes trigger decisions only — no agent call, no watermark
    or ledger writes, no digest emit, and no run-health event.

    Every per-db failure is contained: one database's dead adapter or timeout
    must not skip the databases after it, nor the report, nor — the reason
    this matters — the render and the alerts, which are precisely what makes
    an outage visible. The tick still exits non-zero so cron mail and
    `$?` see it."""
    from .compactor import Compactor
    from .health import categorize, run_recorder
    from .monitoring import ERROR, evaluate_all
    from .orchestrate import Orchestrator, ValidationError
    from .trigger import decide
    cfg = load_config()
    comp = Compactor(cfg)
    orch = Orchestrator(cfg, lock=getattr(args, "lock", None))
    now = dt.datetime.now(dt.timezone.utc)
    t1 = now.strftime("%Y-%m-%dT%H:%M:00Z")
    day = t1[:10]
    consolidation = args.consolidate
    explain = getattr(args, "explain", False)
    t0_day, _ = _day_window(day)
    dbs = comp.discover_dbs(t0_day, t1)

    if explain:
        ledger = orch.state.get_ledger()
        decisions = []
        for db in dbs:
            digest = comp.compact(db, t0_day, t1, day, persist=False)
            jp, _ = comp.digest_paths(db, day)
            entry = ledger.get(str(jp.relative_to(cfg.wiki_repo)), {})
            decisions.append(decide(digest, ledger_entry=entry, window_to=t1,
                                    consolidation=consolidation))
        if getattr(args, "json", False):
            print(json.dumps([d.to_dict() for d in decisions], indent=1))
        else:
            for d in decisions:
                _print_decision(d)
        return 0

    # ADR-0001 fold-in: analyst results left since the last tick, taken in
    # before this tick's own compaction and writes.
    from .queue import fold_results
    fold_results(cfg.wiki_repo, cfg.state_dir,
                push=bool(getattr(cfg, "report", {}).get("push")))

    ingested = []
    failed = False
    with run_recorder(cfg.state_dir, "run") as rec:
        rec.note(consolidation=bool(consolidation), adapter=orch.adapter,
                 dbs=len(dbs))
        if (waited := getattr(args, "lock_wait_s", None)):
            rec.note(lock_wait_s=waited)
        for db in dbs:
            # pre-bound so the `except` below can report where this db broke
            stage, facts, decision, ledger_key = "compact", None, None, None
            try:
                # the daily digest covers 00:00 -> now, regenerated each tick
                before = comp.state.get_watermarks().get(db)
                digest = comp.compact(db, t0_day, t1, day)
                jp, _ = comp.emit(digest)
                comp.state.set_watermark(db, t1)
                ledger_key = str(jp.relative_to(cfg.wiki_repo))
                entry = orch.state.get_ledger().get(ledger_key, {})
                decision = decide(digest, ledger_entry=entry, window_to=t1,
                                  consolidation=consolidation)
                facts = rec.add_db(db, watermark_before=before, watermark_after=t1,
                                   decision=decision.outcome,
                                   decision_reasons=[r["code"] for r in decision.reasons],
                                   model_tier=decision.model_tier,
                                   **_digest_facts(cfg, digest, jp))
                if decision.outcome == "skip":
                    print(f"{db}: skip ({', '.join(r['code'] for r in decision.reasons)})")
                    orch.state.merge_ledger_entry(ledger_key, {"last_decision": decision.to_dict()})
                    continue
                stage = "ingest"
                result = orch.ingest(db, jp, run_id=rec.run_id)
                orch.state.merge_ledger_entry(ledger_key, {"last_decision": decision.to_dict()})
                facts["validation"] = "ok"
                facts["outcome"] = "skipped" if result.get("skipped") else "ingested"
                facts["commit"] = orch.state.get_ledger().get(ledger_key, {}).get("commit")
                ingested.append({"db": db, "notable": result.get("notable"),
                                 "summary": result.get("summary", ""),
                                 "trigger": decision.explanation})
            except Exception as e:  # noqa: BLE001 — one db must not end the tick
                failed = True
                if decision is not None:
                    orch.state.merge_ledger_entry(ledger_key,
                                                  {"last_decision": decision.to_dict()})
                if facts is None:
                    facts = rec.add_db(db)
                if isinstance(e, ValidationError):
                    facts["validation"] = "failed"
                facts["error_category"] = categorize(e)
                facts["error"] = str(e)[:300]
                rec.fail(e)
                print(f"{db}: {stage} failed: {e}", file=sys.stderr)
            if stage == "ingest":
                facts["telemetry"] = dict(orch.last_telemetry)
        if ingested and (any(i["notable"] for i in ingested) or consolidation):
            suffix = "" if consolidation else f"-{now.strftime('%H%M')}"
            try:
                orch.report(day, (t0_day, t1), ingested, suffix,
                            health=_health_note(cfg), run_id=rec.run_id)
                rec.note(report=f"reports/{day}{suffix}.md")
            except Exception as e:  # noqa: BLE001 — the render and the alerts below
                failed = True                  # are how this failure gets noticed
                rec.fail(e)
                rec.note(report_error_category=categorize(e),
                         report_error=str(e)[:300])
                print(f"report failed: {categorize(e)}: {e}", file=sys.stderr)
        try:
            facts = evaluate_all(cfg, cfg.wiki_repo, cfg.state_dir, now=t1,
                                 es=comp.es)
            rec.note(monitoring=len(facts))
            if broken := [f.incident for f in facts if f.verdict == ERROR]:
                rec.note(monitoring_error="no verdict for "
                                          + ", ".join(broken)[:280])
        except Exception as e:  # noqa: BLE001
            rec.note(monitoring_error=f"{categorize(e)}: {e}"[:300])
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
    unknown-handling and minimum-sample rules."""
    from .health import read_agent_runs
    from .state import StateStore
    from .stats import collect, format_stats
    cfg = load_config()
    s = collect(StateStore(cfg.state_dir).get_ledger(), task=args.task,
                events=read_agent_runs(cfg.state_dir))
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
    i.add_argument("--adapter", choices=["codex", "claude", "ollama", "pi"],
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
    read_only = LOCKED.get(args.cmd)
    locked = args.cmd in LOCKED and not (read_only and getattr(args, read_only, False))
    # `health` is read-only except when it alerts: `--alert` rewrites
    # `.state/alerts.json` whole, which loses findings against a concurrent
    # tick's own dispatch. Plain `health` stays lock-free — reading health
    # while a run holds the lock is exactly when an operator needs it.
    if locked or (args.cmd == "health" and getattr(args, "alert", False)):
        return _with_lock(args, args.cmd)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
