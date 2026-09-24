"""`Workbench`: the endpoints as methods. Request -> action -> outcome ->
response, through tables.

Thin on purpose. This module decides nothing about incidents; it decides
which domain call to make, which HTTP status an outcome type gets
(`HTTP_OF`), which role a command type needs (`identity.ROLE_OF`), and
one portal policy (`CONFIRMATION`: a `Resolve` commits only with a non-empty
`residual_risk`). The verb grammar, the decode, the audit and the lock belong
to `incident_action`, which the CLI calls with the same arguments.

Server state: two things mutate, the snapshot `cache` holds and the runs the
advisory `runner` has in flight, and each is its own module's business
(`portal/cache.py`, `advisory.py`). Everything else here is as it was:
`Workbench` holds the config, the provider, a clock and the bind it answers
for; the queue and the incident view read HEAD afresh, build their own
memoised `Tree.at`, and hold nothing after they return. Two requests share
nothing but the wiki checkout, which `transaction` already handles: a
per-call `mkdtemp` worktree for previews, the flock for commits.
"""

import datetime as dt
import json
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .. import (advisory, alerts, changes, daily_html, deeplink, events,
                evidence_ref, health, heat, incident_action, incidents,
                lifecycle, lint, links, markdown, monitoring, readmodel,
                review, transaction, validate)
from ..events import Kind, RunEvent
from ..incident_action import Action, FieldError, LockBusy
from ..incidents import ActionMalformed, Incident, Status
from ..lifecycle import TransitionError
from ..links import LinkBoard
from ..transaction import (BaseMoved, Committed, LintBlocked, NothingToDo,
                           ProposalError, Tree, TreeDirty)
from . import identity, wire
from .cache import RevisionCache
from .identity import Principal, Provider, RequestContext, Unidentified
from .wire import WriteRequest

#: outcome type -> HTTP status. A 409 is "the wiki is not where you thought"
#: (nothing written, re-preview); a 422 is "this proposal cannot land as it
#: is" (nothing written, edit); a 423 is "come back in a moment". All three
#: are ordinary optimistic-concurrency traffic and record a `validation`
#: fact, never a failed run.
HTTP_OF: dict[type, int] = {
    Committed: 200,
    NothingToDo: 200,
    BaseMoved: 409,
    TreeDirty: 409,
    LintBlocked: 422,
    LockBusy: 423,
}

#: builder exception -> the stable `error` string of a 422. Checked in order;
#: the final `ValueError` row is the "cannot be built" class the CLI exits 2
#: for. Nothing else raised by `decode` or `build` is expected.
BUILD_ERRORS: tuple[tuple[type, str], ...] = (
    (TransitionError, "transition_refused"),
    (ActionMalformed, "malformed"),
    (FieldError, "field"),
    (ProposalError, "bad_proposal"),
    (ValueError, "not_buildable"),
)

#: command type -> a field that must be non-empty to *commit* (not to
#: preview). The residual-risk confirmation, carried on the command itself:
#: the closer writes what is still not safe (or clicks "none identified",
#: which writes the phrase), it lands as the first paragraph of the record's
#: notes through `lifecycle._notes_with_risk`, and the page keeps the proof.
#: A confirmation a client can skip by not drawing it is not a confirmation,
#: which is why this is checked here and not in the page. The CLI is
#: deliberately not subject to it; the asymmetry is recorded as open.
CONFIRMATION: dict[type, str] = {lifecycle.Resolve: "residual_risk"}

#: How far back `fleet`'s per-database error count reaches, in days.
FLEET_ERROR_WINDOW_DAYS = 30

#: How many days of the heat window `/api/heat` draws when the page names no
#: span. The board's toggle offers 14, 30 and 90; 30 is the middle one and the
#: page's default, and the value is repeated here because a request that omits
#: the parameter still has to mean something.
HEAT_DEFAULT_DAYS = 30

#: How many hours of agent activity `/api/agents` draws when the page names no
#: span. The screen's toggle offers 24, 48 and 168; 48 is the one the operator
#: asked for by default, and the value is repeated here because a request that
#: omits the parameter still has to mean something.
AGENT_DEFAULT_HOURS = 48

#: The longest window `/api/agents` will answer for. It is the touch join's own
#: bound restated in the unit the request uses, so the screen can never ask for
#: a span whose incident half the snapshot did not build.
AGENT_MAX_HOURS = readmodel.TOUCH_WINDOW_DAYS * 24


def _research_proof(mode: str | None
                    ) -> Callable[[RunEvent, Sequence[RunEvent]], bool]:
    """The proof for one research mode (`health.RESEARCH_MODE_KINDS`): an ok,
    non-dry `research` run recorded with that `facts.mode`. Mode-blind, the
    nightly model-free history pass proved the weekly agentic one ran and
    masked it going stale (issue 17), and a dry run proves nothing about the
    stage cron runs."""
    def proves(run: RunEvent, children: Sequence[RunEvent]) -> bool:
        return (run.command == "research" and run.outcome == "ok"
                and not run.dry_run and (run.mode or None) == mode)
    return proves


#: task -> what a RUN event has to show for that task to count as having run.
FRESHNESS_PROOF: dict[str, Callable[[RunEvent, Sequence[RunEvent]], bool]] = {
    "run": lambda run, children: any(child.decision for child in children
                                     if child.kind is Kind.DB_RUN),
    "report": lambda run, children: bool(run.report_path),
    "lint": lambda run, children: (run.command == "lint"
                                   and run.outcome == "ok"),
    # one row per research mode, keyed as `health._last_successes` keys them
    **{kind: _research_proof(mode)
       for mode, kind in health.RESEARCH_MODE_KINDS.items()},
}

HEADING_PREFIX = "wikipage"


def _unwritten(command: lifecycle.Command) -> str | None:
    """`CONFIRMATION`'s field for this command while it is still blank, else
    None. One definition, because `preview` announces it as `requires` and
    `commit` refuses on it: asked for on one path and enforced on the other,
    it would be two rules that could disagree about what counts as blank."""
    field = CONFIRMATION.get(type(command))
    if field is None or getattr(command, field).strip():
        return None
    return field


def _children_by_run(stream: Sequence[RunEvent]) -> dict[str, list[RunEvent]]:
    """`run_id` -> the DB_RUN and STAGE events that share it, grouped in one
    pass. `run_id` is a grouping and never an identity (`events`' own rule:
    the analyst fold-in replays an enqueuing tick's id days later), which is
    why the rows a caller then builds are keyed by `event_id`."""
    out: dict[str, list[RunEvent]] = {}
    for ev in stream:
        if ev.kind in (Kind.DB_RUN, Kind.STAGE):
            out.setdefault(ev.run_id, []).append(ev)
    return out


def _stamp(when: dt.datetime) -> str:
    """A `readmodel.Touch` instant in the second-granularity UTC spelling
    every other timestamp on this wire uses, so a page can compare a touch
    against a ledger `at` without holding two clocks."""
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _once(stream: Sequence[RunEvent]) -> list[RunEvent]:
    """`stream` with each `event_id` kept at its first occurrence.

    `event_id` is the fold-in's idempotency key (`health.
    known_agent_event_ids`) and `dbwiki stats` dedupes agent-run lines on it,
    so a line the log holds twice is one stage run once. Counting it twice
    here would put a model's row on the Agents screen one stage above what
    the CLI says for the same window (incident-workbench #31)."""
    seen: set[str] = set()
    out = []
    for ev in stream:
        if ev.event_id not in seen:
            seen.add(ev.event_id)
            out.append(ev)
    return out


def _stages_by_run(stream: Sequence[RunEvent]) -> dict[str, list[RunEvent]]:
    """`run_id` -> its STAGE events, oldest first, in one pass.

    `_children_by_run`'s grouping narrowed to the ledger lines and put back in
    clock order: both consumers here draw a tick as a span of stages, and a
    list still in the newest-first order `load_events` returns would draw
    every one of them backwards. A stage is drawn once however many times the
    log holds its line, `_once`'s rule."""
    out: dict[str, list[RunEvent]] = {}
    for ev in _once(stream):
        if ev.kind is Kind.STAGE:
            out.setdefault(ev.run_id, []).append(ev)
    for group in out.values():
        group.sort(key=lambda ev: (ev.at, ev.event_id))
    return out


def _agent_stage(resolver: deeplink.Resolver, ev: RunEvent) -> dict:
    return wire.agent_stage_json(ev, resolver.stage_trace(
        event_id=ev.event_id, task=ev.task, db=ev.db))


