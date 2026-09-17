# How it works

The pipeline end to end: what happens between an Oracle log line landing in
Elasticsearch and a sentence appearing in the wiki. Read
[overview.md](overview.md) first for the vocabulary.

Code references are to `src/dbwiki/`.

---

## 1. Inputs

Three text sources plus optional metrics, declared in `config/dbwiki.yaml`
under `sources:` — each is an index-pattern list, a timestamp field, the
fields that may carry the database name, and a pattern library:

| source | indices | pattern file |
|---|---|---|
| `alert` | `oracle-logs-alert-*`, `.ds-logs-oracle.alert-*` | `patterns/alert.yaml` |
| `listener` | `oracle-logs-listener-*`, `.ds-logs-oracle.listener-*` | `patterns/listener.yaml` |
| `dataguard` | `oracle-logs-dataguard-*`, `.ds-logs-oracle.dataguard-*` | `patterns/dataguard.yaml` |
| `metrics` (optional, off) | `.ds-logs-oracle.metrics-*` | `patterns/metrics.yaml` |

Both index generations are listed on purpose, so a window that straddles the
legacy indices and the newer data streams still works. `db_fields` is tried in
order (`oracle.database.name`, then `db_name.keyword`). ECS listener documents
carry no database field at all, so listener events are attributed via
`db_service_field` — the TNS service-name variants of a known database
(`cdb1`, `cdb1.world`, `CDB1_DGMGRL.world`, …).

A source window that comes back empty is not trusted blindly: `_check_schema`
(`compactor.py`) distinguishes "quiet" from "the field layout drifted" and
raises `UnsupportedSchemaError` rather than silently reporting zero events.

Log shipping *into* Elasticsearch (filebeat on the database hosts) is a
separate system this repo neither installs nor supervises — it only notices
when the feed stops.

## 2. Compaction — `compactor.py`

One run per (database, window). The tick uses day-so-far (`00:00 → now`);
`dbwiki compact` and `dbwiki backfill` can use any range.

1. **Pull** the window from ES over plain HTTP (`es.py`, `requests`, paged by
   `compactor.page_size`).
2. **Classify** every event against the source's pattern library into
   `lifecycle`, `dataguard`, `error`, `warning`, `routine`, or `unmatched`.
3. **Compact**: routine classes collapse into counters (per service, per
   client program, per return code); notable classes are kept verbatim with
   their timestamp and ES `index/_id`, grouped by rule and message template.
   Group and sample counts are capped (`max_groups_per_rule`,
   `max_samples_per_group`, `max_unmatched_groups`) so one runaway error
   cannot blow up a digest. Each group also carries a `context` block: the
   `context_lines` (3) log lines either side of its first occurrence, routine
   neighbours included, so a reader can see what led up to the event and what
   followed. Only the first `context_max_groups` (6) groups in digest order
   (error class first) keep the block, so a storm of unmatched groups
   cannot crowd the prompt. `context_lines: 0` switches it off and the key is then absent
   from the digest entirely; context never reaches the content hash, so a
   growing day-so-far window does not re-ingest just because a group picked
   up more neighbours.
4. **Look up trace files** (`trace.py`, optional, config `trace_lookup`). An
   error-class group whose event names a `.trc` file — "Errors in file …",
   "Incident … dump file: …" — triggers one search against
   `.ds-logs-oracle.trace-*` for that path, and a bounded excerpt of what
   comes back lands in the source section as `trace_evidence`. Excerpting
   here rather than in a prompt keeps the digest the single deterministic
   context artifact: structured ingest, escalated reports and agentic mode
   all read the same evidence, and a replay reproduces it. Only the path and
   the document count reach the content hash, so retuning the excerpt
   re-ingests nothing, while a trace arriving after its alert lines costs one
   re-ingest per new chunk, bounded by `max_docs_per_path`. The key is absent when there is nothing to say.
5. **Detect deltas** against the per-db registry (`.state/registry/<db>.json`,
   which remembers every code, service and program ever seen plus daily
   counters):

   | delta | fires when |
   |---|---|
   | `first_ever_code` | an ORA-/TNS- code never seen for this db before |
   | `new_service` / `new_client_program` | first appearance in the registry |
   | `rate_anomaly` | window per-hour rate > `rate_anomaly_factor` (10×) the median of the last `rate_baseline_days` (7), for counters above `rate_anomaly_min_count` |
   | `silence` | no events although the source logged on each of the last `silence_windows` (3) days — only for windows ≥ `silence_min_hours` (20), so a quiet night is never an outage |
   | `after_change` | an error group's first occurrence lands within `after_change_hours` (2) of a lifecycle change on the same database — the gap is stated as a fact, so the model never has to infer cause itself |

