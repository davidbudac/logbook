"""`dbwiki eval history` — the same days, with and without the wiki's memory.

The question is whether the database-history block changes what the model
writes. One database's recent digests are replayed through the real
structured-ingest chain twice: arm `none` builds the prompt with
`history_days=0`, arm `history` with the number the config uses in
production. The two answers for a day sit side by side in one markdown
report, and the days where they disagree about `notable` or about the
incident action are the whole result — everything else in the report is there
to explain those days.

Three rules run through this module:

* **the domain is one day × one arm.** `ARMS` is a table, not a branch; every
  cell is the same frozen `ArmResult`, and `render_report` is a pure function
  of the rows. Adding a third arm is a tuple entry, not a new code path.
* **local loop, no Langfuse.** The fixture eval (`evaluate.py`) keeps that
  transport; this one is a foreground command whose output is a file, and an
  experiment run would only add a way for it to fail.
* **nothing is written but the report.** Every arm reads a throwaway copy of
  the wiki and writes its pages there; the operator's wiki, the ledger and
  the state dir are untouched, and no arm can see what another arm wrote.

One fidelity limit, stated because a replay that quietly cheats is worse than
no replay. `throwaway_copy` deletes the incident pages opened on or after the
replayed day, so an arm is scored on whether it would open the incident the
day deserves rather than on reading it in the tree. That removes the obvious
leak from the future, and not every leak: `past_fixes` carries no window by
design (a fix from six months ago that held is exactly what the model wants),
so a fix recorded last week against an incident opened last year still
reaches the block when the day being replayed is older than the fix. Read a
`history` arm on an old day as "the wiki as it stands today", not "the wiki
as it stood that night".
"""

import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import db_history, evaluate, patterns, structured, trigger
from .incidents import read_incident

#: (name, history_days); `None` means "whatever the config would have used".
ARMS = (("none", 0), ("history", None))

_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_INCIDENT_RE = re.compile(r"incidents/[\w.-]+")

_HEAD = ("day / arm", *(f for f, _ in evaluate.SCORES), "error")


@dataclass(frozen=True)
class ArmResult:
    """One day judged under one arm. Frozen and complete: the report is a
    pure function of these, so the same run always renders the same file."""

    arm: str
    history_days: int
    proposal: dict | None
    verdict: dict
    notable: bool | None
    incident_action: str | None
    journal_entry: str
    summary: str
    history_refs: tuple[str, ...]
    history_chars: int


# ---- dataset ------------------------------------------------------------------

def wiki_items(wiki: Path, db: str, days: int, *,
               today: dt.date) -> list[dict]:
    """The days `today - days <= day < today` that this wiki has a digest for,
    oldest first, each as one `evaluate.dataset_items`-shaped item.

    The days are named rather than globbed, for the reason `changes.recent`
    names them: a digest file may carry a suffix, and a consolidation tick's
    file must not be mistaken for another day. A digest that will not open or
    will not parse is skipped rather than raised — one bad file on disk must
    not cost the replay the other thirteen days.

    There is no golden decision to join here, so `model_tier` comes from
    `trigger.digest_needs_escalation`, the same function the real tick
    escalates on: the arms then run at the tier the night would have run at.
    """
    root = Path(wiki)
    items: list[dict] = []
    for back in range(days, 0, -1):
        day = (today - dt.timedelta(days=back)).isoformat()
        path = root / "digests" / db / f"{day}.json"
        try:
            digest = json.loads(path.read_text())
            tier = "strong" if trigger.digest_needs_escalation(digest) \
                else "cheap"
            codes = structured.digest_codes(digest)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        items.append({
            "id": day,
            "input": {"db": db, "digest": digest, "wiki": str(root)},
            "expected": {"model_tier": tier, "codes": codes},
            "metadata": {"source": f"digests/{db}/{day}.json"},
        })
    return items


# ---- the tree one arm reads ---------------------------------------------------