def _touched_by(resolver: deeplink.Resolver,
                touches: Sequence[readmodel.Touch],
                stages: Mapping[str, Sequence[RunEvent]]) -> tuple[dict, ...]:
    """One `wire.touch_json` per tick that wrote one incident page, newest
    tick first. A tick whose ledger lines have aged out of `.state/` still
    crosses, with no stages: the commit is proof the tick wrote the page, and
    dropping the row would make the wiki's history and this list disagree."""
    by_run: dict[str, list[readmodel.Touch]] = {}
    for touch in touches:
        by_run.setdefault(touch.run_id, []).append(touch)
    rows = [(max(t.at for t in group), run_id, group)
            for run_id, group in by_run.items()]
    return tuple(wire.touch_json(
        run_id=run_id,
        at=_stamp(newest),
        commits=tuple(t.commit for t in group),
        stages=tuple(_agent_stage(resolver, ev)
                     for ev in stages.get(run_id, ())))
        for newest, run_id, group in sorted(rows, reverse=True,
                                            key=lambda row: (row[0], row[1])))


def _tick_incidents(touches: Sequence[readmodel.Touch],
                    snap: readmodel.Snapshot) -> tuple[dict, ...]:
    """One `wire.touched_incident_json` per incident page one tick wrote, by
    slug. The title, status and database are the snapshot's, so a chip here
    and the screen it opens say the same words; a slug the snapshot no longer
    holds keeps its row with those fields empty."""
    by_slug: dict[str, list[readmodel.Touch]] = {}
    for touch in touches:
        by_slug.setdefault(touch.slug, []).append(touch)
    rows = []
    for slug, group in sorted(by_slug.items()):
        inc = snap.incidents.get(slug)
        rows.append(wire.touched_incident_json(
            slug=slug,
            title=inc.title if inc else "",
            status=str(inc.status) if inc else "",
            db=inc.db if inc else "",
            commits=tuple(t.commit for t in group)))
    return tuple(rows)


def _freshness(stream: Sequence[RunEvent], now: str,
               thresholds: Mapping[str, float]) -> tuple[dict, ...]:
    """One `wire.freshness_json` row per `FRESHNESS_PROOF` task: the newest
    `finished or at` over the RUN events that task's proof accepts, its age
    through `health._hours_between`, and `stale` when that age is over the
    budget.

    A task no run proves carries no timestamp and is never stale, which is
    `health._stage_staleness`'s rule for a stage that never succeeded.

    Pure, and given the whole event list once: the children are grouped a
    single time here rather than rescanned per task, because four tasks over
    one capped log is four scans of the same rows for one answer."""
    children = _children_by_run(stream)
    runs = [ev for ev in stream if ev.kind is Kind.RUN]
    rows = []
    for task, proves in FRESHNESS_PROOF.items():
        stamps = [run.finished or run.at for run in runs
                  if proves(run, children.get(run.run_id, ()))]
        at = max((stamp for stamp in stamps if stamp), default="")
        age_h = health._hours_between(at, now) if at else None
        limit = float(thresholds[task])
        rows.append(wire.freshness_json(
            task=task, at=at, age_h=age_h, threshold_h=limit,
            stale=age_h is not None and age_h > limit))
    return tuple(rows)


def _newest_by_digest(stream: Sequence[RunEvent]) -> dict[str, str]:
    """Normalized digest key -> the `run_id` of the newest run that touched
    it, over an event list already in newest-first order.

    The ledger holds one `last_decision` per digest and `state.
    merge_ledger_entry` overwrites it every tick, so this is the one run whose
    row that stored reasoning describes."""
    newest: dict[str, str] = {}
    for ev in stream:
        if ev.kind is Kind.DB_RUN and ev.digest_path:
            newest.setdefault(events.digest_key(ev.digest_path), ev.run_id)
    return newest


def _run_head(run: RunEvent, dbs: Sequence[RunEvent]) -> dict:
    """`wire.run_head_json` for one run and its DB_RUN children. One builder
    for both envelopes, so the counts on the list and the counts on the run
    screen are the same arithmetic and not two readings of it."""
    return wire.run_head_json(
        run_id=run.run_id, command=run.command, started=run.at,
        finished=run.finished, outcome=run.outcome,
        error_category=run.error_category,
        dbs_ingested=sum(1 for db in dbs if db.outcome == "ingested"),
        dbs_skipped=sum(1 for db in dbs if db.decision == "skip"))


def _digest_page(path: str) -> str:
    """The wiki page a recorded digest path belongs to
    (`digests/<db>/<day>.md`), or `""` when the path names no `digests`
    component for `events.digest_key` to normalize from.

    Whether the wiki holds that page is deliberately not answered: it would
    need a snapshot, and this envelope carries no revision to pin such a claim
    to."""
    key = events.digest_key(path)
    if "digests" not in key.split("/"):
        return ""
    return f"{key[:-len('.json')]}.md" if key.endswith(".json") else key


def _page_changes(snap: readmodel.Snapshot, tree: Tree,
                  sidecars: Sequence[str]) -> tuple[dict, ...]:
    """One `wire.change_row_json` per change the `sidecars` record, as the
    fleet timeline `daily_html.change_rows` orders — the function the daily
    page's strip draws from, so the portal and that page cannot list a day's
    changes differently. `change_rows` reads through `changes.of_digest`, so
    a digest written before the `changes` key still shows its lifecycle
    groups.

    A sidecar that will not parse, or parses into something `of_digest`
    cannot read, is skipped rather than raised: one bad file must not cost
    the operator the page, `daily_html.day_digests`' rule. The database is
    the sidecar's directory, which is how that function names it too."""
    digests: dict[str, dict] = {}
    for path in sidecars:
        try:
            digest = json.loads(tree.read(path) or "")
            changes.of_digest(digest)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        digests[path.split("/")[1]] = digest
    return tuple(
        wire.change_row_json(
            db=db, change=change,
            page=page if snap.exists(
                page := f"digests/{db}/{change.day}.md") else "")
        for db, change in daily_html.change_rows(dict(sorted(digests.items()))))


class ApiError(Exception):
    """A refusal with an HTTP status and a stable `error` string. Raised by
    `Workbench` methods; the server renders it with `wire.error_json`."""

    def __init__(self, status: int, error: str, message: str, **extra):
        self.status = status
        self.error = error
        self.extra = extra
        super().__init__(message)


@dataclass(frozen=True)
class Response:
    """What the server writes back. `body` is a string only for `GET /`, the
    one route that answers HTML; every other route answers a dict `wire`
    spelled and `wire.encode` serialises."""

    status: int
    body: dict | str
    content_type: str = "application/json; charset=utf-8"


