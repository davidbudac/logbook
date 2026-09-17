# ADR-0003 — One incident status vocabulary, and human intent as dated action sections

Date: 2026-08-30
Status: accepted

## Context

The pipeline detects notable database-log windows and opens incidents well.
It has no controlled way for a DBA to record what they did about one, or to
say that it is over. Today they hand-edit curated Markdown in the `wiki/`
repo and commit it like any other change.

Two things are missing, and they are different problems.

**There is no agreed status vocabulary in the code.** The schema is not the
problem. `wiki/AGENTS.md` already documents
`status: open|monitoring|resolved`, and all 25 live incident pages carry
`status: open`. The drift is that four readers decide "is this incident
active" three ways.

| Reader | Rule today |
|---|---|
| `structured._unresolved` (`src/dbwiki/structured.py`, lines 75-87) | anything but the literal `resolved` is active |
| `daily_html.open_incidents` (`src/dbwiki/daily_html.py`, lines 106-127) | delegates to `structured.all_open_incidents` |
| `research._open_incident_codes` (`src/dbwiki/research.py`, lines 119-127) | requires the literal `open` |
| `research_offload._incident_state` (`src/dbwiki/research_offload.py`, lines 119-130) | `resolved` is resolved, `open` and `monitoring` are open, anything else is neither |

So a `monitoring` incident silently loses research priority, and a typo such
as `status: opne` reads as active in one place, inactive in another, and
neither in a third. Nothing catches it. `lint.RULES` has six rules and none
of them looks at incident status, so a misspelled value publishes clean.
`daily_html._incident_card`, line 508, then badges the page `Open incident`
whatever the status says.

**There is no durable place for human intent.** `wiki/AGENTS.md` names a
`## Resolution history` section on error pages. It exists in 0 of 64
error pages and no code path fills it; outside the Stage 0 sketches its only
mention in `src/` is the ingest prompt line forbidding external claims there
(`src/dbwiki/orchestrate.py`, line 1117). `## Actions`, `## Timeline` and
`## Monitoring` appear nowhere in the wiki or the code. The de facto incident
timeline is the run of `## Update <day>` blocks the structured ingest writer
maintains, which is machine narration of evidence, not a record of what a
person decided or why.

A durable place for intent is also hard to hold. The structured writer
replaces `## ` sections by heading regex and, on the match path, collapses
runs of three newlines across the whole page. A human section survives
re-ingest only if no writer regex can match its heading and its body carries
nothing the collapse would rewrite.

## Decision

One status vocabulary, one owner of what an incident page may say, and one
state machine that decides which transition writes which files. Human intent
lives in the incident page itself, in a section shape the ingest writer
cannot match.

### Status vocabulary

`incidents.Status` is a `StrEnum` with three members and no others.

```python
class Status(StrEnum):
    OPEN = "open"
    MONITORING = "monitoring"
    RESOLVED = "resolved"


ACTIVE = frozenset({Status.OPEN, Status.MONITORING})
```

`resolved` is terminal. `is_active(status)` is the one predicate, and
`parse_status(raw)` is the one place a frontmatter string meets the
vocabulary. There is no `closed`, and no page needs migrating.

Reading an off-vocabulary value fails loud rather than quiet. The page reads
as `OPEN` and `Incident.unknown_status` carries the raw text, so an attention
queue never drops a page it cannot classify. Lint turns that same value into
a blocking `incident-status-invalid` error, and `lifecycle.build` refuses to
transition the page until a human repairs it.

### The action record

Every command a human issues produces exactly one `ActionRecord`, written
into the incident page as its own section.

````markdown
## Action 2026-08-30T14:22:10Z

```yaml
kind: resolve
actor: dba@example.com
intent: confirm the transport failure is over
summary: restarted the standby listener, then watched a full day of shipping
status_after: resolved
outcome: succeeded
```

free-text notes
````