def throwaway_copy(wiki: Path, dst: Path, db: str, day: str) -> Path:
    """`wiki` copied to `dst` without `.git`, with every incident opened on or
    after `day` deleted and its index bullet with it. Returns `dst`.

    An arm is scored partly on whether it would open the incident the day
    deserves, and the incident that day actually earned is sitting in the
    tree, opened by the ingest this replay is re-running. Left there it is the
    answer printed on the question: the model reads it as already open and
    proposes `update`, or `none`, and the arm measures nothing.

    The prune is deliberately not restricted to `db`, though `db` is part of
    the signature and names the database being replayed. An incident opened
    after this day is a leak from the future whichever database opened it, and
    the prompt shows `all_open_incidents` fleet-wide.

    The source tree is only ever read. A missing `incidents/`, a missing
    `index.md` and a page that will not parse are all ordinary states of a
    real wiki, not errors."""
    shutil.copytree(wiki, dst, ignore=shutil.ignore_patterns(".git"))
    pruned: list[str] = []
    pages = sorted((dst / "incidents").glob("*.md")) \
        if (dst / "incidents").is_dir() else []
    for page in pages:
        rel = page.relative_to(dst).as_posix()
        try:
            incident = read_incident(page.read_text(), rel)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        if incident.opened[:10] >= day:
            page.unlink()
            pruned.append(rel.removesuffix(".md"))
    _drop_index_bullets(dst / "index.md", pruned)
    return dst


def _drop_index_bullets(index: Path, pages: list[str]) -> None:
    """Every index line linking one of `pages`, removed. The index is written
    by `apply_proposal` as `- [[incidents/<slug>]] — <title>`, so the link is
    what identifies the line; matching on the title would drop the wrong
    bullet the first time two incidents share one."""
    if not pages:
        return
    try:
        text = index.read_text()
    except OSError:
        return
    links = {f"[[{page}]]" for page in pages}
    kept = [ln for ln in text.splitlines()
            if not any(link in ln for link in links)]
    index.write_text("\n".join(kept) + "\n")


# ---- what the model could have repeated ---------------------------------------

def history_tokens(block: str, day: str,
                   today_codes=()) -> frozenset[str]:
    """Every token of the rendered history block a model could quote back:
    the earlier days it names, the incident pages it links, and the error
    codes it carries — minus `today_codes`, the codes the day's own digest
    supplies. The block lists past fixes *for today's codes*, so each of
    them is in the prompt twice; a proposal naming one repeats the digest,
    and crediting the history block for it inflated `history_refs`.

    Dates on or after `day` are excluded because they are not history. The
    block's window already ends before `day`, but a fix line carries no window
    and an incident title may name any date at all; counting one of those as
    a repeat would credit the block for a date the digest itself supplies.

    Incident pages are kept as `incidents/<slug>` with any `.md` stripped,
    which is the form the block's own lines use and the form a model writes
    when it links one."""
    tokens = {d for d in _DAY_RE.findall(block) if d < day}
    tokens.update(m.removesuffix(".md") for m in _INCIDENT_RE.findall(block))
    # normalized both sides, so `ORA-01653` and `ORA-1653` are one code
    today = set(patterns.extract_codes(" ".join(map(str, today_codes))))
    tokens.update(c for c in patterns.extract_codes(block) if c not in today)
    return frozenset(tokens)


def _repeated(proposal: dict, tokens: frozenset[str]) -> tuple[str, ...]:
    """The tokens that reached the model's own prose, sorted. Everything the
    proposal writes as text is searched: a date quoted in an error note is as
    much a repeat as one quoted in the journal entry."""
    inc = proposal["incident"]
    text = "\n".join([proposal["summary"], proposal["journal_entry"],
                      inc.get("body") or "",
                      *(u["note"] for u in proposal["error_updates"])])
    return tuple(sorted(t for t in tokens if t in text))


# ---- one day, one arm ---------------------------------------------------------