def utc_now() -> str:
    """The clock a preview mints `at` from. Named rather than private so
    `server` can pass it back in as the default it did not override."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_review_id(text: str) -> bool:
    try:
        validate.review_id(text)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Workbench:
    """Everything the server can be asked. One instance per server; every
    method is safe to call from any thread because it holds nothing.

    `now` is the clock that mints `at` for a preview (tests pin it). `bind`
    is what `GET /api/health` reports and what the server's Host check
    compares against.

    `cache`, `heat_cache` and `runner` are the members that mutate. The
    dataclass stays frozen: no reference changes, and each object behind one
    mutates under its own lock — the caches swap the value they hold
    (`portal/cache.py`'s whole subject), the runner admits and finishes runs
    (`advisory.Runner`'s).

    The two caches are separate one-slot `RevisionCache`s rather than one
    keyed store, because the heat window is built for the board alone and a
    shared slot would let the screen that asks for it evict the snapshot every
    other screen reads.

    The advisory member is spelled `runner` and not `advisory`. A field
    annotated `advisory.Runner` inside this class body is evaluated after the
    name `advisory` has been bound to the field's default in the same
    statement, so the annotation would resolve against `None` rather than
    against the module. None means this deployment builds no runner at all,
    which is what a test that never passes one gets, and every advisory
    endpoint answers 503 for it."""

    cfg: object
    provider: Provider
    bind: str
    now: Callable[[], str] = field(default=utc_now)
    cache: "RevisionCache[readmodel.Snapshot]" = field(
        default_factory=RevisionCache)
    heat_cache: "RevisionCache[heat.Heat]" = field(
        default_factory=RevisionCache)
    runner: "advisory.Runner | None" = None
    board: LinkBoard = field(default_factory=links.empty_board)

    @property
    def wiki(self) -> Path:
        return self.cfg.wiki_repo

    @property
    def state_dir(self) -> Path:
        return self.cfg.state_dir

    @property
    def resolver(self) -> deeplink.Resolver:
        """The deep-link resolver for this deployment, rebuilt per call.

        `Resolver` is stateless and cheap, and building it here rather than
        caching it on the frozen dataclass keeps `cfg` the single source of
        what the deployment maps. Spelled `resolver` and not `links` because
        `_links` is already this class's markdown link policy, and one word
        for two unrelated things is how a caller reaches for the wrong one.
        `cfg.portal.get("links")` may be absent or empty: the portal config
        merge is one level deep, so a block that names `links` replaces
        `PORTAL_DEFAULTS`' mapping whole and a missing sub-key must never be
        a `KeyError`."""
        return deeplink.Resolver.from_config(self.cfg.portal.get("links") or {})

    def _ref_links(self, text: str) -> tuple[dict, ...]:
        """Every deep link one page's evidence references resolve to, in block
        order, each reference's bounded filter ahead of its one document."""
        resolver = self.resolver
        refs, _ = evidence_ref.parse_refs(text)
        return tuple(wire.link_json(link) for ref in refs
                     for link in (resolver.logs(ref), resolver.document(ref)))

    def queue(self, principal: Principal, *, all_: bool = False) -> dict:
        """`GET /api/incidents[?all=1]`: the attention queue in
        `incidents.load_incidents` order, which is path order and the
        positional identity `structured` numbers in the report prompt.

        Reads the working tree, not a revision, so a page the ingest agent
        created and no tick has committed is still listed. Such a row carries
        `dirty: true` and answers `409 uncommitted_page` when opened, naming
        the CLI recipe; it is not a 404, because an operator must be able to
        find the incident they just watched appear.

        Per row: the last published verdict for a monitoring incident
        (`read_facts`), `dirty` from the one `transaction.stray_paths` call
        this request makes, and `lifecycle.allowed` mapped through
        `incident_action.VERB_OF`. Carries `revision` (HEAD now) and every
        stray path. One `git status` per request, never one per row: it is
        the most expensive part of a read. Requires READ_ROLE.

        `last_seen` is `readmodel.last_seen` against the snapshot at
        `revision`, which comes from the one-slot `RevisionCache` this class
        already holds for the read views. So the day costs one snapshot build
        the first time HEAD moves and nothing at all afterwards, where reading
        each linked error page per row would cost a tree read per request.

        The rows come from the working tree and the snapshot from a committed
        revision. An incident page no tick has committed still gets a
        `last_seen`: the occurrence rows it joins against are a fact about the
        committed error pages and not about that page."""
        identity.require(principal, identity.READ_ROLE)
        revision = transaction.head(self.wiki)
        occurrences = self._snapshot(revision).occurrences
        rows = incidents.load_incidents(self.wiki)
        if not all_:
            rows = incidents.active(rows)
        strays = transaction.stray_paths(self.wiki)
        listed = []
        for inc in rows:
            facts = (monitoring.read_facts(self.state_dir, inc.slug)
                     if inc.status is Status.MONITORING else None)
            commands = (() if inc.unknown_status is not None
                        else lifecycle.allowed(inc.status))
            listed.append(wire.queue_row_json(
                inc, verdict=(facts or {}).get("verdict"),
                dirty=inc.path in strays,
                allowed=tuple(incident_action.VERB_OF[command]
                              for command in commands),
                last_seen=readmodel.last_seen(inc, occurrences)))
        return wire.queue_json(revision=revision, strays=strays,
                               rows=tuple(listed))

    def incident(self, principal: Principal, slug: str) -> dict:
        """`GET /api/incidents/{slug}`: one incident as the committed wiki has
        it, at `revision = head(wiki)`, read through `Tree.at`. An uncommitted
        hand edit is not shown, it is *reported* (`dirty`, `strays`).

        `allowed` is one row per verb legal from the page's status:
        `{verb, permitted, role, fields}`, where `fields` is
        `incident_action.command_fields` joined to `PRESENTATION` and `role`
        is `identity.ROLE_OF`. A verb the principal may not run is present
        with `permitted: false` and its role. An off-vocabulary status yields
        no rows at all.

        `links` is one `wire.link_json` per deep link the page's
        `evidence_ref` blocks resolve to, through `deeplink.Resolver`. No
        network call is made to build them.

        `research` is one row per error code the page links, out of the read
        model at the same revision this endpoint already pins. The snapshot
        is built here rather than the `## Reference` blocks being read
        through `tree`, so the incident screen and the database screen answer
        "is this code researched" from one parse of one revision.

        `touched_by` is the loop ticks that wrote this page, joined from the
        snapshot's `Run-ID:` touches to the ledger stages under the same id.
        It reads `.state/` as well as the wiki, the only read view here that
        does, because the question it answers — which tick wrote this, and
        what did that tick run — has one half in each. Through
        `events.stages` and not `load_events`, because this is the screen an
        operator opens all day and the other two logs hold nothing it draws.

        Also: `transaction.page_history(wiki, path, limit=...)` with an empty
        `actor` marking an out-of-band commit, `monitoring.read_facts` ->
        `closure_case(facts, revision=revision)` when the status is
        monitoring, `Actions.problems`, and the page body verbatim. Requires
        READ_ROLE."""
        identity.require(principal, identity.READ_ROLE)
        revision = transaction.head(self.wiki)
        path, tree, inc = self._locate(slug, revision)
        rows = []
        for command in (() if inc.unknown_status is not None
                        else lifecycle.allowed(inc.status)):
            verb = incident_action.VERB_OF[command]
            fields = tuple(
                wire.field_json(verb, spec,
                                incident_action.PRESENTATION[(verb, spec.name)])
                for spec in incident_action.command_fields(command))
            rows.append(wire.allowed_json(
                verb, identity.ROLE_OF[command],
                identity.permits(principal, command), fields))
        body = tree.read(path)
        snap = self._snapshot(revision)
        return wire.incident_json(
            inc, revision=revision, body=body, allowed=tuple(rows),
            links=self._ref_links(body or ""),
            closure=self._closure(inc, revision),
            history=transaction.page_history(self.wiki, path),
            strays=transaction.stray_paths(self.wiki),
            link_base=self.cfg.report.get("link_base",
                                          daily_html.DEFAULT_LINK_BASE),
            exists=lambda rel: tree.read(rel) is not None,
            touched_by=_touched_by(
                self.resolver, snap.touches_by_incident.get(slug, ()),
                _stages_by_run(events.stages(self.state_dir))),
            research=snap.research, resolutions=snap.resolutions,
            past_fixes=snap.past_fixes,
            last_seen=readmodel.last_seen(inc, snap.occurrences))

    def fleet(self, principal: Principal) -> dict:
        """`GET /api/fleet`: one row per database the wiki holds a
        `databases/<db>.md` page for, in `snap.dbs` order, which is sorted.

        Every count is read off the snapshot rather than the working tree, so
        the whole row set costs the one `head` call plus a rebuild when HEAD
        has moved since the last request. `open` and `monitoring` count the
        database's incidents by status; `journal_day` and `journal_headline`
        are its newest journal entry, `""` each when it has none; `errors_30d`
        counts its `## Occurrences` rows inside `FLEET_ERROR_WINDOW_DAYS` of
        today.

        The error window is applied here and not in `readmodel`, because "the
        last 30 days" is a fact about when the operator is looking and the
        snapshot is a fact about a revision. Requires READ_ROLE."""
        identity.require(principal, identity.READ_ROLE)
        head = transaction.head(self.wiki)
        snap = self._snapshot(head)
        since = (dt.date.fromisoformat(self.now()[:10])
                 - dt.timedelta(days=FLEET_ERROR_WINDOW_DAYS)).isoformat()
        rows = []
        for db in snap.dbs:
            statuses = [inc.status for inc in snap.db_incidents(db)]
            journal = snap.journals.get(db, ())
            rows.append(wire.fleet_row_json(
                db=db,
                open_=statuses.count(Status.OPEN),
                monitoring=statuses.count(Status.MONITORING),
                journal_day=journal[0].day if journal else "",
                journal_headline=journal[0].headline if journal else "",
                errors_30d=len([o for o in snap.occurrences_of(db=db)
                                if o.day >= since]),
                page=f"databases/{db}.md"))
        return wire.fleet_json(prov=wire.provenance_json(snap, head=head),
                               rows=tuple(rows))

    def heat(self, principal: Principal, days: str) -> dict:
        """`GET /api/heat?days=`: daily `error`, `warning` and `unmatched`
        counts per database over the last `days` days.

        `days` arrives as the raw query string, the `database(name)`
        precedent: the query is the boundary and the parse belongs on this
        side of it. Empty means `HEAT_DEFAULT_DAYS`, which is what a page
        that has not touched its toggle sends. Anything else that is not an
        integer in 1..`heat.WINDOW_DAYS` is a 400 `bad_days` rather than a
        clamp, because a page asking for 400 days has a bug and silently
        drawing 90 would hide it.

        The whole window is built once per revision and sliced here. The
        toggle is a fact about what the operator wants to look at and the
        window is a fact about a revision, the same split `fleet` makes for
        its 30-day error count, so flipping 30 to 90 costs a slice and not a
        rebuild. Both `days` and every counts array are sliced by the same
        `-n:`, which is what keeps a column and its date aligned.

        Requires READ_ROLE."""
        identity.require(principal, identity.READ_ROLE)
        refusal = (f"days must be an integer in 1..{heat.WINDOW_DAYS}, "
                   f"not {days!r}")
        try:
            span = HEAT_DEFAULT_DAYS if not days else int(days)
        except ValueError:
            raise ApiError(400, "bad_days", refusal) from None
        if not 1 <= span <= heat.WINDOW_DAYS:
            raise ApiError(400, "bad_days", refusal)
        head = transaction.head(self.wiki)
        window = self._heat(head)
        return wire.heat_json(
            prov=wire.provenance_json(window, head=head),
            days=window.days[-span:],
            classes=heat.CLASSES,
            rows=tuple(wire.heat_row_json(
                db=row.db, host=row.host,
                counts={name: row.counts[name][-span:]
                        for name in heat.CLASSES})
                for row in window.rows))

    def database(self, principal: Principal, name: str) -> dict:
        """`GET /api/db?name=`: everything one database's page, incidents,
        error pages and journal say about it at one revision.

        Read entirely off the snapshot, so the incident rows are the committed
        wiki's and carry no `verdict`, `dirty` or `allowed`: those are working
        tree and live-monitoring facts, and this view reads a revision rather
        than a checkout. The queue is still where an operator acts.

        `errors` aggregates the `## Occurrences` rows to one line per code and
        is deliberately not windowed, unlike `fleet`'s `errors_30d`: an
        operator who has navigated to one database is asking what its history
        holds, not what the last month does.

        `page` is the standing `databases/<db>.md` rendered through
        `markdown.render`, or None when the snapshot holds no readable page
        for the database.

        A page the snapshot could not read leaves that incident's ownership at
        `hand` with no commit rather than failing the view, the same tolerance
        `readmodel.build` already applies. Requires READ_ROLE."""
        identity.require(principal, identity.READ_ROLE)
        head = transaction.head(self.wiki)
        snap = self._snapshot(head)
        if name not in snap.dbs:
            raise ApiError(404, "unknown_db",
                           f"no database {name!r} in the wiki at "
                           f"{snap.revision[:12]}")
        rows = []
        for inc in snap.db_incidents(name):
            info = snap.pages.get(inc.path)
            rows.append(wire.db_incident_row_json(
                inc,
                origin=info.origin if info else readmodel.Origin.HAND,
                commit=info.commit if info else None))
        tally: dict[str, tuple[int, str]] = {}
        for occ in snap.occurrences_of(db=name):
            count, last = tally.get(occ.code, (0, ""))
            tally[occ.code] = (count + 1, max(last, occ.day))
        by_code = sorted(tally.items())
        by_day = sorted(by_code, key=lambda row: row[1][1], reverse=True)
        return wire.db_json(
            prov=wire.provenance_json(snap, head=head),
            db=name,
            page=self._rendered(snap, snap.pages.get(f"databases/{name}.md")),
            incidents=tuple(rows),
            errors=tuple(wire.error_row_json(
                code=code, count=count, last_day=day,
                researched=(found.researched
                            if (found := snap.research.get(code)) else ""),
                resolved=len(snap.resolutions.get(code, ())))
                for code, (count, day) in by_day),
            journal=tuple(wire.journal_entry_json(entry)
                          for entry in snap.journals.get(name, ())))

    def page(self, principal: Principal, path: str) -> dict:
        """`GET /api/page?path=`: one wiki page rendered at one revision.

        The path is answered only when it ends `.md` and `snap.exists(path)`,
        which is to say the git inventory at the pinned revision holds it.
        Anything else is a 404 naming that revision, and no filesystem path is
        ever formed from the query, so `..`, an absolute path and every path
        outside the inventory are refused before there is a read to be done.
        That is the whole security argument of this endpoint.

        A curated page renders from `snap.text[path]`, already in memory. A
        page the inventory holds and the snapshot never read — a digest, which
        `readmodel.build` deliberately leaves closed, or a page that would not
        parse — is read lazily through `Tree.batch_at` at the *snapshot's*
        revision rather than at HEAD, so the body and the provenance triple
        describe one revision. A lazy read that comes back None is the same
        404: the inventory and the blob store disagreed, which is not an
        answer to give the operator.

        `type` and `title` come off `PageInfo` when the snapshot indexed the
        page, so the frontmatter-then-`# `-then-stem rule has one owner; a
        page it did not index has no type and falls back to its `# ` heading
        and then its stem.

        `links` is one `wire.link_json` per deep link the page's
        `evidence_ref` blocks resolve to, in block order. A page that carries
        none answers an empty list, which is a different statement from a page
        whose references this deployment cannot map.

        `changes` is what the operators did on the page's day, read off the
        digest sidecars `Snapshot.change_sidecars` names — a digest's own, or
        on a day's report every database's — in the same batch and at the
        same revision as the body. Requires READ_ROLE."""
        identity.require(principal, identity.READ_ROLE)
        head = transaction.head(self.wiki)
        snap = self._snapshot(head)
        missing = f"no page {path!r} in the wiki at {snap.revision[:12]}"
        if not (path.endswith(".md") and snap.exists(path)):
            raise ApiError(404, "no_such_page", missing)
        prov = wire.provenance_json(snap, head=head)
        indexed = self._rendered(snap, snap.pages.get(path))
        sidecars = snap.change_sidecars(path)
        if indexed is not None:
            tree = (transaction.Tree.batch_at(self.wiki, snap.revision,
                                              sidecars)
                    if sidecars else transaction.Tree.of({}))
            return wire.page_json(prov=prov, body=indexed,
                                  links=self._ref_links(snap.text[path]),
                                  changes=_page_changes(snap, tree, sidecars))
        tree = transaction.Tree.batch_at(self.wiki, snap.revision,
                                         (path, *sidecars))
        text = tree.read(path)
        if text is None:
            raise ApiError(404, "no_such_page", missing)
        rendered = markdown.render(text, links=self._links(snap),
                                   heading_prefix=HEADING_PREFIX)
        body = wire.page_body_json(
            path=path, type_="", title=rendered.title or Path(path).stem,
            rendered=rendered,
            origin=(readmodel.Origin.MACHINE if transaction.is_machine(path)
                    else readmodel.Origin.HAND),
            commit=None,
            backlinks=snap.backlinks.get(path, ()))
        return wire.page_json(prov=prov, body=body,
                              links=self._ref_links(text),
                              changes=_page_changes(snap, tree, sidecars))

    def search(self, principal: Principal, query: str) -> dict:
        """`GET /api/search?q=`: `readmodel.Snapshot.search`'s casefolded scan
        over the curated pages, field hits ahead of body hits.

        An empty or whitespace-only needle is a 400 and never a 200 with no
        hits: every page contains the empty string, so "nothing matched" would
        be a false statement about the wiki. The page keeps its empty state
        until the operator has typed something rather than asking for that
        answer. Requires READ_ROLE."""
        identity.require(principal, identity.READ_ROLE)
        if not query.strip():
            raise ApiError(400, "empty_query",
                           "a search needs something to look for; ?q= is not "
                           "a query that matched nothing")
        head = transaction.head(self.wiki)
        snap = self._snapshot(head)
        return wire.search_json(
            prov=wire.provenance_json(snap, head=head), query=query,
            hits=tuple(wire.hit_json(hit) for hit in snap.search(query)))

    def link_board(self, principal: Principal) -> dict:
        """`GET /api/links`: the operator's link board. Requires READ_ROLE.

        The board is read once, when the server is constructed, and not per
        request. `config/links.yaml` is configuration in the same sense as
        `config/dbwiki.yaml`: the pipeline never writes it, so there is no tick
        that can change it under a running portal and no staleness for a
        re-read to cure. An operator who edits it restarts the portal, exactly
        as they already do for a bind address or an advisory budget. Reading it
        per request would also move its refusals from startup, where a
        mistyped url stops the server with a message, to an operator's click.

        Spelled `link_board` and not `links` for the reason `resolver` is not
        spelled `links` either: `_links` on this class is the markdown link
        policy, and `self.links` beside `self._links` is how a caller reaches
        for the wrong one. The wire key stays `links`, because on the page
        there is only one kind of link board."""
        identity.require(principal, identity.READ_ROLE)
        return wire.links_json(self.board)

    def runs(self, principal: Principal) -> dict:
        """`GET /api/runs`: every run the three `.state/` logs still hold,
        newest first, with each run's seven-stage timeline on its row.

        No network call, and `health.assess` is deliberately not reused: it
        probes Elasticsearch and this very process, and a request thread makes
        no network call. Only `health`'s constants and `_hours_between` are
        borrowed, the cross-module private-name precedent `events.load_events`
        already sets with `health._read_jsonl`.

        No `revision`. This reads `.state/` and not the wiki, so there is no
        revision to name and nothing here is pinned to one.

        No cap on the run list either, and no page parameter. `coverage`
        already states the caps that bound the answer, and a second truncation
        rule would be a second story about what is missing.

        `freshness` merges the operator's `health.stage_stale_hours` over
        `health.DEFAULT_STAGE_STALE_HOURS`, the merge `assess` does, so a
        configured budget is honored on both surfaces; the evidence each task
        is proved by is `FRESHNESS_PROOF`'s. `backlog` is `events.backlog`,
        which owns the skip-versus-debt rule through `health.deliberate_skip`,
        and `pending` is the starts no finish ever answered.

        `trend` and `failures` are two more passes over the events already
        loaded here — one over the calendar, one over the categorised failures
        — so the aggregates and the run list are bounded by one `coverage`
        block and read no log twice. Requires READ_ROLE."""
        identity.require(principal, identity.READ_ROLE)
        loaded = events.load_events(self.state_dir)
        children = _children_by_run(loaded.events)
        rows = []
        for ev in loaded.events:
            if ev.kind is not Kind.RUN:
                continue
            kids = children.get(ev.run_id, ())
            rows.append(wire.run_row_json(
                head=_run_head(ev, [kid for kid in kids
                                    if kid.kind is Kind.DB_RUN]),
                stages=tuple(wire.stage_json(stage)
                             for stage in events.timeline(ev, kids))))
        hcfg = self.cfg.health
        thresholds = {**health.DEFAULT_STAGE_STALE_HOURS,
                      **(hcfg.get("stage_stale_hours") or {})}
        now = self.now()
        return wire.runs_json(
            generated_at=now,
            coverage=tuple(wire.coverage_json(c) for c in loaded.coverage),
            freshness=_freshness(loaded.events, now, thresholds),
            rows=tuple(rows),
            backlog=tuple(wire.backlog_row_json(digest=row["digest"],
                                                db=row["db"],
                                                decision=row["decision"])
                          for row in events.backlog(self.state_dir)),
            pending=tuple(wire.pending_row_json(run_id=ev.run_id,
                                                command=ev.command,
                                                started=ev.at)
                          for ev in events.pending(loaded.events)),
            trend=tuple(wire.day_json(day)
                        for day in events.days(loaded.events)),
            failures=tuple(wire.failure_group_json(group) for group in
                           alerts.recurring(loaded.events, self.state_dir)))

    def agents(self, principal: Principal, hours: str) -> dict:
        """`GET /api/agents?hours=`: what the agent loops did over the last
        `hours`, one row per tick, and which incident pages each tick wrote.

        This is the one endpoint that joins the two records the workbench
        keeps. `.state/agent_runs.jsonl` says which model ran which stage, for
        how long and at what token cost; the wiki's history says which pages
        the same tick committed, through the `Run-ID:` trailer the read model
        turns into `touches_by_run`. Neither log knows about the other, and
        the trailer is the only key they share.

        `hours` arrives as the raw query string, the `heat` precedent: the
        query is the boundary and the parse belongs on this side of it. Empty
        means `AGENT_DEFAULT_HOURS`. Anything else that is not an integer in
        1..`AGENT_MAX_HOURS` is a 400 `bad_hours` rather than a clamp, because
        a page asking for a year has a bug and silently drawing thirty days
        would hide it. The ceiling is the touch join's own window: past it the
        stages would keep arriving and the incidents under them would stop,
        which reads as ticks that touched nothing.

        The window is applied to the ledger by string comparison. Every `at`
        on both sides is second-granularity UTC in one spelling, which sorts
        lexically, so the alternative is parsing every line of a capped log to
        answer a question the characters already answer.

        A tick is grouped by `run_id`, which is a grouping and never an
        identity (`events`' own rule), and that is exactly what is wanted
        here: an analyst fold-in replaying an old id is the same tick's work
        landing later, and the operator asking what the loop did wants it on
        the tick that caused it.

        `models` is `events.by_model`, which groups through `stats.group`,
        over the window's stages counted once per `event_id` (`_once`), so a
        model's stage count is what `dbwiki stats --since <hours>h --by
        model` counts for it over the same lines (incident-workbench #31).

        Both halves may be short and each says so. `coverage` bounds the
        stages, the provenance triple pins the incidents, and a tick whose
        RUN line has aged out keeps its stages under an empty `command`.

        Requires READ_ROLE, and makes no network call, for `runs`' reason."""
        identity.require(principal, identity.READ_ROLE)
        refusal = (f"hours must be an integer in 1..{AGENT_MAX_HOURS}, "
                   f"not {hours!r}")
        try:
            span = AGENT_DEFAULT_HOURS if not hours else int(hours)
        except ValueError:
            raise ApiError(400, "bad_hours", refusal) from None
        if not 1 <= span <= AGENT_MAX_HOURS:
            raise ApiError(400, "bad_hours", refusal)
        now = self.now()
        head = transaction.head(self.wiki)
        snap = self._snapshot(head)
        loaded = events.load_events(self.state_dir)
        since = _stamp(readmodel.instant(now) - dt.timedelta(hours=span))
        inside = _once([ev for ev in loaded.events
                        if ev.kind is Kind.STAGE and ev.at >= since])
        commands = {ev.run_id: ev.command for ev in reversed(loaded.events)
                    if ev.kind in (Kind.RUN, Kind.START) and ev.command}
        ticks = []
        resolver = self.resolver
        for run_id, stages in _stages_by_run(inside).items():
            ticks.append(wire.tick_json(
                run_id=run_id,
                command=commands.get(run_id, ""),
                started=stages[0].at,
                finished=stages[-1].at,
                stages=tuple(_agent_stage(resolver, ev) for ev in stages),
                incidents=_tick_incidents(
                    snap.touches_by_run.get(run_id, ()), snap),
                totals=wire.totals_json(events.totals(stages))))
        ticks.sort(key=lambda tick: (tick["started"], tick["run_id"]),
                   reverse=True)
        return wire.agents_json(
            prov=wire.provenance_json(snap, head=head),
            generated_at=now,
            window_hours=span,
            coverage=tuple(wire.coverage_json(c) for c in loaded.coverage),
            ticks=tuple(ticks),
            models=tuple(wire.model_roll_json(roll)
                         for roll in events.by_model(inside)),
            totals=wire.totals_json(events.totals(inside)))

    def run(self, principal: Principal, run_id: str) -> dict:
        """`GET /api/run?id=`: one run's timeline and what each database did
        inside it.

        A `run_id` with no RUN event falls back to a START with that id, and
        neither is a 404: `.state/` drops its oldest lines on write, so an id
        older than the caps is gone rather than wrong.

        The per-database rows are one per DB_RUN child, sorted by `event_id`.
        Their reason *codes* are always the child's own `reason_codes`. The
        *evidence* is joined from `events.decisions` only when this run is the
        newest run touching that digest, because the ledger holds one
        `last_decision` per digest and decorating an older run with it would
        attribute a later tick's reasoning to an earlier run.

        `usage` is the `Usage` of the ingest STAGE line for the same database,
        or None, never a zero. `links` is the Langfuse session for this run and
        its ELK history, in that order, built from config, so an `available`
        link means configured rather than verified. `totals` measures this
        run's STAGE children and `compare` carries the previous run of the
        same command beside it, head and totals.

        Requires READ_ROLE, and makes no network call, for `runs`' reason."""
        identity.require(principal, identity.READ_ROLE)
        loaded = events.load_events(self.state_dir)
        found = next((ev for ev in loaded.events
                      if ev.kind is Kind.RUN and ev.run_id == run_id), None)
        if found is None:
            found = next((ev for ev in loaded.events
                          if ev.kind is Kind.START and ev.run_id == run_id),
                         None)
        if found is None:
            raise ApiError(404, "unknown_run",
                           f"no run {run_id!r} in .state/, whose logs drop "
                           f"their oldest lines at the cap: an id older than "
                           f"the caps is gone, not wrong")
        kids = [ev for ev in loaded.events if ev.run_id == run_id
                and ev.kind in (Kind.DB_RUN, Kind.STAGE)]
        dbs = sorted((ev for ev in kids if ev.kind is Kind.DB_RUN),
                     key=lambda ev: ev.event_id)
        stages = [ev for ev in kids if ev.kind is Kind.STAGE]
        newest = _newest_by_digest(loaded.events)
        ledger = events.decisions(self.state_dir)
        rows = []
        for child in dbs:
            key = events.digest_key(child.digest_path)
            evidence = ({reason.code: reason.evidence
                         for reason in ledger.get(key, ())}
                        if newest.get(key) == run_id else {})
            ingest = next((ev for ev in stages if ev.task == "ingest"
                           and ev.db == child.db), None)
            rows.append(wire.db_run_json(
                event_id=child.event_id,
                db=child.db,
                decision=child.decision,
                reasons=tuple(wire.reason_json(code=code,
                                               evidence=evidence.get(code, {}))
                              for code in child.reason_codes),
                outcome=child.outcome,
                error=child.error,
                error_category=child.error_category,
                model_tier=child.model_tier,
                commit=child.commit,
                digest=child.digest_path,
                digest_page=_digest_page(child.digest_path),
                usage=wire.usage_json(ingest.usage if ingest else None)))
        resolver = self.resolver
        tools = (resolver.trace(run_id=run_id),
                 resolver.run_history(run_id=run_id))
        agents = tuple(_agent_stage(resolver, ev) for ev
                       in sorted(stages, key=lambda ev: (ev.at, ev.event_id)))
        before = events.previous_run(loaded.events, found)
        return wire.run_json(run=_run_head(found, dbs),
                             stages=tuple(wire.stage_json(stage) for stage
                                          in events.timeline(found, kids)),
                             dbs=tuple(rows), agents=agents,
                             links=tuple(wire.link_json(t) for t in tools),
                             totals=wire.totals_json(events.totals(kids)),
                             compare=self._compare(loaded.events, before))

    def _compare(self, stream: Sequence[RunEvent],
                 before: RunEvent | None) -> dict | None:
        """The previous run's head and totals, or None when the logs hold no
        earlier run of this command.

        Its children are grouped by `run_id` exactly as this run's are, so the
        two columns are the same arithmetic over the same grouping and the
        compare column agrees with that run's own screen."""
        if before is None:
            return None
        kids = [ev for ev in stream if ev.run_id == before.run_id
                and ev.kind in (Kind.DB_RUN, Kind.STAGE)]
        return wire.compare_json(
            head=_run_head(before, [ev for ev in kids
                                    if ev.kind is Kind.DB_RUN]),
            totals=wire.totals_json(events.totals(kids)))

    def _links(self, snap: readmodel.Snapshot) -> markdown.SnapshotLinks:
        """The link policy for rendering one snapshot's prose: `resolve_link`
        over every `.md` path the revision holds, so a `[[link]]` renders
        exactly as lint judges it, and `Snapshot.exists` for a relative
        href."""
        pages = frozenset(p for p in snap.inventory if p.endswith(".md"))
        return markdown.SnapshotLinks(
            exists=snap.exists, resolve=lambda t: lint.resolve_link(t, pages))

    def _rendered(self, snap: readmodel.Snapshot,
                  info: readmodel.PageInfo | None) -> dict | None:
        """`wire.page_body_json` for a page the snapshot indexed, None for
        one it did not. Its text is already in memory and `PageInfo` already
        applied the title rule, so nothing here reads git or re-derives a
        name."""
        if info is None:
            return None
        return wire.page_body_json(
            path=info.path, type_=info.type, title=info.title,
            rendered=markdown.render(snap.text[info.path],
                                     links=self._links(snap),
                                     heading_prefix=HEADING_PREFIX),
            origin=info.origin, commit=info.commit,
            backlinks=snap.backlinks.get(info.path, ()))

    def _snapshot(self, revision: str) -> readmodel.Snapshot:
        """The read model at `revision`, through the one object on this
        server that mutates. A caller that loses the rebuild race gets the
        previous snapshot, whose `revision` the response carries beside
        `head`; the cache documents why that is the honest answer."""
        return self.cache.get(
            revision,
            lambda rev: readmodel.build(self.wiki, rev, now=self.now))

    def _heat(self, revision: str) -> "heat.Heat":
        """The whole heat window at `revision`, through its own cache.

        The return annotation is a string for the reason the class docstring
        gives about `runner`: this class body binds the name `heat` to the
        endpoint method above, and an evaluated annotation here would resolve
        `heat.Heat` against that function instead of against the module. Bodies
        are unaffected, so `heat.build` below is the module.

        It takes the revision rather than a snapshot and asks `_snapshot` for
        one itself, so a caller cannot hand it a snapshot built at some other
        revision than the one it is about to label the window with. Losing the
        snapshot's rebuild race therefore builds the window against the
        previous revision as well, and the two answers stay one consistent
        pair rather than a mix.

        The window is the full `heat.WINDOW_DAYS` whichever span the request
        asked for; `heat` slices. That is what makes the toggle free.

        A caller that loses this cache's own race gets the previous window,
        whose `revision` the response carries beside `head`, exactly as
        `_snapshot`'s loser does."""
        return self.heat_cache.get(
            revision,
            lambda rev: heat.build(self.wiki, self._snapshot(rev),
                                   now=self.now))

    def health(self) -> dict:
        """`GET /api/health`, unauthenticated, for `dbwiki health`'s probe:
        `{"ok": true, "bind", "revision": head(wiki), "actor", "push"}`. No
        lock, one git call, so it still answers while a tick holds the lock.
        Fails (500) only when the wiki is not a repository, which is what the
        probe should surface.

        The five keys are spelled here rather than in `wire`, which has no
        `health_json`: this is the one probe body, it is not the page's
        dialect, and a wire function with one caller and no domain value
        behind it would be a name for a dict literal."""
        try:
            revision = transaction.head(self.wiki)
        except RuntimeError as e:
            raise ApiError(500, "wiki_unreadable",
                           f"the wiki at {self.wiki} is not a readable "
                           f"repository: {e}") from None
        try:
            actor = self.provider.identify(
                RequestContext("", {}, "")).actor.email
        except Unidentified:
            actor = ""
        return {"ok": True, "bind": self.bind, "revision": revision,
                "actor": actor, "push": self.cfg.portal["push"]}

    def preview(self, principal: Principal, slug: str,
                req: WriteRequest) -> Response:
        """`POST /api/incidents/{slug}/preview`. Lock-free, writes nothing,
        records no run event: a preview is not a run.

        `base = req.base or head(wiki)`; `at = req.at or self.now()`. The
        server mints both and the response carries both; the page holds them
        for the life of the form and echoes them to `commit`. `at` survives a
        re-preview against a moved base, because `at` is the record's
        identity and a fresh one would be a second record.

        `_built` runs the role gate then `incident_action.decode`; the
        proposal comes from `Action.proposal(Tree.at(wiki, base))`, which
        refuses a tree that is not the asserted base. Then
        `transaction.preview`. The response carries `head` beside `base`, so a
        client holding a stale base sees it before it commits; `cli` is
        `incident_action.retry_line`, the pasteable fallback for exactly this
        transaction; `requires` names `CONFIRMATION`'s field while it is still
        empty, so the confirm screen knows to ask.

        Always 200 once it built: `blocked` is a field of the answer, not a
        status, because a blocked preview is exactly what the operator asked
        to see."""
        head = transaction.head(self.wiki)
        base = req.base or head
        at = req.at or self.now()
        path, tree, inc = self._locate(slug, base)
        action, proposal = self._built(principal, slug, tree, req, at)
        pv = transaction.preview(self.wiki, proposal)
        field = _unwritten(action.command)
        return Response(200, wire.preview_json(
            action, proposal, pv, head=head,
            status_after=str(lifecycle.next_status(inc.status,
                                                   action.command)),
            principal=principal,
            requires=(field,) if field is not None else ()))

    def commit(self, principal: Principal, slug: str,
               req: WriteRequest) -> Response:
        """`POST /api/incidents/{slug}/commit`. The one place the wiki is
        written from HTTP.

        Refuses before building: `base` or `at` absent -> 400
        (`preview_first`), because the published bytes must be the previewed
        bytes. Then the same gate and decode as preview, so the proposal is
        byte-identical to the previewed one: `build` is pure over (base,
        command, actor, at). `CONFIRMATION`'s field empty -> 422
        (`confirmation_required`, naming the field), checked *after* `_built`'s
        role gate, so a caller who may not resolve learns "forbidden" first.

        The write is `incident_action.publish(..., lock_wait_s=
        cfg.portal["lock_wait_s"], push=cfg.portal["push"],
        surface="portal")`, which owns the lock, the audit and the out-of-lock
        push. This method only maps its answer through `HTTP_OF` and
        `wire.outcome_json`.

        Twice with the same body: `BaseMoved` naming the sha that already
        holds it, or adoption of its own debris and a normal `Committed`.
        Nothing here retries."""
        if not (req.base and req.at):
            raise ApiError(400, "preview_first",
                           "commit needs the base and at a preview chose",
                           base=None, at=None)
        _, tree, _ = self._locate(slug, req.base)
        action, proposal = self._built(principal, slug, tree, req, req.at)
        field = _unwritten(action.command)
        if field is not None:
            raise ApiError(422, "confirmation_required",
                           f"a resolve records what is still not safe: write "
                           f"it in {field}, or say none identified",
                           field=field, base=req.base, at=req.at)
        published = incident_action.publish(
            self.wiki, self.state_dir, action, proposal,
            lock_wait_s=self.cfg.portal["lock_wait_s"],
            push=self.cfg.portal["push"], surface="portal")
        return Response(HTTP_OF[type(published)], wire.outcome_json(
            published, base=action.base, at=action.at,
            retry_after_s=self.cfg.portal["lock_wait_s"]))

    def tools(self, principal: Principal, slug: str) -> dict:
        """`GET /api/incidents/{slug}/tools`: every advisory row, what a click
        would read right now, and what the window has already spent.

        Packs for real (`advisory.Runner.manifest`) and reaches no model, so
        the panel can state the size of the material and the state of the
        ceiling before the operator spends anything. That is the whole point
        of the endpoint: an advisory click is the one button on this
        workbench that costs money, and a button that cannot say what it would
        do is a button nobody should press.

        Read at HEAD through `_target`, so the manifest and the run a click
        then starts describe one revision. Requires READ_ROLE."""
        identity.require(principal, identity.READ_ROLE)
        target = self._target(slug, transaction.head(self.wiki))
        rows = self._runner().manifest(target)
        return wire.advisory_tools_json(
            slug=slug, evidence_revision=target.revision,
            rows=tuple(wire.advisory_tool_json(row) for row in rows))

    def advisory_start(self, principal: Principal, slug: str,
                       req: wire.ToolRequest) -> Response:
        """`POST /api/incidents/{slug}/advisory`: admit one run and answer
        202 with its `queued` record, which already carries the manifest the
        worker packed. The page then polls `GET /api/advisory?run=`.

        The order of the first three steps is the endpoint's security
        argument, and it is checked by a test. DRAFT_ROLE first, so a caller
        who may not run a tool learns "forbidden" rather than which tools this
        deployment has turned off or which ids it does not have; then the row,
        whose own `role` is required next, because a row may ask for more than
        DRAFT_ROLE and the cheaper gate must not be the only one; then the
        question against `advisory.asks`, because a row that takes no question
        and a question nobody asked are both malformed asks and neither should
        read evidence or reach a model to be told so; then the target. No
        model is reached on any refusal, and no evidence is even read.

        `at` is the caller's or the server's: `advisory.run_id_for` hashes it,
        so a page that re-posts the same click converges on the run it already
        started instead of paying twice, and a page that sends none is minting
        a new click. Never writes the wiki and never takes the lock: an
        advisory run's whole authority is its own file under `.state/`."""
        identity.require(principal, identity.DRAFT_ROLE)
        runner = self._runner()
        spec = self._spec(runner, req.tool)
        identity.require(principal, spec.role)
        asks = advisory.asks(spec)
        if bool(req.question) != asks:
            wanted = ("asks a question of the material and none was sent"
                      if asks else "takes no question")
            raise ApiError(400, "bad_question", f"{spec.id} {wanted}")
        target = self._target(slug, transaction.head(self.wiki),
                              question=req.question)
        try:
            run = runner.start(spec, target, at=req.at or self.now())
        except advisory.Refused as refusal:
            raise self._refused(refusal) from None
        return Response(202, wire.advisory_run_json(run))

    def advisory(self, principal: Principal, run_id: str) -> dict:
        """`GET /api/advisory?run=`: one run as this process reads it, which
        is `advisory.Runner.read` and nothing else.

        A run the runner does not hold is a 404 naming the retention, the
        `run` precedent: run files are pruned to the newest
        `advisory.Settings.retain`, so an id older than that is gone rather
        than wrong. Requires READ_ROLE, not the tool's role: the answer is
        already paid for, and an operator who may read the incident may read
        what was said about it."""
        identity.require(principal, identity.READ_ROLE)
        runner = self._runner()
        # the id becomes `advisory/{run_id}.json`; anything but a plain id is
        # a path, and a path is the same miss a pruned run is (issue 09)
        run = runner.read(run_id) if validate.is_safe_id(run_id) else None
        if run is None:
            raise ApiError(404, "unknown_run",
                           f"no advisory run {run_id!r}; the runner keeps the "
                           f"{runner.settings.retain} newest runs and drops "
                           f"the rest, so an older id is gone, not wrong")
        return wire.advisory_run_json(run)

    def inbox(self, principal: Principal) -> dict:
        """`GET /api/inbox`: the weekly reviews `.state/review/reviews/` still
        holds, newest first, one summary row each.

        No findings on a row. The inbox is a list of weeks, and a list that
        already carried every finding would leave the review screen nothing to
        fetch and would grow with `cap` times `keep_reviews`.

        No `revision`. This reads `.state/` and not the wiki, the `runs`
        rule. An empty inbox is an honest empty list and never a 503: the
        endpoint reads files that either exist or do not, and a deployment
        that has never run `dbwiki review` is not an outage.

        A file this reader cannot understand is left off the list rather than
        failing it, `monitoring.read_facts`' tolerance and for its reason: the
        realistic case is not corruption but the one `review._check_version`
        exists for, a newer cron stage publishing and the portal rolling back,
        and one such week must not take down every other week with it. Opening
        that week by name still raises, because there the file is the answer.

        Requires READ_ROLE."""
        identity.require(principal, identity.READ_ROLE)
        rows = []
        for review_id in review.list_reviews(self.state_dir):
            found = self._readable(review_id)
            if found is not None:
                rows.append(wire.inbox_row_json(found))
        return wire.inbox_json(generated_at=self.now(), rows=tuple(rows))

    def review(self, principal: Principal, review_id: str) -> dict:
        """`GET /api/review?id=`: one week's findings, with what the operator
        has already said about each merged in from `acks.json`.

        A review the state directory does not hold is a 404 naming the
        retention, the `run` and `advisory` precedent: `review.run` prunes to
        `keep_reviews`, so an older id is gone rather than wrong.

        The merge happens here at read and the review file is never rewritten
        by this endpoint, which is what makes `review.py`'s two-file split
        hold: the cron stage owns `state.json` and `reviews/<id>.json`, the
        portal owns `acks.json`, and no lock stands between them.

        Requires READ_ROLE: an operator who may read the wiki may read what
        the week selected out of it."""
        identity.require(principal, identity.READ_ROLE)
        # the id becomes `reviews/{review_id}.json`; only `YYYY-Www` names a
        # week, and anything else is the same miss a pruned week is, never a
        # read of whatever file the id spells (issue 09)
        found = (review.load_review(self.state_dir, review_id)
                 if _is_review_id(review_id) else None)
        if found is None:
            keep = review.Rules.resolve(self.cfg).keep_reviews
            raise ApiError(404, "unknown_review",
                           f"no review {review_id!r}; the newest {keep} are "
                           f"kept and the rest are pruned, so an older id is "
                           f"gone, not wrong")
        return wire.review_json(found, acks=review.load_acks(self.state_dir))

    def inbox_act(self, principal: Principal,
                  req: wire.InboxRequest) -> Response:
        """`POST /api/inbox`: acknowledge one finding, or suppress it for a
        number of days.

        The order of the first two steps is the endpoint's security argument
        and is checked by a test, the `advisory_start` shape. DRAFT_ROLE
        first, so a caller who may not write learns "forbidden" rather than
        which fingerprints this deployment holds; then the fingerprint, across
        every review still held rather than only the newest, because an
        operator can be reading last week's; then the dispatch.

        The body's *shape* is settled before any of that, in
        `InboxRequest.from_json`, which is the `advisory_start` order again. A
        400 there names a key the caller wrote and says nothing about this
        deployment, which is what makes it safe ahead of the gate.

        DRAFT_ROLE and not `identity.ROLE_OF`, which is keyed by lifecycle
        command type: this writes `.state/` and never the wiki, so there is no
        command to look up.

        `review.acknowledge` and `review.suppress` are the only writers of
        `acks.json` and are already idempotent and audited, so a second
        identical POST answers 200 with the same body, writes no new bytes and
        appends no audit row. Nothing here retries and nothing here takes the
        lock."""
        identity.require(principal, identity.DRAFT_ROLE)
        if not self._published(req.fingerprint):
            raise ApiError(404, "unknown_item",
                           f"no finding {req.fingerprint!r} in any review this "
                           f"workbench holds; reviews are pruned to the "
                           f"newest {review.Rules.resolve(self.cfg).keep_reviews}"
                           f", so an older fingerprint is gone, not wrong")
        now = req.at or self.now()
        actor = principal.actor.email
        if req.action == "acknowledge":
            item = review.acknowledge(self.state_dir, req.fingerprint,
                                      actor=actor, now=now)
        else:
            item = review.suppress(self.state_dir, req.fingerprint,
                                   actor=actor, now=now, days=req.days)
        return Response(200, wire.inbox_act_json(fingerprint=req.fingerprint,
                                                 ack=wire.ack_json(item)))

    def _published(self, fingerprint: str) -> bool:
        """Whether any review this deployment still holds selected that
        finding. Every review and not just the newest: an operator reading
        last week's page is acting on a fingerprint this week may have
        resolved, and refusing that would make the older screen unusable.

        A week this reader cannot understand is skipped, `inbox`'s tolerance:
        one unreadable file must not stop an operator acknowledging a finding
        in a good one."""
        for review_id in review.list_reviews(self.state_dir):
            if fingerprint in wire.review_fingerprints(
                    self._readable(review_id) or {}):
                return True
        return False

    def _readable(self, review_id: str) -> dict | None:
        """One published review, or None where the file is absent or is one
        this code cannot read. `inbox` and `_published` say why the second
        case is a skip and not a refusal; `review` deliberately does not use
        this, because there the file is the answer being asked for."""
        try:
            return review.load_review(self.state_dir, review_id)
        except (ValueError, OSError):
            return None

    def _runner(self) -> "advisory.Runner":
        """The runner, or the 503 every advisory endpoint answers when this
        deployment has none or has switched the block off. One check for both
        cases: to the operator they are one outage, and the page reads the
        stable `error` rather than the sentence."""
        if self.runner is None or not self.runner.settings.enabled:
            raise ApiError(503, "advisory_unavailable",
                           "the advisory tools are off; set advisory."
                           "enabled: true to turn them on")
        return self.runner

    def _spec(self, runner: "advisory.Runner",
              tool: str) -> "advisory.ToolSpec":
        """The resolved row, or the refusal its absence maps to: 404
        `unknown_tool` naming the tools this deployment has, 503
        `tool_disabled` for a row the config turned off. A disabled row is a
        deployment decision and not a client error, which is why it is the
        same status the whole block's switch answers."""
        try:
            return runner.spec(tool)
        except advisory.UnknownTool:
            known = ", ".join(sorted(runner.settings.tools))
            raise ApiError(404, "unknown_tool",
                           f"no advisory tool {tool!r}; the tools are "
                           f"{known}") from None
        except advisory.ToolDisabled:
            raise ApiError(503, "tool_disabled",
                           f"{tool} is off in this deployment's advisory "
                           f"block") from None

    def _refused(self, refusal: "advisory.Refused") -> ApiError:
        """`advisory.Refused` as the 429 the page renders. `busy` is a full
        slot, which is a wait: it carries `retry_after_s`, the row's timeout,
        because that is how long the run ahead of it may still take. Every
        other reason is a ceiling, which is not a wait at all: it carries the
        stable `reason` and the spend and the ceiling behind it, so the page
        says what was reached rather than inviting a retry that would be
        refused again.

        Both carry `run_id`. A refusal writes a `refused` record and spends
        nothing, and naming it is what makes a click that cost nothing still
        addressable."""
        if refusal.reason == "busy":
            return ApiError(429, "advisory_busy",
                            "every advisory slot on this workbench is busy; "
                            "the run ahead of this one has not finished",
                            retry_after_s=refusal.retry_after_s,
                            run_id=refusal.run.run_id)
        return ApiError(429, "ceiling_reached",
                        f"this tool has reached its {refusal.reason} ceiling "
                        f"for the window; nothing was spent",
                        reason=refusal.reason,
                        ceiling=wire.advisory_ceiling_json(
                            budget=refusal.ceiling, spent=refusal.spend,
                            reason=refusal.reason),
                        run_id=refusal.run.run_id)

    def _target(self, slug: str, revision: str, *,
                question: str = "") -> "advisory.Target":
        """What both advisory endpoints ask about: the incident at `revision`,
        the snapshot at that revision and the monitoring facts, pinned into
        one frozen value a reader cannot look past.

        `question` is the operator's free text, `""` for the manifest, which
        prices what a click would read and is not the click.

        One builder because the manifest and the run a click then starts must
        read the same evidence: two spellings of this would be two answers to
        "what would this cost", and the operator is shown the first and
        charged for the second."""
        _, tree, inc = self._locate(slug, revision)
        return advisory.Target.incident(
            inc, tree=tree, revision=revision,
            snapshot=self._snapshot(revision),
            facts=monitoring.read_facts(self.state_dir, inc.slug),
            question=question)

    def _locate(self, slug: str, revision: str) -> tuple[str, Tree, Incident]:
        """`(path, tree, incident)` at `revision`. Raises ApiError 404
        (`no_such_incident`) when the revision holds no such page, or 409
        (`uncommitted_page`) when the working tree holds one and the revision
        does not, with the CLI recipe that commits it. The slug reaches
        `incidents.incident_path`, whose `Path(slug).name` is the traversal
        boundary a URL segment leans on.

        The recipe names `Path(path).stem`, the canonical slug, and not the
        URL segment it came from, so a segment like `../../etc/passwd` cannot
        reach the operator's shell as itself."""
        path = incidents.incident_path(slug)
        tree = Tree.at(self.wiki, revision)
        text = tree.read(path)
        if text is None:
            if not (self.wiki / path).is_file():
                raise ApiError(404, "no_such_incident",
                               f"no incident page at {path}")
            wiki, rel = shlex.quote(str(self.wiki)), shlex.quote(path)
            message = shlex.quote(f"incident: add {Path(path).stem}")
            raise ApiError(
                409, "uncommitted_page",
                f"{path} is in the working tree but not in "
                f"{revision[:12]}; the workbench builds from a revision",
                paths=[path],
                cli=f"git -C {wiki} add {rel} && "
                    f"git -C {wiki} commit -m {message}")
        return path, tree, incidents.read_incident(text, path)

    def _built(self, principal: Principal, slug: str, tree: Tree,
               req: WriteRequest,
               at: str) -> tuple[Action, transaction.Proposal]:
        """The shared half of preview and commit, in this order:
        `incident_action.VERBS[req.verb]` (unknown -> 422 `bad_verb`);
        `require(principal, ROLE_OF[type])` (-> 403 `forbidden`, naming the
        role); `incident_action.decode`; `Action.proposal(tree)`. The gate
        precedes the decode so a viewer learns "forbidden", not "your field is
        wrong". Every `ValueError` from either step maps through
        `BUILD_ERRORS` to a 422 carrying the exception's message and, for a
        `FieldError`, its field.

        Returns the proposal beside the action because the build's refusals
        need that same mapping, and a caller handed the action alone would
        have to build the proposal a second time to publish it.

        `identity.Forbidden` is a `PermissionError`, not a `ValueError`, so it
        passes the mapping untouched and reaches the server as its own 403."""
        command_type = incident_action.VERBS.get(req.verb)
        if command_type is None:
            raise ApiError(422, "bad_verb",
                           f"unknown verb {req.verb!r}; one of "
                           + ", ".join(incident_action.VERBS))
        identity.require(principal, identity.ROLE_OF[command_type])
        try:
            action = incident_action.decode(
                slug=slug, verb=req.verb, fields=req.fields,
                actor=principal.actor, base=tree.revision, at=at)
            return action, action.proposal(tree)
        except ValueError as e:
            error = next(name for cls, name in BUILD_ERRORS
                         if isinstance(e, cls))
            extra = {"field": e.field} if isinstance(e, FieldError) else {}
            raise ApiError(422, error, str(e), **extra) from None

    def _closure(self, inc: Incident, revision: str) -> dict | None:
        """`wire.closure_json(monitoring.closure_case(read_facts(...),
        revision=revision))` for a monitoring incident; None otherwise.
        Missing facts are normal — no tick has run since monitoring started —
        so None, not an error."""
        if inc.status is not Status.MONITORING:
            return None
        facts = monitoring.read_facts(self.state_dir, inc.slug)
        if facts is None:
            return None
        return wire.closure_json(
            monitoring.closure_case(facts, revision=revision), facts)