The heading carries `at`, an ISO `YYYY-MM-DDTHH:MM:SSZ` timestamp.
`incidents.ACTION_HEAD_RE` captures it back out, so `at` is not repeated
inside the fence. It is the record's identity: the heading, the `log.md`
timestamp, and what makes a replayed transaction converge on identical bytes
instead of a second record.

The fenced `yaml` block carries the rest of the record, in field order, empty
optional fields omitted:

| Key | Meaning |
|---|---|
| `kind` | one of `record-action`, `start-monitoring`, `extend-monitoring`, `resolve`, `reopen` |
| `actor` | the operator's email |
| `intent` | why the operator did it |
| `summary` | what they did |
| `status_after` | the status the transaction leaves behind |
| `ticket` | change or ticket reference, optional |
| `outcome` | `pending`, `succeeded`, `failed`, or `rejected`; `pending` is the honest default right after an action |
| `rollback` | how to undo it, optional |
| `window` | the monitoring window this action set, optional |
| `evidence` | digest paths now, `evidence_ref` records after phase 2.6 |

`kind`, `actor`, `intent`, `summary` and `status_after` are always written,
and so is `outcome`, because `pending` is a value rather than an absence. The
other four appear only when set. A key outside that list is rejected rather
than ignored, so a mistyped `rollbak:` fails loudly instead of quietly
dropping the operator's undo instructions.

Free-text `notes` follow the closing fence. They are the only multi-line
field.

`ActionRecord.__post_init__` validates once, at that boundary, and nothing
downstream re-checks. It rejects a malformed `at`, an unknown `kind`, an
empty `actor`, `intent` or `summary`, any non-`notes` field spanning more
than one line, an `evidence` entry that is not a `digests/<db>/<day>` path,
and notes containing a `## ` line, a fence line, or a run of three newlines.
Those last three are not style rules. Each one would let a later ingest
split, truncate, or silently rewrite the record.

`render_action` and `parse_action` are inverses. `parse(render(x)) == x`
holds for every record that exists, because the constructor also strips the
surrounding whitespace off `notes`: there is one canonical spelling of a
record, so a replay compares equal instead of merely equivalent.
`append_action` replaces an existing section with the same heading and
appends otherwise, so running it twice writes identical bytes.

### Monitoring state in frontmatter

Frontmatter owns current state. The action log owns history. They agree the
moment an action is written and diverge after an extension pushes the window
out, which is why both exist.

An incident under observation carries one flow mapping:

```yaml
monitoring: {kind: 'error_absent', code: 'TNS-12564', start: '2026-08-30T14:22:10Z', until: '2026-09-02T00:00:00Z'}
```

`kind` and its payload are the flattened `RecoverySignal`, a closed union of
four variants: `error_absent{code}`, `event_present{pattern}`,
`flow_resumed{source}`, and `manual{description}`. An `event_present` pattern
is capped at 200 characters and matched in a killable child process under a
timeout, because `re` cannot be interrupted and the match runs under the
tick's lock. `start` and `until` are ISO-8601 Z, `until` exclusive and later
than `start`. Flat and all-string, so one dict serves both the frontmatter
mapping and the action record's `window`.

Incidents gain `updated:`. `incidents.set_status` is the only
frontmatter mutator an incident writer may use, and it rewrites `status`,
`updated` and `monitoring` in one pass, deleting `monitoring` when no window
is given. A resolved incident therefore cannot keep a stale window.

### The transition table

`lifecycle.TRANSITIONS` maps `(current status, command type)` to the
resulting status. An absent key is a `TransitionError`, so a transition
nobody wrote down is refused rather than falling through.

| From | RecordAction | StartMonitoring | ExtendMonitoring | Resolve | Reopen |
|---|---|---|---|---|---|
| `open` | `open` | `monitoring` | | `resolved` | |
| `monitoring` | `monitoring` | `monitoring` | `monitoring` | `resolved` | |
| `resolved` | `resolved` | | | | `open` |