6. **Verdict**: `notable = true` if any notable group or any delta exists.
7. **Emit** the digest twins — `digests/<db>/<date>.json` and `.md` — through
   an atomic write, and advance the watermark for the database.

The digest is also hashed (`Compactor.content_hash`) over its semantic content
only, so two ticks whose windows differ but whose events do not produce the
same hash. That hash drives the dedupe in the next step and the replay safety
of `dbwiki retry`.

Anatomy of the readable twin (real excerpt, trimmed):

```markdown
# Digest: cdb1 — 2026-08-29
- window: `2026-08-29T00:00:00Z` → `2026-08-29T20:15:00Z`
- events: 1,459 total, 176 notable in 77 groups
- notable: **true**

## Deltas (never seen before / anomalies)
- **first-ever error code** `ORA-16649` (alert, first seen 2026-08-29T08:04:00.816Z)

## alert — 531 events
classes: routine: 365, error: 63, unmatched: 57, warning: 39, lifecycle: 6

### Notable
- **[error] tns_error** ×32 (`TNS-12543`, `TNS-513`) — 08:03:49 → 08:05:26
  > Fatal NI connect error 12543, connecting to: (DESCRIPTION=…)
  - es: .ds-logs-oracle.alert-default-2026.08.20-000008/AaBNRRdNRyfGx_MnTLot, …
```

Everything an agent later writes is anchored to lines like these — the digest
path, and optionally the ES doc ids from the JSON twin.

## 3. The trigger decision — `trigger.py`

Pure function: digest + prior ledger entry + tick flags → decision. No
timestamps, no randomness, so `--explain` and the real run always agree.

Check order, first match wins:

<!-- figure: trigger | First match wins, so the order *is* the semantics: an unchanged window can never reach the notability check, and a routine one never wakes a model by accident. -->

| # | outcome / reason code | when |
|---|---|---|
| 1 | `skip` / `already_ingested` | this exact window end was already ingested |
| 2 | `skip` / `content_unchanged` | window moved, content hash identical |
| 3 | `wake` / one reason per delta + `notable_class` per group | the digest is notable |
| 4 | `force_consolidation` | routine window on a `--consolidate` tick |
| 5 | `wake` / `manual` | routine window, invoked by hand |
| 6 | `skip` / `routine_only` | otherwise |

**Tier** comes from the same module: `digest_needs_escalation()` returns
`strong` when the digest carries a `first_ever_code` or `silence` delta, or any
error-class notable group; `cheap` otherwise. Tier is per database per window,
not per tick, and it also decides which path an escalated report takes.

`dbwiki run --explain [--json]` replays steps 1–3 only — no agent call, no
digest emit, no watermark or ledger write, no run-health event.

## 4. The agent stages — `harness.py`, `structured.py`

One contract, whatever the provider:

> The harness is invoked with (task, prompt, wiki checkout) and must exit
> having (a) made its wiki edits and (b) written `.agent-result.json`:
> `{task, db, notable, summary, pages_touched, incidents_opened,
> incidents_updated, flags}`. **The agent never touches git** — the
> orchestrator validates, commits and rolls back.

**Adapters** (`agents.adapter`): `codex` (`codex exec`, reads `AGENTS.md`
natively), `claude` (`claude -p --output-format json`), `ollama` (codex
`--oss --local-provider ollama`), `pi` (any provider `pi` knows, including a
local OpenAI-compatible server). Each adapter also has a usage parser, so
tokens and cost land in telemetry where the CLI reports them.

<!-- figure: modes | Structured mode covers ingest and routine reports; a notable window's report stays agentic unless `agents.escalated_report` says otherwise. Lint is always agentic. -->

**Two modes**, because small local models have fine judgment and terrible file
mechanics:

| | agentic (`agents.mode: agentic`) | structured (`agents.mode: structured`) |
|---|---|---|
| model sees | the wiki checkout, `AGENTS.md`, tools | one text prompt, no tools, no file access (`pi --no-tools --no-context-files`) |
| model returns | edited files + result JSON | one JSON proposal: summary, notable, journal entry, per-code notes, incident action (`none\|open\|update`), flags |
| files written by | the model | deterministic Python (`structured.py`) — journal, profile stub, error pages and occurrence rows, incident, `index.md`, `log.md` |
| guarantees | contract + rails | writers are idempotent and lint-clean by construction; the model never names a path, never closes an incident, and codes it did not see in the digest are dropped into `flags` |