def run_item(item: dict, cfg, *, arm: str, history_days: int) -> ArmResult:
    """One day through the real ingest chain under one arm, scored.

    The history block is rendered here as well as inside `build_prompt`, with
    exactly the arguments that function passes, so `history_chars` and the
    token set describe the block the model actually saw rather than one this
    module composed for itself.

    The verdict is `evaluate.evaluate_item`'s, field for field, built with
    that module's own helpers. Never raises: a model that answers garbage
    twice and an apply that blows up are results of the experiment, and an
    `ArmResult` carrying the error is the measurement for that cell."""
    verdict = evaluate.empty_verdict()
    proposal = None
    tele: dict = {}
    notable: bool | None = None
    action: str | None = None
    entry = summary = ""
    refs: tuple[str, ...] = ()
    chars = 0
    try:
        db = item["input"]["db"]
        digest = item["input"]["digest"]
        day = digest["window"]["day"]
        codes = structured.digest_codes(digest)
        with tempfile.TemporaryDirectory() as tmp:
            wiki = throwaway_copy(Path(item["input"]["wiki"]),
                                  Path(tmp) / "wiki", db, day)
            block = db_history.render(db_history.gather(
                wiki, db, today=dt.date.fromisoformat(day), codes=codes,
                days=history_days))
            chars = len(block)
            proposal = structured.propose(
                structured.build_prompt(db, digest, wiki,
                                        history_days=history_days), cfg,
                escalate=item["expected"]["model_tier"] == "strong",
                telemetry=tele)
            verdict["parsed_ok"] = True
            verdict["flags"] = len(proposal["flags"])
            verdict["codes_allowlisted"] = evaluate._codes_allowlisted(
                proposal, digest)
            # before the apply, which may open the very incident being scored
            verdict["incident_consistent"] = evaluate._incident_consistent(
                proposal, wiki)
            notable = proposal["notable"]
            action = proposal["incident"]["action"]
            entry = proposal["journal_entry"]
            summary = proposal["summary"]
            refs = _repeated(proposal, history_tokens(block, day, codes))
            result = structured.apply_proposal(
                wiki, db, digest, f"digests/{db}/{day}.md", proposal,
                digest["window"]["to"])
            verdict["apply_ok"] = True
            # the rails add their own flags (a dropped code, a broken link),
            # so the applied count supersedes the proposal's; an apply that
            # raised leaves the proposal's count standing
            verdict["flags"] = len(result["flags"])
    except Exception as e:  # noqa: BLE001 — a failed arm is a measurement
        verdict["error"] = f"{type(e).__name__}: {e}"
    verdict.update(evaluate._telemetry(tele))
    return ArmResult(arm=arm, history_days=history_days, proposal=proposal,
                     verdict=verdict, notable=notable, incident_action=action,
                     journal_entry=entry, summary=summary, history_refs=refs,
                     history_chars=chars)


# ---- the run ------------------------------------------------------------------

def _print_header(model: str, n: int) -> None:
    print(f"eval history — {n} item(s) × {len(ARMS)} arm(s), model {model}",
          file=sys.stderr)
    print(file=sys.stderr)
    print(evaluate._line(_HEAD), file=sys.stderr)
    print("  " + "  ".join("-" * w for w in evaluate._WIDTHS).rstrip(),
          file=sys.stderr, flush=True)


def _print_row(item: dict, result: ArmResult) -> None:
    verdict = result.verdict
    print(evaluate._line((f"{item['id']} {result.arm}",
                          *(evaluate._cell(verdict.get(f))
                            for f, _ in evaluate.SCORES),
                          (verdict["error"] or "")[:60])),
          file=sys.stderr, flush=True)


def run(items: list[dict], cfg, *, model: str, provider: str | None = None,
        days: int, out: Path) -> list[tuple]:
    """Every item under every arm, and the report written to `out`. Returns
    one (item, ArmResult, ArmResult) row per item, in `ARMS` order.

    Progress goes to stderr in the fixture eval's columns, so the report on
    stdout's sibling file and the table on the terminal cannot describe
    different runs. `days` is not in the spec's sketch signature: the report
    header names the window it replayed, and the items alone cannot say
    whether a missing day had no digest or was never asked for."""
    resolved = cfg.agents.get("history_days", structured.DEFAULT_HISTORY_DAYS)
    cfg = evaluate._pinned(cfg, model, provider)
    _print_header(model, len(items))
    rows: list[tuple] = []
    for item in items:
        results = []
        for arm, arm_days in ARMS:
            result = run_item(item, cfg, arm=arm,
                              history_days=resolved if arm_days is None
                              else arm_days)
            _print_row(item, result)
            results.append(result)
        rows.append((item, *results))
    db = rows[0][0]["input"]["db"] if rows else ""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(rows, db=db, model=model, days=days))
    return rows


# ---- the report ---------------------------------------------------------------

def _wiki_rev(wiki: str) -> str:
    """The wiki checkout's short revision, or `""`. A replay of a tree that is
    not a git repository is still a valid replay, so this never raises."""
    try:
        out = subprocess.run(["git", "-C", wiki, "rev-parse", "--short",
                              "HEAD"], capture_output=True, text=True,
                             timeout=10)
    except Exception:  # noqa: BLE001 — a missing sha is metadata, not an error
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _val(v, digits: int = 1) -> str:
    """One measurement, or `-` when the harness never reported it. `0` is a
    measurement and prints as one; absent is not."""
    n = evaluate._num(v)
    return "-" if n is None else evaluate._cell(round(n, digits))