Recording an action is a timeline event, not a status. It is legal from every
status, `resolved` included, because a post-incident note must not require
reopening the incident; otherwise people will hand-edit the page instead.
`Reopen` returns to `open` rather than `monitoring`, so a stale signal cannot
carry over.

`Resolve` is the only path to `resolved` and only a human reaches it. No
agent can run any transition. Automation gathers recovery facts and may say
an incident looks ready to close; the decision and the record stay the DBA's.

### What a transaction writes

`lifecycle.build(tree, path, command, actor, at)` is pure. It reads through a
snapshot at an asserted base commit, takes `at` as its clock, and returns one
`transaction.Proposal`. The same inputs produce the same bytes.

- Every command writes the incident page (the action section, plus the
  frontmatter when the status or window changes) and one `log.md` line,
  `[<at>] incident — <path>: <kind> by <actor> — <summary>`.
- `StartMonitoring` and `ExtendMonitoring` write the `monitoring:` mapping.
- `Resolve` moves the `index.md` bullet from `## Open incidents` to
  `## Resolved incidents`, keeping its label. The resolved section is created
  at the end of the page on first use, and a golden fixture pins where it
  lands.
- `Resolve` also appends one `## Resolution history` row to each error page
  the incident links through `[[errors/<code>]]`, in the same transaction,
  under the `update_error_pages` flag which defaults on. A linked error page
  the tree does not hold yields an advisory note and no row; creating error
  pages stays the ingest writer's job.
- `Reopen` moves the `index.md` bullet back.

The error page's table is:

```markdown
| resolved | db | incident | remediation | evidence |
|---|---|---|---|---|
```

Rows are keyed on `| <day> | <db> | [[<incident>]] | `, so resolving the same
incident twice rewrites the row instead of adding a second one. Evidence may
be empty. The dated action record is itself the operator note that
`wiki/AGENTS.md`'s recovery rule accepts as evidence, and the row cites the
record's `at`.

Monitoring facts are not wiki content. Deterministic evaluation writes them
to `.state/monitoring/<incident>.json`, machine-owned, so gathering evidence
never dirties the wiki tree and never competes with a human edit.

## Consequences

**The ingest writer gains a heading it must never match.** Its two heading
regexes are `\A## Update <day>\s*\Z` and `\A## <day> — `
(`src/dbwiki/structured.py`, lines 837 and 776). Neither can match
`## Action <ISO timestamp>`, and phase 0.3 pins that with a test that runs
both real regexes through the real `day_block_replace` over a page carrying
action records. Any future writer regex has to keep that property. The same
writer collapses runs of three newlines page-wide on its match path, which is
why the record rejects them in notes rather than trusting the writer to
behave. `structured.apply_proposal` also gains a flag-and-skip
guard so a re-ingest cannot append `## Update` to a resolved incident.

**Lint gains two blocking rules.** `incident-status-invalid` (error) catches
an off-vocabulary status, including a `monitoring` incident with no readable
window. `action-malformed` (error) catches a section that looks like an
action record and is not one. Both run in preview, before publication, so a
typo becomes a refused transaction instead of a silent misread. `parse_actions`
returns what parsed and what did not, so lint can report every bad section
and a daily render does not fail on one.

**The four readers collapse into one walk.** `incidents.load_incidents` reads
every page once and each consumer states its own question against the result:

- `daily_html.open_incidents` keeps incidents that are active and existed on
  the rendered day.
- `research._open_incident_codes` collects error codes from active incidents,
  so a `monitoring` incident keeps its research priority instead of dropping
  out.
- `research_offload` indexes by error code from the same walk instead of
  re-globbing per code.
- `structured.all_open_incidents` filters to active with path order
  preserved, because position in that list is the number the report prompt
  shows and `apply_report` resolves positionally.
- `daily_html._incident_card` labels from `Status.label` instead of the
  hardcoded `Open incident` badge, so a monitoring incident reads as
  monitoring.