Structured mode covers **ingest** and **routine reports**. A *notable* window's
report stays agentic by default — attaching the window to existing history is
the report's whole value and needs a model that can read the wiki — unless
`agents.escalated_report: structured`, which instead hands the strong tier a
deterministically assembled **Material** pack (notable digest excerpts, the
latest same-day prior report, up to 3 open incidents, up to 5 error-class
pages, each capped and marked `[truncated]`) and renders its extra
`notable_analysis` field as a `## Notable items` section. Lint always runs
agentic.

Both ingest prompts also carry the database's **own history**, so a day is
judged next to the days before it. The compactor writes a `changes` list at
the top of every digest and a `## Changes` section in the readable twin, both
derived from the `lifecycle`-class groups the pattern library already
classifies: `ALTER SYSTEM SET`, mount and open, tablespace and datafile DDL,
redo config, instance startup and shutdown. `db_history.gather` reads those
changes back off the digests on disk across the last `agents.history_days`
(90) days, together with the journal headlines for days that had something to
say, the incidents opened or updated in that window that have since been
resolved and the day each was, the operator action records against them
(record-action, resolve, reopen), and the `past_fixes` rows for the codes in
today's digest. The open incidents are not in the block; both prompts already
print them for themselves, directly above it. `db_history.render` puts the
result between the error-page list and the digest, in
`structured.build_prompt` and in the agentic ingest prompt alike. The sections
run past fixes, operator actions, resolved incidents, changes, journal, and
the caps follow in that order: 5000 characters for the whole block, 6 fixes, 8
actions, 5 incidents, 12 changes, 7 journal days, every line at `MAX_LINE`.
The order is the cap's order, because the cap truncates the tail: the lists
that answer "has this been fixed before" lead, and the journal, the most
restateable and least actionable of the five, goes last. Changes are folded
one line per day and rule before the cap applies, so a single restart's eight
to eleven lifecycle groups read as four lines naming the close, the mount and
the open, and the change line is the line the rule's regex actually matched,
not whichever line the ES document happened to open with. A day whose digest
records no events at all, or exists, is not notable and has no changes,
contributes no line, so quiet days do not spend the journal budget. The block
is labelled
context, not evidence: `INGEST_TEMPLATE` carries a rule telling the model to
use it to judge whether today's events are new, recurring or follow a
recorded change, and never to cite it as evidence for today or restate it as
a fact of this digest. The agentic prompt carries the same sentence.
`agents.history_days: 0` drops the block entirely.

When an escalated window runs agentic and the harness itself dies — a nonzero
exit, a timeout, a quota refusal — the structured escalated path runs once for
the same window, so the wiki still gets a report. That result carries a
`fallback: structured …` flag naming the adapter's failure. Both attempts land
in telemetry: the agentic one as a rolled-back `agentic` row, the structured
one as the row that committed. A validation failure or an unparsable result is
not a harness death and takes the feedback-retry path below instead.
`agents.escalated_report_fallback: structured|none`, default `structured`.

A validation failure, or a clean exit with no/unparsable result JSON, gets
`agents.feedback_retries` (1 here) second chance: same prompt with the problem
list appended and the failed attempt's edits still in the tree to fix in place.
A timeout, a nonzero exit or an unknown adapter is never retried.
`agents.timeout_seconds` (1800) bounds every call.

Which local model goes in `agents.pi` is a measured choice, not a guess.
`dbwiki eval run --model M` replays the seven real digest fixtures through the
same `build_prompt` → `propose` → `apply_proposal` path this section describes,
each into a throwaway wiki, and scores the result: did it parse, did it apply,
were its error codes in the digest, did it invent an incident update, how many
flags, how long, how many tokens. Nothing it touches is the real wiki, so it is
safe to run against production config while a tick is due. The table it prints
is the comparison; with `langfuse.enabled` the same run also lands as a Langfuse
experiment on the `dbwiki-ingest` dataset (docs/langfuse.md, "Experiments").

`dbwiki eval history --db cdb1 [--days 14]` asks a different question about the
same chain: whether the database-history block above the digest changes what the
model writes. It replays the last `--days` digests of one database out of a real
wiki checkout twice — arm `none` with `history_days: 0` and arm `history` with
the configured window — each arm on its own throwaway copy of the wiki with the
incidents opened on or after the replayed day deleted, so an arm is scored on
whether it would open the incident that day deserves rather than on having read
it. The markdown report says, per day and per arm, whether the day came out
notable, what the model did about an incident, which dates, slugs and codes out
of the block its prose repeated, and the flags, tokens and duration; the days
where the two arms disagree about `notable` or the incident action are marked
and listed again at the end. That last list is the answer the command exists
for: an empty one means the block is spending tokens and buying nothing. It is
a local loop with no Langfuse, and it writes nothing but the report.