def _mean(values, digits: int = 1) -> str:
    """The mean of the values that are real measurements, or `-`. A cell the
    run could not measure is left out of the average, never counted as zero —
    the same rule `evaluate._num` exists for."""
    nums = [n for n in (evaluate._num(v) for v in values) if n is not None]
    if not nums:
        return "-"
    return evaluate._cell(round(sum(nums) / len(nums), digits))


def _differences(results) -> list[str]:
    """What the arms disagreed about, one phrase each. Only `notable` and the
    incident action count: those two decide which pages the day writes, and
    prose that merely reads differently is not a changed decision.

    Only a day on which every arm parsed is compared: an arm that produced
    no proposal has `notable None`, which is a failure, not history changing
    the decision."""
    if not all(r.verdict.get("parsed_ok") for r in results):
        return []
    out = []
    for label, values in (("notable", [r.notable for r in results]),
                          ("incident", [r.incident_action for r in results])):
        if len(set(values)) > 1:
            out.append(f"{label} " + " -> ".join(str(v) for v in values))
    return out


def _quote(text: str) -> str:
    return "\n".join(f"> {ln}" if ln else ">"
                     for ln in (text.splitlines() or ["(none)"]))


def _summary_row(name: str, results: list) -> str:
    actions = [r.incident_action for r in results]
    return "| " + " | ".join([
        name,
        str(len(results)),
        f"{sum(1 for r in results if r.verdict['parsed_ok'])}/{len(results)}",
        f"{sum(1 for r in results if r.verdict['apply_ok'])}/{len(results)}",
        str(sum(1 for r in results if r.notable)),
        "/".join(str(actions.count(a)) for a in ("open", "update", "none")),
        _mean([len(r.journal_entry) for r in results]),
        _mean([len(r.history_refs) for r in results], 2),
        _mean([r.verdict["input_tokens"] for r in results]),
        _mean([r.verdict["duration_s"] for r in results], 2),
    ]) + " |"


def render_report(rows, *, db: str, model: str, days: int) -> str:
    """The whole comparison as one markdown page: the run's provenance, one
    summary line per arm, one section per replayed day, and the list of days
    where the arms decided differently.

    Pure, and total over an empty `rows`: a database with no digests in the
    window still produces a report that says so."""
    names = [r.arm for r in rows[0][1:]] if rows else [a for a, _ in ARMS]
    wiki = rows[0][0]["input"]["wiki"] if rows else ""
    rev = _wiki_rev(wiki) if wiki else ""
    arms = " | ".join(f"{r.arm} (history_days={r.history_days})"
                      for r in rows[0][1:]) if rows else " | ".join(names)
    out = [f"# History eval — {db}", "",
           f"- model: {model}",
           f"- window: last {days} day(s), {len(rows)} item(s)",
           f"- arms: {arms}",
           f"- wiki: {wiki or '(none)'}" + (f" @ {rev}" if rev else ""),
           f"- generated: {dt.date.today().isoformat()}",
           "", "## Summary", "",
           "| arm | items | parsed_ok | apply_ok | notable | "
           "incident open/update/none | journal chars | history refs | "
           "input tokens | duration |",
           "|" + "---|" * 10]
    for i, name in enumerate(names):
        out.append(_summary_row(name, [row[i + 1] for row in rows]))

    for item, *results in rows:
        differs = _differences(results)
        out += ["", f"## {item['id']}" + (" **differs**" if differs else ""),
                "",
                "| | " + " | ".join(r.arm for r in results) + " |",
                "|" + "---|" * (len(results) + 1)]
        for label, cells in (
                ("notable", [str(r.notable) for r in results]),
                ("incident action",
                 [r.incident_action or "-" for r in results]),
                ("history_refs",
                 [", ".join(r.history_refs) or "(none)" for r in results]),
                ("flags", [_val(r.verdict["flags"], 0) for r in results]),
                ("input tokens",
                 [_val(r.verdict["input_tokens"]) for r in results]),
                ("duration",
                 [_val(r.verdict["duration_s"], 2) for r in results])):
            out.append(f"| {label} | " + " | ".join(cells) + " |")
        for r in results:
            out += ["", f"**Journal — {r.arm}**", "", _quote(r.journal_entry)]
        out.append("")
        for r in results:
            out.append(f"**Summary — {r.arm}**: {r.summary or '(none)'}")

    out += ["", "## Days where history changed the decision", ""]
    changed = [f"- {item['id']} — {'; '.join(diff)}"
               for item, *results in rows
               if (diff := _differences(results))]
    out += changed or ["- (none)"]
    return "\n".join(out) + "\n"