**The wiki gains history it does not have today.** `## Resolution history`
sits in 0 of 64 error pages now. Every resolve writes one row per linked
error page, which is what makes "have we seen this before, and what fixed
it?" answerable from the error page rather than by reading incident prose. No
live page needs migrating, because all 25 incidents are `status: open` and
stay valid unchanged.

**Direct Git edits stay supported.** Nothing here takes the hand-edit path
away. A page a human wrote by hand parses through the same reader, and a
malformed one is reported by lint rather than ignored. What the transaction
adds is a base-commit assertion and a validated preview, so an out-of-band
commit between preview and commit is refused rather than silently overwritten.

**The cost is a wider contract.** `wiki/AGENTS.md` grows the action-record
and Resolution-history shapes, and any writer of incident pages, human or
machine, now has a schema it can violate. That is the trade. A schema that
lint can enforce, in exchange for free-text pages that nothing can check.

## Rollout

1. **Phase 0.2:** `incidents.Status`, `Incident`, `load_incidents`,
   `is_active`, the four reader migrations, the `incident-status-invalid`
   lint rule, and the status-badge fix.
2. **Phase 0.3:** this ADR becomes accepted. `ActionRecord`,
   `MonitoringWindow`, `RecoverySignal`, the codec, the `action-malformed`
   rule, and the `wiki/AGENTS.md` contract sections.
3. **Phase 0.5:** `lifecycle.TRANSITIONS` and `build`, which is where
   `## Resolution history`, the `index.md` move, and the `log.md` line are
   first written.
4. **Phase 0.7:** deterministic monitoring evaluation into
   `.state/monitoring/`, reading the windows this ADR defines.
5. **Deferred:** merging or superseding near-duplicate incidents, and the
   `evidence_ref` record that replaces bare digest paths in `evidence`
   (phase 2.6).

## Note 2026-09-08 — the merge command

`dbwiki incident merge <dropped-slug> --into <kept-slug>` is the first half of
the Rollout's deferred item, "merging or superseding near-duplicate
incidents", now built. It exists because structured ingest used to open a new
page per re-ingest, so the wiki holds several incident pages for the same db
and the same day, and until now nothing could fold them back together except a
hand edit.

`Merge` adds two rows to the transition table, `(open, Merge) -> resolved` and
`(monitoring, Merge) -> resolved`. `(resolved, Merge)` is deliberately absent.
A page can only be folded away once, and an absent key is already a
`TransitionError`, so a second merge of the same pair is refused by the table
rather than by a special case, and the wiki cannot grow a second record
claiming the same page is a duplicate.

What the kept page absorbs is the dropped page's `## Evidence` lines and every
`## Update <day>` whose day it has none of its own for, carried in day order
because that run of sections is the page's chronology. The evidence lines land
in `## Evidence` rather than under an `## Update <today>` wrapper, because the
ingest writer owns that heading and replaces it wholesale, so anything parked
there on a still-open page would be gone after the next ingest of today's
digest.

**One act, two records.** This is the one place merge widens the contract
above. "Every command a human issues produces exactly one `ActionRecord`" no
longer holds literally: a merge writes one record on each of the two pages it
touches, a `merge` record on the page being dropped and a `record-action`
record on the page that keeps the incident. Both pages need a durable account
of the same act, because each is read on its own. Somebody who lands on the
dropped page has to learn where the incident went, and somebody reading the
kept page has to learn why evidence it never ingested is now sitting in it;
one record on one page leaves the other page silent about a change to its own
contents. The rest of the per-command shape is unchanged: one `log.md` line,
the dropped page's, and one `index.md` move, the same `## Open incidents` to
`## Resolved incidents` move a resolve makes, through the same code and
keeping the bullet's label.

A merge writes no `## Resolution history` rows on the linked error pages. A
duplicate folding away is not a confirmed fix, and that table is the record of
fixes, so "every resolve writes one row per linked error page" above stays
exactly as true as it was.
