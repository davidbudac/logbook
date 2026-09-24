"""`dbwiki incident`: the operator's hands on one incident page.

Ten verbs. Two read (`list`, `show`); six mutate (`record-action`,
`monitor`, `extend`, `resolve`, `reopen`, `merge`) and every one of them is
preview-first. A preview prints the diff, the lint verdict, and the exact
command that publishes it, and touches nothing until `--commit`. That retry
line is shell-quoted and carries `--actor`, `--base` and `--at`, so
publishing is a paste rather than a retype, and a second run converges on the
same bytes under the same identity instead of appending a second record.

The last two, `link-codes` and `backfill-resolutions`, are neither: they
carry no operator words into a record, so they have nothing to read back and
offer `--dry-run` instead of `--commit`. Each sweeps every incident page
rather than addressing one, which is why they are the only verbs here that
take no slug.

The grammar is not here. `incident_action` owns which fields a verb takes,
how named values decode into a `lifecycle.Command`, how one renders back into
words, and what the run record says about a transaction, because the portal
needs every one of those too. What is here is argparse, printing, and the map
from a published outcome to an exit code. Flags are built from
`incident_action.command_fields` joined to `PRESENTATION`, so a flag cannot
be spelled two ways and a verb cannot accept a flag its command has no field
for.

Exit codes, the only result-union-to-codes mapping in the CLI: 0 published or
nothing to do, 1 the preview would be blocked or the lock was busy, 2 the
command cannot be built, 3 the base moved, 4 lint blocked, 5 the tree was
dirty. Three, four and five are ordinary traffic under optimistic
concurrency, not pipeline health failures, so they record a `validation` fact
and never `rec.fail`. A busy lock now records the same way, because
`incident_action.publish` takes the lock itself and `incident` is therefore
not in `cli.LOCKED`; the exit code and the holder's message are unchanged.
"""

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

from . import (gitutil, incident_action, incidents, lifecycle, lint,
               pagetext, transaction)
from .config import load_config
from .incident_action import PRESENTATION, VERBS, Widget
from .lock import LockBusyError, lock_wait_default, single_flight

INCIDENT_PAGE_RE = re.compile(r"\Aincidents/[^/]+\.md\Z")

#: The one subject the backfill publishes under, so a second sweep that finds
#: nothing left to link cannot be told apart from the first by its message.
LINK_CODES_MESSAGE = "Incidents: link the error pages they name"
LINK_CODES_NOTHING = ("nothing to do; every incident already links the codes "
                      "it names")

#: The same, for the sweep that puts a `## Resolution history` row on every
#: error page a resolved incident links.
BACKFILL_MESSAGE = ("Error pages: record the incidents already resolved "
                    "against them")
BACKFILL_NOTHING = ("nothing to do; every error page already records the "
                    "incidents resolved against it")