## 5. The rails — `orchestrate.py`

Every agent stage runs inside the same fence:

<!-- figure: rails -->

1. **Single-flight lock.** `flock` on `.state/orchestrator.lock` for the whole
   command (`run`, `ingest`, `report`, `lint`, `research`, `retry`, `analyst`,
   `backfill`, `health --alert`). Default wait is **0 — fail fast**, with the
   holder's pid and command on stderr and a `lock_busy` run-health event;
   `--lock-wait SECONDS` or `DBWIKI_LOCK_WAIT` changes that. Read-only
   invocations (`--explain`, `--dry-run`, `--deterministic-only`, `compact`,
   `health`, `stats`, `dbs`, `es`, `awr`, `render-daily`) never take it.
2. **Clean-tree check.** Uncommitted changes outside `digests/` and `html/`
   abort the run (`dirty_tree`) — a failed run ends in a rollback that would
   otherwise wipe your edits.
3. **Digests committed first**, so the agent runs over a tree where its own
   changes are the only non-machine ones.
4. **Result validation.** Task matches, required keys present, every
   `pages_touched` entry exists on disk, nothing claimed under `digests/` or
   `html/`, `digests/` untouched, the tree actually changed, `log.md` updated,
   and every changed page other than `index.md` and `log.md` declared in
   `pages_touched` — an undeclared change refuses the publication. Ingest,
   report and lint additionally compare every changed `incidents/` page
   against the base revision: an agent may append a `## Update <day>`, an
   evidence line or an occurrence row and bump `updated:`, but `status:`, the
   `monitoring:` window, the `## Action` records and the `## Resolution
   history` rows belong to `dbwiki incident` (ADR-0003), and any change to
   one of them refuses the publication. Structural lint cannot see this: it
   judges the resulting page, not whether the actor was allowed to make the
   transition.
5. **Stage-specific rails.** Research may only change `errors/`, `sources/`,
   `index.md`, `log.md`; it may not create a source page (new sources enter by
   a human commit); every URL in a changed error page must sit on an approved
   source's domain.
6. **Deterministic provenance lint** over the changed paths only. Error-severity
   findings (`frontmatter-malformed`, `frontmatter-contradictory`,
   `wikilink-broken`, `digest-missing`, `citation-malformed`) block the commit;
   warnings do not. Rules: [provenance.md](provenance.md).
7. **Commit or rollback, as a unit.** Success → one commit carrying a
   `Run-ID: <run id>` trailer, plus a push when `report.push` is on. Failure →
   `git checkout -- .` and `git clean -fd` excluding the machine directories,
   so a rolled-back run never deletes a digest or a rendered HTML page. Every
   commit is scoped by pathspec to the paths it owns — the proposal's paths
   for a stage, `digests` for the compactor's output, `html` for the rendered
   summary — so a file a human staged mid-run is neither committed nor
   pushed. The lock keeps two pipeline runs apart; it does not stop a human
   using git.
8. **Ledger + telemetry.** The digest's ledger entry records `ingested` or
   `failed` with the content hash, the error category and the cost/quality
   block; one run-health event and one `agent_runs.jsonl` line are appended.

## 6. A tick, in order — `cli.py: cmd_run`

<!-- figure: tick -->

1. Fold any analyst telemetry results into `.state/agent_runs.jsonl` (ADR-0001;
   no-op when the analyst queue is unused).
2. Discover databases in ES and compact each one's day-so-far window.
3. Decide per database (section 3).
4. Ingest each woken database, through the rails.
5. Report if anything notable was ingested, or on the consolidation tick.
6. Render `wiki/html/<day>.html` + `index.html` — **every** tick, even one
   where every database skipped, so today's operator page is never stale.
7. Evaluate alerts against the state the tick leaves behind.

`dbwiki run` exits 1 when any database or the report failed, so cron sees
partial failures; the HTML render and the alert evaluation still happen first.