#: published outcome -> exit code.
EXIT_OF: dict[type, int] = {
    transaction.Committed: 0,
    transaction.NothingToDo: 0,
    incident_action.LockBusy: 1,
    transaction.BaseMoved: 3,
    transaction.LintBlocked: 4,
    transaction.TreeDirty: 5,
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: How many seconds past the clock an unpinned `at` may move to find one the
#: page does not already hold (issue 13). A pinned `--at` never moves: a
#: replay must converge on the bytes it names or be refused.
UNPINNED_AT_TRIES = 10


def _next_second(at: str) -> str:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (dt.datetime.strptime(at, fmt)
            + dt.timedelta(seconds=1)).strftime(fmt)


def _flag_kwargs(verb: str, spec: incident_action.Field) -> dict:
    """The argparse kwargs one field's flag takes: help and required from the
    schema, shell shape from the widget. Metavars are the widget's, not a
    fourth column in `PRESENTATION`, because they say the same thing the
    widget already says."""
    pres = PRESENTATION[(verb, spec.name)]
    kwargs: dict = {"help": pres.help, "required": spec.required}
    match pres.widget:
        case Widget.FLAG:
            kwargs.update(action="store_false", dest=spec.name)
        case Widget.LIST:
            kwargs.update(action="append", metavar="PATH")
        case Widget.CHOICE:
            kwargs["choices"] = list(pres.choices)
        case Widget.SIGNAL:
            kwargs["metavar"] = "KIND:PAYLOAD"
        case Widget.INSTANT:
            kwargs["metavar"] = "ISO_Z"
    return kwargs


def _fields(args, verb: str) -> dict:
    """What argparse parked, as the plain data `decode` takes. An absent
    optional stays absent, so the command's own default applies and the retry
    line stays short; a repeatable flag arrives as the list `append` built."""
    out = {}
    for spec in incident_action.command_fields(VERBS[verb]):
        value = getattr(args, spec.name, None)
        if value is None:
            continue
        out[spec.name] = (list(value)
                          if PRESENTATION[(verb, spec.name)].widget
                          is Widget.LIST else value)
    return out


def _lock_wait(args) -> float:
    """`--lock-wait`, then `DBWIKI_LOCK_WAIT`: the precedence `cli._with_lock`
    applied while `incident` was in `LOCKED`, kept now that `publish` takes
    the lock, so the documented flag and env var still mean what they say."""
    wait = getattr(args, "lock_wait", None)
    return wait if wait is not None else lock_wait_default()


def _nothing_to_do(action: incident_action.Action) -> str:
    return (f"nothing to do; {action.path} already holds this action "
            f"at {action.at}")


def _preview(wiki: Path, proposal, action: incident_action.Action) -> int:
    """Show the transaction and refuse to be mistaken for having run it.

    Exit 1 is the lint verdict alone. A stray prints its line and still
    exits 0: it is a fact about the tree rather than about this proposal,
    and clearing it needs no rebuild."""
    pv = transaction.preview(wiki, proposal)
    if pv.paths:
        print(pv.diff, end="")
    else:
        print(_nothing_to_do(action))
    if pv.findings:
        print(lint.format_findings(list(pv.findings)))
    for rel in pv.strays:
        print(f"stray: {rel} is uncommitted and not ours; commit or restore "
              f"it before --commit")
    for note in pv.notes:
        print(f"note: {note}")
    print("preview only. publish with: " + incident_action.retry_line(action))
    return 1 if pv.blocked() else 0


def _report(published: incident_action.Published,
            action: incident_action.Action) -> int:
    """Say exactly what the wiki did, and map it to an exit code. Printing is
    the only thing left here: `publish` already wrote the audit, and it did so
    without printing, because a `dbwiki incident ... | head` closes the pipe
    and a BrokenPipeError raised inside the recorder is what would categorise
    a run whose commit already landed as failed."""
    out: list[str] = []
    err: list[str] = []
    match published:
        case transaction.Committed(sha=sha, paths=paths, pushed=pushed):
            out.append(f"{sha} {' '.join(paths)}"
                       + (" (pushed)" if pushed else ""))
        case transaction.NothingToDo():
            out.append(_nothing_to_do(action))
        case transaction.BaseMoved(expected=expected, actual=actual):
            err.append(f"base moved: {expected[:12]} -> {actual[:12]}; "
                       f"nothing was written. rebuild with --base {actual}")
        case transaction.LintBlocked(findings=findings):
            err.append(lint.format_findings(list(findings)))
            err.append("lint blocked the commit; nothing was written")
        case transaction.TreeDirty(paths=paths):
            err.append(f"wiki tree dirty; nothing was written. commit or "
                       f"restore: {', '.join(paths)}")
        case incident_action.LockBusy(holder=holder):
            err.append(holder)
        case _:
            raise TypeError(f"{published!r} is not a published outcome")
    for line in out:
        print(line)
    for line in err:
        print(line, file=sys.stderr)
    return EXIT_OF[type(published)]


def cmd_incident_apply(args) -> int:
    """One mutating verb, previewed or published.

    Everything that can refuse before the wiki is touched refuses here: an
    unresolvable actor, an unreadable notes file, a field the verb has no home
    for, a signal the codec does not know, an illegal transition, free text
    that cannot become a record. All of those are exit 2, the whole "the
    command could not be built" class, and every builder exception is a
    ValueError subclass so that class is one `except` rather than five."""
    cfg = load_config()
    wiki = cfg.wiki_repo
    try:
        if args.notes_file:
            args.notes = Path(args.notes_file).read_text().strip()
        actor = transaction.resolve_actor(
            wiki, configured=cfg.portal.get("operator_email"),
            override=args.actor)
    except (OSError, RuntimeError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    verb = args.incident_cmd
    try:
        base = args.base or transaction.head(wiki)
        at = args.at or _now()
        tree = None
        for attempt in range(UNPINNED_AT_TRIES):
            action = incident_action.decode(
                slug=args.slug, verb=verb, fields=_fields(args, verb),
                actor=actor, base=base, at=at)
            tree = tree or transaction.Tree.at(wiki, action.base)
            try:
                proposal = action.proposal(tree)
                break
            except incidents.ActionCollision:
                if args.at or attempt == UNPINNED_AT_TRIES - 1:
                    raise
                at = _next_second(at)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if args.preview:
        return _preview(wiki, proposal, action)
    return _report(incident_action.publish(
        wiki, cfg.state_dir, action, proposal, lock_wait_s=_lock_wait(args),
        push=bool(cfg.report.get("push")), surface="cli"), action)


def _window_line(window: incidents.MonitoringWindow) -> str:
    signal = incidents.signal_to_yaml(window.signal)
    payload = next(v for k, v in signal.items() if k != "kind")
    return (f"{signal['kind']} {payload} from {window.start} "
            f"until {window.until}")


def cmd_incident_show(args) -> int:
    """Everything the page says about one incident, plus what may be done to
    it next. Reads the working tree, not a revision: the operator is asking
    what the wiki says now, including a hand edit nobody has committed."""
    from .monitoring import read_facts
    cfg = load_config()
    path = incidents.incident_path(args.slug)
    page = cfg.wiki_repo / path
    if not page.is_file():
        print(f"no incident page at {path}", file=sys.stderr)
        return 2
    inc = incidents.read_incident(page.read_text(), path)
    facts = (read_facts(cfg.state_dir, inc.slug) or {}
             if inc.status is incidents.Status.MONITORING else {})
    verdict = facts.get("verdict") or ""
    if verdict and facts.get("contradictions"):
        verdict += f" (first contradiction: {facts['contradictions'][0]})"
    print(path)
    for label, value in (("title", inc.title), ("db", inc.db)):
        if value:
            print(f"{label}: {value}")
    print(f"status: {inc.status} ({inc.status.label})")
    if inc.unknown_status is not None:
        print(f"status is not in the vocabulary: {inc.unknown_status!r}; "
              f"fix it by hand (lint reports it)")
    for label, value in (
            ("opened", inc.opened),
            ("updated", inc.updated),
            ("errors", ", ".join(inc.error_codes)),
            ("window", _window_line(inc.monitoring) if inc.monitoring else ""),
            ("monitoring", verdict)):
        if value:
            print(f"{label}: {value}")
    print("actions:")
    for record in inc.actions.records:
        print(f"  {record.at} {record.kind} {record.actor} "
              f"[{record.outcome}] {record.summary}")
    if not inc.actions.records:
        print("  (none)")
    print("allowed: " + ", ".join(
        incident_action.VERB_OF[command]
        for command in lifecycle.allowed(inc.status)))
    return 0


def cmd_incident_list(args) -> int:
    """The attention queue, in wiki path order. Carries each monitoring
    incident's last published verdict so an operator sees which windows have
    already answered without opening a page."""
    from .monitoring import read_facts
    cfg = load_config()
    rows = incidents.load_incidents(cfg.wiki_repo)
    if not args.all:
        rows = incidents.active(rows)
    if not rows:
        print("no incidents" if args.all else "no active incidents")
        return 0
    for inc in rows:
        facts = (read_facts(cfg.state_dir, inc.slug) or {}
                 if inc.status is incidents.Status.MONITORING else {})
        verdict = facts.get("verdict") or ""
        print(f"{str(inc.status):<11} {verdict:<11} {inc.slug}  {inc.title}")
    return 0



def _link_codes_sweep(wiki: Path, base: str) -> tuple[dict[str, str],
                                                      dict[str, list[str]]]:
    """What `link_codes` would change across every `incidents/*.md` at
    `base`, as `(files, codes)`: the rewritten page per path it changes, and
    the codes each of those pages newly links.

    Read out of the revision through `Tree.at` rather than off disk, so the
    sweep is built against the same base the commit asserts, and existence is
    answered from the revision's own inventory rather than one `git show` per
    code: 76 error pages against 34 incidents is a lookup, not a fan-out."""
    inventory = frozenset(gitutil.ls_tree(wiki, base))
    tree = transaction.Tree.at(wiki, base)
    files: dict[str, str] = {}
    codes: dict[str, list[str]] = {}
    for rel in sorted(p for p in inventory if INCIDENT_PAGE_RE.match(p)):
        before = tree.read(rel)
        if before is None:
            continue
        after = incidents.link_codes(before, inventory.__contains__)
        if after == before:
            continue
        held = set(incidents.ERROR_LINK_RE.findall(before))
        files[rel] = after
        codes[rel] = [code for code
                      in dict.fromkeys(incidents.ERROR_LINK_RE.findall(after))
                      if code not in held]
    return files, codes


def _report_sweep(published, nothing: str, noun: str) -> int:
    """`_report`'s arms for a transaction that is not one incident's action:
    no `Action` to name, so `NothingToDo` says what the sweep found instead
    of which record a page already holds, and `Committed` counts the pages
    rather than listing them, because a sweep touches dozens.

    `nothing` and `noun` are the only words the two sweeps do not share. Both
    converge, so both reach `NothingToDo` on a second run, and the sentence
    that explains why is the sweep's own."""
    match published:
        case transaction.Committed(sha=sha, paths=paths, pushed=pushed):
            print(f"{sha} {len(paths)} {noun}"
                  f"{'' if len(paths) == 1 else 's'}"
                  + (" (pushed)" if pushed else ""))
        case transaction.NothingToDo():
            print(nothing)
        case transaction.BaseMoved(expected=expected, actual=actual):
            print(f"base moved: {expected[:12]} -> {actual[:12]}; nothing was "
                  f"written. run it again", file=sys.stderr)
        case transaction.LintBlocked(findings=findings):
            print(lint.format_findings(list(findings)), file=sys.stderr)
            print("lint blocked the commit; nothing was written",
                  file=sys.stderr)
        case transaction.TreeDirty(paths=paths):
            print(f"wiki tree dirty; nothing was written. commit or restore: "
                  f"{', '.join(paths)}", file=sys.stderr)
        case incident_action.LockBusy(holder=holder):
            print(holder, file=sys.stderr)
        case _:
            raise TypeError(f"{published!r} is not a published outcome")
    return EXIT_OF[type(published)]


def _publish_sweep(args, cfg, base: str, files: dict[str, str], *,
                   message: str, lock_name: str, nothing: str,
                   noun: str) -> int:
    """Commit what a sweep computed, under its own lock and its own subject.

    The whole tail one sweep shares with the other: resolve the actor, wrap
    the files in a `Proposal` against the base they were built from, hold the
    single-flight lock across the commit, and turn the outcome into an exit
    code. An unresolvable actor is exit 2, the same "the transaction could not
    be built" class `cmd_incident_apply` uses; a lock another surface holds is
    the holder's own message and exit 1.

    `lock_name` is per verb rather than shared: two sweeps writing different
    directories have no reason to queue behind each other, and a name that
    said only `cli:sweep` would make the wait message name the wrong verb."""
    wiki = cfg.wiki_repo
    try:
        actor = transaction.resolve_actor(
            wiki, configured=cfg.portal.get("operator_email"))
    except (RuntimeError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    proposal = transaction.Proposal(base=base, actor=actor, message=message,
                                    files=files)
    try:
        with single_flight(cfg.state_dir, lock_name, _lock_wait(args)) as lock:
            outcome = transaction.commit(wiki, proposal, lock=lock,
                                         push=bool(cfg.report.get("push")))
    except LockBusyError as e:
        outcome = incident_action.LockBusy(str(e))
    return _report_sweep(outcome, nothing, noun)


def cmd_incident_link_codes(args) -> int:
    """Backfill: every incident page links the error pages it only named.

    The reader that answers "which incidents mention ORA-600"
    (`Incident.error_codes`, and every screen built on it) sees a code only
    through a `[[errors/CODE]]` wikilink, and the ingest writer wrote bare
    text until `structured.apply_proposal` learned to link. This verb closes
    that gap for the pages already written, in one commit over the whole
    `incidents/` directory rather than one per page: the sweep is a single
    correction to how the wiki spells one fact, and thirty-four commits
    saying so would bury it.

    `--dry-run` prints `path: CODE, CODE` for each page that would change and
    writes nothing, which is the shape an operator diffs against the wiki
    before letting it publish. Not preview-first like the six mutating verbs:
    those carry an operator's words into an audited record and must be read
    back before they land, while this one only rewrites a spelling the reader
    already knows how to check, and it converges — a second run finds nothing
    left to link and returns `NothingToDo`."""
    cfg = load_config()
    wiki = cfg.wiki_repo
    base = transaction.head(wiki)
    files, codes = _link_codes_sweep(wiki, base)
    if args.dry_run:
        for rel, named in codes.items():
            print(f"{rel}: {', '.join(named)}")
        if not codes:
            print(LINK_CODES_NOTHING)
        return 0
    return _publish_sweep(args, cfg, base, files,
                          message=LINK_CODES_MESSAGE,
                          lock_name="cli:link-codes",
                          nothing=LINK_CODES_NOTHING,
                          noun="incident page")


def _resolution_sweep(
        wiki: Path, base: str) -> tuple[dict[str, str], list[str], list[str]]:
    """What `backfill-resolutions` would change across every `errors/*.md` at
    `base`, as `(files, rows, notes)`: the rewritten error page per path it
    changes, one `errors/<CODE>.md: <slug> (<day>)` line per row it would
    newly write, and an advisory per incident it passed over.

    Read out of the revision through `Tree.at`, like `_link_codes_sweep`, so
    the sweep is built against the same base the commit asserts.

    The rewrite accumulates: one error page can carry rows from several
    incidents, so each incident starts from what the previous one left rather
    than from the revision, and only the pages whose bytes actually moved
    reach `files`. That is also what makes the verb converge: a page that
    already holds every row it should is not in `files`, and a second sweep
    finds nothing.

    The row comes from `lifecycle.resolution_row`, the same function the
    published resolve calls, because a row this writes and a row `build`
    writes must key on the same prefix or a later resolve would append beside
    the backfilled row instead of rewriting it."""
    inventory = frozenset(gitutil.ls_tree(wiki, base))
    tree = transaction.Tree.at(wiki, base)
    pages: dict[str, str] = {}
    rows: list[str] = []
    notes: list[str] = []
    for rel in sorted(p for p in inventory if INCIDENT_PAGE_RE.match(p)):
        text = tree.read(rel)
        if text is None:
            continue
        inc = incidents.read_incident(text, rel)
        if inc.status is not incidents.Status.RESOLVED:
            continue
        record = next((r for r in reversed(inc.actions.records)
                       if r.kind == "resolve"), None)
        if record is None:
            last = (inc.actions.records[-1].kind if inc.actions.records
                    else "none")
            notes.append(f"{rel} is resolved and holds no resolve record "
                         f"(last action: {last}); no "
                         f"{lifecycle.RESOLUTION_SECTION} row was written")
            continue
        if not record.error_pages:
            notes.append(f"{rel} was resolved with --no-error-pages at "
                         f"{record.at}; no {lifecycle.RESOLUTION_SECTION} "
                         f"row was written")
            continue
        prefix, row = lifecycle.resolution_row(inc, record)
        for code in inc.error_codes:
            page_rel = f"errors/{code}.md"
            before = (pages[page_rel] if page_rel in pages
                      else tree.read(page_rel))
            if before is None:
                notes.append(f"{page_rel} is absent; no "
                             f"{lifecycle.RESOLUTION_SECTION} row was written "
                             f"for {inc.slug}")
                continue
            after = pagetext.section_line_replace(
                before, lifecycle.RESOLUTION_SECTION, prefix=prefix, line=row,
                header=lifecycle.RESOLUTION_HEAD)
            pages[page_rel] = after
            if after != before:
                rows.append(f"{page_rel}: {inc.slug} ({record.at[:10]})")
    files = {rel: after for rel, after in pages.items()
             if after != tree.read(rel)}
    return files, rows, notes


def cmd_incident_backfill_resolutions(args) -> int:
    """Backfill: every error page records the incidents already resolved
    against it.

    `## Resolution history` is where an operator meeting an error class reads
    what has actually fixed it before, and `lifecycle.build` has written that
    row since resolves learned to touch error pages. Incidents resolved
    before it did left no row, so the section reads as though nothing was
    ever fixed. This verb writes the missing rows for every resolved incident
    the wiki holds, in one commit over `errors/`, because the sweep is a
    single correction to what the reader is owed rather than one edit per
    incident.

    A resolved incident with no `resolve` record is ordinary traffic, not a
    defect: an incident closed by `merge` folds a duplicate away, which is
    not a confirmed fix and records no row (ADR-0003). The note names the
    kind of the page's last action so an operator reading a screenful of them
    can tell a merge from anything else without opening the pages.

    `--dry-run` prints one line per row it would write and touches nothing,
    not even the lock. Like `link-codes`, this is not preview-first: it
    carries no operator words, writes only rows the resolve records already
    justify, and converges. A second run finds nothing left and returns
    `NothingToDo`."""
    cfg = load_config()
    wiki = cfg.wiki_repo
    base = transaction.head(wiki)
    files, rows, notes = _resolution_sweep(wiki, base)
    for note in notes:
        print(f"note: {note}")
    if args.dry_run:
        for line in rows:
            print(line)
        if not rows:
            print(BACKFILL_NOTHING)
        return 0
    return _publish_sweep(args, cfg, base, files,
                          message=BACKFILL_MESSAGE,
                          lock_name="cli:backfill-resolutions",
                          nothing=BACKFILL_NOTHING,
                          noun="error page")


def _verb_help(command: type) -> str:
    """A subcommand's one-line help: the opening sentence of the `lifecycle`
    command's docstring. Derived rather than restated, so the help text cannot
    come to describe a rule the state machine dropped."""
    return " ".join((command.__doc__ or "").split()).split(". ")[0]


def add_incident_parser(sub, add_lock_wait) -> None:
    """Register the whole `incident` surface on `sub`.

    `add_lock_wait` is `cli._add_lock_wait`, handed in rather than imported:
    the six mutating verbs need that flag, `cli` must not import this module
    at module scope, and its help text keeps one owner. The flag survives
    `incident` leaving `cli.LOCKED`; it now reaches
    `incident_action.publish`, which is what takes the lock."""
    p = sub.add_parser("incident", help="operator actions on an incident")
    verbs = p.add_subparsers(dest="incident_cmd", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--actor", metavar="EMAIL",
                        help="who is acting (default portal.operator_email, "
                             "then the wiki's git user.email)")
    shared.add_argument("--base", metavar="SHA",
                        help="full wiki sha to build against (default HEAD)")
    shared.add_argument("--at", metavar="ISO_Z",
                        help="the record's timestamp; reuse it to retry "
                             "without writing a second record (default now)")
    shared.add_argument("--commit", action="store_false", dest="preview",
                        help="publish; without it nothing is written")
    shared.add_argument("--notes-file", metavar="PATH",
                        help="read --notes from a file")
    add_lock_wait(shared)

    listing = verbs.add_parser("list", help="incidents needing attention")
    listing.add_argument("--all", action="store_true",
                         help="resolved incidents too")
    listing.set_defaults(preview=True, fn=cmd_incident_list)

    show = verbs.add_parser("show", help="one incident, as the wiki has it")
    show.add_argument("slug")
    show.set_defaults(preview=True, fn=cmd_incident_show)

    backfill = verbs.add_parser(
        "link-codes", help="link the error pages every incident only names")
    backfill.add_argument("--dry-run", action="store_true",
                          help="print what would change and write nothing")
    add_lock_wait(backfill)
    backfill.set_defaults(preview=False, fn=cmd_incident_link_codes)

    rows = verbs.add_parser(
        "backfill-resolutions",
        help="record every resolved incident on the error pages it links")
    rows.add_argument("--dry-run", action="store_true",
                      help="print what would change and write nothing")
    add_lock_wait(rows)
    rows.set_defaults(preview=False, fn=cmd_incident_backfill_resolutions)

    for name, command in VERBS.items():
        verb = verbs.add_parser(name, parents=[shared],
                                help=_verb_help(command))
        verb.add_argument("slug")
        for spec in incident_action.command_fields(command):
            verb.add_argument(incident_action.flag_of(name, spec.name),
                              **_flag_kwargs(name, spec))
        verb.set_defaults(fn=cmd_incident_apply)