The daily HTML page is deterministic machine output, like digests: tiered
*Needs attention* (notable databases plus every still-open incident), *Worth a
look* (first-ever codes, new services/programs, rate anomalies, silence —
always labeled as telemetry, never as database state) and a collapsed
*Routine* table. Headlines come from the ledger's model-written summaries with
a deterministic fallback; links resolve against `report.link_base`, which must
point where markdown renders (the wiki's remote), not at a local path.

## 7. Research — `research.py`, `research_structured.py`

Its own weekly schedule, deliberately **not** part of the tick: its cost is
bounded and independent of log flow, and new error codes become candidates on
their own (a fresh `errors/<CODE>.md` has no `researched:` date yet).

Deterministic Python picks the workload — unresearched pages, pages whose
research went stale because a cited source was re-reviewed or deprecated,
pages linked from open incidents first. Then, depending on `research.mode`:

- **`structured`** (current): no agent browses. Code fetches the Oracle error
  help page itself from the one approved + `fetchable: true` source, and a
  cheap local text call distills the fetched text into `{"cause", "action"}`.
  `apply_research` writes `researched:` and the `## Reference` section, cited
  against exactly the source that was fetched. A page whose fetch fails is
  flagged and left untouched; the run still succeeds for the others.
- **`agentic`**: a web-capable adapter (`research.adapter` / `research.model`)
  browses and writes cited `## Reference` sections itself.
- **`offload`** (ADR-0002): no agent and no web egress here at all — see §8.

Citations are gated by the **source catalog**: `wiki/sources/<slug>.md` pages
with `status`, `tier`, `domains`, `fetchable`, `last_reviewed`,
`review_after_days`. A URL may be cited only if its host is (a subdomain of) a
domain on an `approved` page. External knowledge never enters Occurrences or
Resolution history. Deprecating a source makes every page citing it stale, so
it gets re-researched. Source-page review (`--review-sources`, monthly) always
runs agentic — judging whether a live site went stale needs a browse.

## 8. The two distributed shapes (both off here)

**Analyst node** — ADR-0001. `analyst.enabled: true` splits the pipeline
across two machines connected *only* by the wiki git remote: an on-prem node
with ES credentials and no cloud-LLM credentials, and an operator workstation
with cloud-LLM credentials and no ES access. An escalated window then writes
the structured placeholder report immediately and enqueues the fully assembled
agentic prompt as `queue/pending/<...>.json` in the wiki repo. `dbwiki analyst
--once` on the workstation claims the oldest pending day (superseding older
requests for the same day), runs the same rails, and overwrites the same report
path — the live page silently upgrades from structured to agentic. Telemetry
comes back through `queue/results/`, folded in by the next tick.

**Research offload** — ADR-0002. `research.mode: offload` sends *anonymized*
requests through a separate exchange repo to a lean researcher
(`src/dbwiki_researcher/`) that never clones the wiki. `redact.py` builds a
per-run vocabulary from wiki pages, registries and ES config, sweeps patterns
(IPs, FQDNs, `(HOST=…)`, `(SERVICE_NAME=…)`, `USER=`, ES ids, oracle paths) and
replaces every hit with a consistent pseudonym (`DB_A`, `HOST_B`, …); a
fail-closed leak check refuses any request still carrying an identifier.
`dbwiki redact errors/<CODE>.md` prints exactly what would leave the box.
Results are validated, de-mapped, and written through the same deterministic
writer and rails.

## 9. State, and what is safe to replay

Everything under `.state/` is machinery state, never wiki content:

| file | what |
|---|---|
| `watermarks.json` | per-db window end already processed |
| `registry/<db>.json` | every code/service/program ever seen + daily counters (delta baselines) |
| `ingest_ledger.json` | per digest: ingested/failed, content hash, error category, cost/quality block |
| `run_health.jsonl` | one event per command: per-db decisions, reasons, tier, validation outcome, commit |
| `agent_runs.jsonl` | one line per attempted LLM stage: duration, tokens, cost, attempts |
| `alerts.json` / `alerts.jsonl` | alert dedupe state / the file sink |
| `cron.log` | stdout+stderr of scheduled runs; silent when healthy |
| `orchestrator.lock` | `{pid, command, since}` of the current holder |
| `elk/*.jsonl` | derived streams for the filebeat shipper |

Replay semantics, by design:

- **Compaction is idempotent** — re-running a window overwrites its digest
  deterministically.
- **Ingest is deduped** by window end and content hash, so a tick that finds
  nothing new does nothing.
- **Same-day supersede** — the daily digest is cumulative, so re-ingesting a
  day rewrites that day's journal entry, incident update section and occurrence
  row instead of appending a reworded duplicate (enforced by construction in
  structured mode, by contract in agentic mode). A second `open` for a database
  that already has an incident open on that day collapses onto the existing
  page, however the model spells the slug.
- **Failures are replayable when the category says so.** `dbwiki retry`
  re-ingests failed digests whose file still exists, whose content hash still
  matches, and whose category a rerun can fix (`validation_failed`,
  `harness_error`, `agent_timeout`, `no_result` — never `dirty_tree` or
  `wiki_missing`). It goes through the normal ingest path, rails included.
- **Nothing auto-closes.** An incident is never closed because events stopped;
  recovery needs explicit evidence.

Failure categories and their recovery commands: [monitoring.md](monitoring.md).
