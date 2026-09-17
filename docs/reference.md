# Operator reference

Every knob, command and state file, in one place. For the short version start with [start-here.md](start-here.md); the architecture is in [../DESIGN.md](../DESIGN.md).

## Dependencies

Deliberately thin: the compactor is stdlib plus two packages, and everything
expensive is an external process the orchestrator shells out to.

**Python** — `requires-python >= 3.13` (`pyproject.toml`).

| Package | Why |
|---|---|
| `requests >= 2.32` | the only Elasticsearch client — plain HTTP against `elasticsearch.url`, no ES SDK |
| `pyyaml >= 6.0` | `config/dbwiki.yaml` and the `patterns/*.yaml` libraries |
| `pytest >= 8` (dev group) | test suite, incl. golden-digest regression fixtures |
| `langfuse >= 3` (optional extra, `uv sync --extra langfuse`) | Langfuse trace export, only when `langfuse.enabled` — see `docs/langfuse.md` |

Everything else the code imports is stdlib (`json`, `pathlib`, `subprocess`,
`hashlib`, `datetime`, `statistics`, `difflib`, `html`, …).

**External tools** — not Python packages, must exist on `PATH`:

| Tool | Required | Used for |
|---|---|---|
| `uv` | yes | dependency resolution and the `uv run dbwiki` entry point |
| `git` | yes | the wiki is a git repo; every agent run commits, pushes, or rolls back |
| Elasticsearch | yes | the log source (`elasticsearch.url`, default `localhost:9200`) |
| agent CLI | yes, one of | the LLM stages — `claude`, `codex`, `ollama` (via codex `--oss`), or `pi`, selected by `agents.adapter` (`src/dbwiki/harness.py`) |
| a local model server + `pi` | yes, as currently configured | the structured stages (ingest, routine reports, research) always run on the `agents.pi` block — Nemotron-3.5-Lightning-30B-A3B in unsloth studio — whatever `agents.adapter` says, while the agentic stages (lint, notable-window reports) run on `agents.adapter`, currently `codex`. As configured today the machine needs **both** |

Only the adapters actually configured need to be installed; the others are
alternatives, not co-requisites. Agentic `research` additionally needs a **web-capable**
adapter, hence its own `research.adapter` / `research.model` override —
`research.mode: structured` needs none of that, only `pi` (see "Structured
mode" below).

Upstream of all this, log shipping into Elasticsearch (filebeat on the database
hosts) is a separate system this repo neither installs nor supervises — it only
detects when the feed stops. See `docs/scheduling.md`.

## Setup

```sh
uv sync
cp config/dbwiki.yaml.example config/dbwiki.yaml   # or edit in place
export DBWIKI_ES_PASSWORD=...                      # overrides elasticsearch.password
```

Set `DBWIKI_ES_PASSWORD` in the shell (and in the crontab's environment) —
never commit a real password to `config/dbwiki.yaml`, which is tracked. The
`changeme` in it is the docker-elk lab default.

`CHANGELOG.md` records what changed when, including the dated measurements
behind the current adapter and model settings.

## Commands

```sh
uv run dbwiki compact --db cdb1 --date 2026-07-10   # one daily digest
uv run dbwiki compact --all --date 2026-07-10       # all dbs
uv run dbwiki backfill --from 2025-08-24 --to 2026-07-11
uv run dbwiki ingest --db cdb1 --date 2026-07-10    # LLM: digest -> wiki edits
uv run dbwiki report --date 2026-07-10              # LLM: fleet report
uv run dbwiki lint                                  # deterministic provenance lint, then LLM health check
uv run dbwiki lint --deterministic-only [--json]    # mechanical rules only (no LLM); see docs/provenance.md
uv run dbwiki research [--limit 3]                  # LLM: ORA cause lookup, approved sources only
uv run dbwiki research --caveats [--dry-run]        # LLM+web: practitioner notes onto researched error pages (research.caveats.enabled)
uv run dbwiki redact errors/ORA-12154.md [--all]    # ADR-0002: show the anonymized research request that would leave the box (nothing sent)
uv run dbwiki-researcher [--once|--watch] [--config researcher.yaml]  # ADR-0002 researcher: claim → agent(web) → result → push (cloud box only)
uv run dbwiki review [--explain] [--force]          # weekly attention review -> the workbench inbox (--explain: no LLM, no writes)
uv run dbwiki run [--consolidate]                   # one adaptive scheduler tick
uv run dbwiki run --explain [--json]                # why would each db wake/skip? (no side effects)
uv run dbwiki health [--json] [--alert]             # pipeline health: collection, staleness, failures, backlog
uv run dbwiki retry [--dry-run] [--db cdb1]         # re-ingest failed digests (non-destructive)
uv run dbwiki stats [--json] [--task ingest]        # agent cost/quality per task, adapter and model tier
uv run dbwiki awr --file awr.json [--emit]          # validate an AWR summary into a digest; see docs/awr-input.md
uv run dbwiki dbs                                   # discover databases
uv run dbwiki es search --source alert --db cdb1 \
    --from 2026-07-10T00:00:00Z --to 2026-07-11T00:00:00Z --query 'ORA-*'
uv run dbwiki es trace --path <trace file>          # raw docs behind one .trc the alert log named
```

## Daily DBA summary (HTML)

Every `run` tick renders and commits `html/<day>.html` + `html/index.html`
into the wiki — one deterministic page per day for DBAs: **Needs attention**
(databases whose day was notable, plus every still-open incident), **Worth a
look** (first-ever codes, new services/programs, rate anomalies, silence —
always labeled as telemetry, never as database state), and a collapsed
**Routine** table of per-db counts. Headlines come from the ingest ledger's
model-written summaries with a deterministic fallback; every entry links to
the journal, digest, incident and fleet-report pages via `report.link_base`
(they resolve where markdown renders, i.e. on the GitHub remote). `html/` is
machine output like `digests/` — agents never write it, lint never reads it.

## Scheduling (adaptive)

Five cron entries: a 2h adaptive tick, a daily consolidation, weekly lint and
research, and a monthly source review. The LLM is woken only for windows the
compactor found notable. **`docs/scheduling.md` is the reference** — installed
crontab, the wake/skip decision, and which model each stage actually spends
(structured mode routes ingest to the local `pi` model regardless of
`agents.adapter`).

Every command that mutates the wiki tree (`run`, `ingest`, `report`, `lint`,
`research`, `retry`, `analyst`, `backfill`, `health --alert`) holds a
single-flight lock (`.state/orchestrator.lock`, `flock`). A second entrant
fails fast with the holder's pid and command (exit 1, a `lock_busy` run-health
event) unless it passes `--lock-wait SECONDS` or the environment sets
`DBWIKI_LOCK_WAIT` — the installed crontab uses `--lock-wait 3600` so a tick
waits out a slow agent instead of racing it. `dbwiki run` also exits 1 when
any database or the report failed in the tick (the tick still renders the
daily HTML and evaluates alerts first), so cron sees partial failures.

`research` is deliberately **not** part of the `run` tick: its cost is bounded
and independent of log flow, and new error codes become candidates on their own
(their pages have no `researched:` date yet). It needs a web-capable adapter —
override with `research.adapter` / `research.model` in `config/dbwiki.yaml` if
the default one cannot browse. Citations are restricted to the domains of
`wiki/sources/*.md` pages with `status: approved`; the orchestrator rolls the
run back if the agent cites anything else or invents a new source page.

`research.mode: structured` (default `agentic`) skips the web-capable agent
entirely for error-page lookups: deterministic code fetches the page itself
from the one approved+`fetchable: true` source (`sources/oracle-docs`) and a
local text model only summarizes the fetched text into cause/action JSON —
see "Structured mode" below. Reviewing source pages (`--review-sources`)
always stays agentic, mode setting or not.

## Telemetry → ELK (optional)

`elk/` ships the pipeline's own telemetry — run health, cron output, the agent
audit trail, per-db decisions, ledger events — into a local Elasticsearch via
a self-contained filebeat container, and installs a Kibana dashboard
(`dbwiki — pipeline observability`) showing what the pipeline is doing.
Everything needed to recreate it on another machine is in `elk/README.md`.

## Telemetry → Langfuse (optional)

`langfuse.enabled: true` in `config/dbwiki.yaml` additionally exports every
attempted agent stage as one Langfuse trace — a generation observation with
the prompt as input, the result JSON as output, adapter/model/usage/cost,
and trace scores (`validation_ok`, `rolled_back`, `lint_findings`,
`duration_s`); `session_id` is the run id shared with the wiki commit
trailer and the ELK events. Unlike the counts-only ledger/ELK telemetry
this deliberately carries content (prompts embed real alert-log excerpts),
so point it at a Langfuse host you trust with that. Needs `uv sync --extra
langfuse`; keys via `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`. Best-effort
like all telemetry here — a Langfuse problem warns once on stderr and never
fails a run. Setup, exported fields, and the analyst-node caveats:
`docs/langfuse.md`.

## Health and recovery

Every command that does work appends one compact run-health event (facts only —
no log messages, no prompts) to `.state/run_health.jsonl`. `dbwiki health` reads
that log plus the ledger, the watermarks, the wiki tree and one read-only ES
probe per source; it exits 0 healthy, 1 with a failure or blocker, 2 when the
state is unusable (ES unreachable and no local state). Every failure line is
followed by the exact recovery command (`-> …`).

It also judges what the pipeline *depends on* (a `dependencies` block): LM
Studio reachable with the configured `agents.pi` models at a usable context
(only when a structured stage is configured), the agentic adapter binary and
`pi`/`git` on `PATH`, the wiki branch's upstream and unpushed-commit count
when `report.push` is on, the git identity used for wiki commits, and free
disk under `.state/`; and whether each stage is overdue — `run`/`report`
older than `health.stage_stale_hours` (default 26h; `lint`/`research` 192h)
since their last success. Any of those is a failure (exit 1), never a blocker,
and fingerprints into the alerts below like every other category.

It deliberately separates **source silence** (this source is quiet, another one
is live) from **collection failure** (nothing recent anywhere — the current
state: the upstream collector died 2026-07-12T11:12Z) from **unknown** (the
probe failed). Recovery is only ever reported as `resumed_flow` (events after a
gap), `operator_annotation` (a note in `.state/recovery_annotation.txt`), or
`unknown` — absence of events is never recovery, and nothing auto-closes an
incident.

`dbwiki retry` re-ingests failed digests whose file still exists, whose content
hash still matches the failure, and whose category a rerun can fix
(`validation_failed`, `harness_error`, `agent_timeout`, `no_result` — not
`dirty_tree` or `wiki_missing`). It goes through the normal ingest path, so
dedupe, validation, rollback and unrelated working-tree changes behave exactly
as in a scheduled run. `health.stale_hours` (default 26) sets the staleness
threshold.

Before any of that costs a whole rerun, `agents.feedback_retries` (default 0)
gives one in-run second chance to any agentic stage (ingest, report, lint,
research — structured mode has its own, separate retry): a validation failure,
or the agent exiting clean with no result JSON / an unparsable one, re-invokes
the same prompt with the problem list (or the missing-result complaint)
appended, the failed attempt's edits still in the tree to fix in place rather
than rollback-and-redo. A timeout, nonzero exit or unknown adapter is never
retried, and a second failure still rolls back and raises exactly as it would
without the retry. The number of attempts (1 = no retry needed) lands in the
ledger and `.state/agent_runs.jsonl` telemetry alongside the fields below.

## Failure-only alerts (off by default)

```yaml
alerts:
  enabled: false        # nothing is sent until this is true
  sink: stderr          # stderr (one JSON line, cron mail) | file | webhook
  #file: .state/alerts.jsonl
  #sinks: [file, webhook]   # several at once; a list here wins over `sink`
  #webhook_url: https://ntfy.sh/your-topic   # or env DBWIKI_ALERT_WEBHOOK_URL
```

`webhook` is one JSON POST per alert (stdlib, 5 s timeout, no retries), which
is what ntfy/Slack/Teams/Discord incoming hooks accept. An unknown sink name is
a configuration error: the run warns, counts a `sink_error`, and leaves the
alert state untouched so the finding alerts again once the config is fixed.

When enabled, `dbwiki run` — and `dbwiki health --alert` — evaluate the health
assessment above against `.state/alerts.json`, a small versioned state file
keyed by failure fingerprint (error category + db + source). Alerts *consume*
that assessment; they never decide what a failure is.

One alert per fingerprint: a new failure alerts, the same failure on the next
tick does not (its `count`/`last_seen` are updated silently), and a **material
escalation** — the category for one (db, source) changing, e.g. `source_silent`
becoming `collection_failure` — alerts again. A fingerprint that disappears is
dropped and counted as `recovered` in the run-health event; there is no success
alert, and a healthy run sends nothing and does not touch the state file.
Configurable re-alert intervals are deliberately deferred until single-shot
dedupe proves insufficient.

Each alert carries the run id, category, db/source, the window or last-event
fact behind it, the last successful stage, and the exact recovery command
(`dbwiki retry --db cdb1`, `dbwiki run --consolidate`, "restart the collector
on lab-dg1", "restore the wiki/ checkout"). Never a log message, a prompt, or
a credential. Transport is out of scope: email/webhook/desktop delivery is one
`Sink.send(alert_dict)` implementation away (`src/dbwiki/alerts.py`).

## Agent cost and quality

Every attempted agent stage — ingest, report, lint, research, structured or
agentic — appends one line to `.state/agent_runs.jsonl` and (for ingests)
enriches its ingest-ledger entry with the same telemetry: `run_id` (shared
with the run-health event and the wiki commit's `Run-ID:` trailer), task,
adapter, model and tier, mode, attempts, duration, timeout flag, token/cost
`usage`, digest size, pages touched, incidents opened/updated, validation and
rollback outcome, and the blocking-lint count. Counts and identifiers only —
never a prompt, a log message, a page body or a credential.

`dbwiki stats` reads `agent_runs.jsonl` (falling back to the ledger for
ingests recorded before that file existed), grouped by (task, adapter, model
tier, mode): runs, accepted/failed, rollback rate, median duration, total and
median cost, mean pages touched, lint-defect rate, mean/max attempts and the
share of runs with known usage, plus a cheap-vs-strong block per task.
Missing token/cost data is reported as `unknown` next to a `cost_n` sample
size, never estimated; entries written before this existed still appear, with
their telemetry fields `unknown`. No tier verdict is offered until both sides
have at least five runs (`stats.MIN_SAMPLE`). Telemetry capture is best-effort
throughout: a capture failure warns on stderr and lands in the run-health
event, but never blocks or rolls back an otherwise-good ingest.

## Performance inputs (optional, off by default)

Two read-only performance sources can be added as evidence. Both are
deliberately threshold-free: they carry numbers, and interpreting them is the
agent's job, not a rule's.

**Fleet metrics.** `metric_sources:` in `config/dbwiki.yaml` (shipped commented
out, `enabled: false`) adds a metric data stream — e.g.
`.ds-logs-oracle.metrics-*` with `oracle.metric.{type,name,unit,value}`,
`oracle.{tablespace,fra,metrics,dataguard}.*` — using the same per-source shape
as `sources:`. An enabled metric source is merged into `sources` with
`kind: metric` and then rides the normal pipeline: same watermarks, same digest,
same deltas, same replay semantics. `patterns/metrics.yaml` turns utilization
and load samples into routine counters; the only notable rules key off a signal
the documents themselves carry (`event.outcome: failure` — the collector could
not read the database). No rule compares a value against a threshold.

**AWR summaries.** `dbwiki awr --file <path>` validates a bounded per-(db,
snapshot) JSON aggregate against contract v1 — snapshot window, DB time, top-N
waits, top-N SQL by elapsed (`sql_id` only, never SQL text), load-profile
scalars — and prints the digest it produces; `--emit` writes it beside the
compactor's digests. Files only in v1: no live Oracle. Contract, digest shape
and what is deferred: `docs/awr-input.md`.

Performance evidence may be cited on an incident page only as `derived`
evidence with matching db identity and an overlapping window
(`docs/provenance.md`).

## Agent adapters

Set `agents.adapter` in `config/dbwiki.yaml`: `codex` (default), `claude`,
`ollama` (codex `--oss --local-provider ollama`; pull a model first), or `pi`
(the pi coding agent in one-shot mode; any provider from `pi --list-models`,
e.g. `agents.pi.provider: unsloth` for a local unsloth-studio model — no bash;
web tools are added only for the research task, and only if the
`pi-web-access` extension is installed — see `research.adapter` below).
Model tiers per adapter:
`cheap` for bulk ingestion, `strong` when a digest carries incident-grade
events.

## Analyst node (optional)

`docs/adr/0001-onprem-analyst-split.md` lets the pipeline split across two
machines connected **only** by the wiki git remote: an **on-prem node** (this
machine, ES credentials, no cloud-LLM credentials) running cron ticks,
compaction, structured ingest, and structured *and placeholder* reports; and
an **analyst node** (an operator workstation, cloud-LLM credentials, no ES
credentials — it cannot reach the databases) running the agentic stages.
Phase 1 covers escalated reports only; lint and research stay on-prem until
phase 2.

`analyst.enabled: false` (default) is unchanged behavior — everything below
is dormant. With it `true`, an escalated window's on-prem tick writes the
structured escalated-report placeholder immediately (`reports/<day>.md` and
the HTML page are never stale) and enqueues an agentic analysis request as a
JSON file under `queue/pending/` in the wiki repo, instead of running the
agentic adapter locally. `dbwiki analyst [--once]`, run on the workstation
(same repo and config; `agents.adapter`/`agents.<adapter>.strong` there pick
the cloud model), claims the oldest pending day and the newest request within
it — an older still-pending request for the same day is superseded — runs it
through the same validate/lint/rollback rails as every other agent stage, and on
success **overwrites the same report path** (same-day supersede; git history
keeps the structured version). If the workstation is off or its keys are
revoked, the pipeline has already degraded gracefully to the structured
placeholder; only `dbwiki health`'s queue block complains about backlog age.

Telemetry crosses back the same way: the analyst cannot write the on-prem
`.state/`, so it commits a telemetry record to `queue/results/` instead; the
next on-prem tick folds it into `.state/agent_runs.jsonl` before compaction,
skipping any `run_id` already recorded (safe to replay), and deletes the
file. `dbwiki stats` and the ELK shipper see analyst runs exactly like local
ones, one tick late.

```yaml
analyst:
  enabled: false     # on-prem: true = enqueue escalated windows instead of
                     # running the agentic adapter locally
  stale_hours: 26    # health: age at which a claimed request is flagged as
                     # a crashed analyst; queue/failed/ is always flagged
```


## Research offload (ADR-0002, rolling out)

`docs/adr/0002-offload-research-anonymized.md` moves *research* (the one stage
that needs a cloud, web-capable agent) off the on-prem box to a lean
**researcher** that never clones the wiki and only ever sees anonymized
requests via a separate exchange repo. Step 1 is in: `src/dbwiki/redact.py`
builds a per-run vocabulary (db/host/service pages, registries, ES config,
`redact.terms`), sweeps patterns (IPs, FQDNs, `(HOST=…)`, `(SERVICE_NAME=…)`,
`USER=`, ES index/doc ids, oracle/home paths) and replaces every hit with a
consistent pseudonym (`DB_A`, `HOST_B`, `IP_A`, …); a fail-closed leak check
refuses any request that still carries an identifier. `dbwiki redact
errors/<CODE>.md` (or `--all`, `--candidates`, `--source <slug>`) prints
exactly the request JSON that would be sent, with the pseudonym mapping on
stderr — review it before turning `research.mode: offload` on.

Step 2 is in too: `src/dbwiki/exchange.py` is the exchange repo protocol
(`requests/pending|claimed|failed`, `results/`; git is the lock, same races
as the analyst queue), and `research.mode: offload` + `research.exchange.path`
make every `dbwiki research` run (a) fold results the researcher pushed —
validate (approved sources only, sizes, known run_id), de-map pseudonyms with
the on-prem `.state/redaction/<run_id>.json`, write through the deterministic
writer, run the research rails + lint, one wiki commit per result with the
request's `Run-ID`; unapplicable results go to the exchange's `failed/` with a
reason — then (b) enqueue one redacted request per candidate page / due
source (pages already in flight are skipped; a leak refuses that one request
and is counted in `dbwiki health`).

Step 3, the researcher (`src/dbwiki_researcher/`, console script
`dbwiki-researcher`, config `config/researcher.yaml.example`): a fresh
interpreter importing it loads only `dbwiki.harness`, `dbwiki.exchange` and
`dbwiki.queue` — no ES client, no compactor, no wiki, no `requests`. Its loop
is claim → agent (`web=True`) in a scratch dir holding only `request.json` →
validate against the request (approved domains only, sizes, kind/code) →
`results/<run_id>-<key>.json` → push; a bad answer requeues once, then
`requests/failed/`. `dbwiki-researcher --once` (default; `--drain` to empty
the queue) or `--watch` under cron/systemd on the cloud box, whose only
credentials are the exchange clone and the agent's own. Remaining: step 4,
the flip (`research.mode: offload` on-prem, egress closed).

## Structured mode (for local models)

`agents.mode: agentic` (default) lets the model read `wiki/AGENTS.md` and edit
the wiki itself. `agents.mode: structured` is the DESIGN.md fallback for models
whose judgment is fine but whose file mechanics are not — small local models
forget `log.md` and mangle frontmatter. In structured mode the model gets one
text-only call with **no tools and no file access** (`pi --no-tools
--no-context-files`): it receives the rendered digest plus the dedup context it
needs (this db's open incidents, which error pages already exist for the
digest's codes) and returns a single JSON object — summary, notable, journal
entry, per-code notes, an incident action (`none|open|update`), flags.
`src/dbwiki/structured.py` validates that object strictly (every error names
its field; one retry with the error appended, then the run fails as a harness
error) and then does **all** the file work itself: journal, db profile stub,
error-class pages and occurrence rows, incident open/update, `index.md`,
`log.md`. The model never names a path, never closes an incident, and codes it
did not see in the digest are dropped into `flags` instead of being written.

Writers are lint-clean and idempotent by construction — re-applying the same
proposal to the same tree changes nothing — but the orchestrator keeps every
rail: clean-tree check, result validation, deterministic lint, rollback, ledger
(the entry records `adapter: pi` and `mode: structured`).

The same machinery covers the **routine** fleet report: when no database in
the window was notable, the model gets the window's ingest results, the
collection-health lines and the numbered list of open incidents, and returns
prose only — summary, overview, one status line per database, one note per
open incident it has something to say about. `apply_report` renders
`reports/<day>[-HHMM].md` (frontmatter, summary table, health block, open
items), the index line and the `log.md` line; links are emitted only for
pages that exist. A window with a **notable** item stays agentic by default
whatever the mode — the "have we seen this before, and what fixed it" step
needs a model that can read the wiki. `lint` always runs agentic.

`agents.escalated_report: structured` (default `agentic`) opts an escalated
window into the structured path too, instead of falling back to the agentic
adapter. The prompt is `build_report_prompt` plus a deterministically
assembled "Material" section: each notable database's digest excerpt (deltas
and notable groups only), the latest same-day prior report if one exists, up
to 3 open incident pages in full, and up to 5 error-class pages for codes the
notable digests actually carry — every piece capped and marked `[truncated]`
when cut, standing in for the wiki-reading step an agent would otherwise do.
The model answers with one extra field, `notable_analysis` (one
`{db, analysis}` entry per notable database, validated against exactly that
set — an unknown db is rejected like any other malformed field), which
`apply_report` renders as a `## Notable items` section ahead of the routine
summary table. It may cite a wiki page only as a `[[...]]` wikilink that
appears verbatim in the Material; anything else it writes is flattened to
plain text, same as every other model-supplied string here. Telemetry still
records `model_tier: strong` — this path trades the agentic adapter for the
structured one, not the escalation itself.

`research` has its **own**, separate switch — `research.mode`, not
`agents.mode` — because it needs no wiki context at all: `research.mode:
structured` (default `agentic`) skips the web-capable agent for error-page
lookups, has deterministic code fetch the page itself from the one
approved+fetchable source, and asks a cheap local text call only to
distill the fetched text into `{"cause", "action"}` JSON
(`src/dbwiki/research_structured.py`). `apply_research` sets `researched:` and
writes the `## Reference` section itself, cited against exactly the source
that was fetched, so the same `unapproved_urls`/provenance checks the agentic
path is held to apply unchanged. A page whose fetch fails (non-200, network
error) is flagged (`no fetchable reference for <code>`) and left untouched —
the run still succeeds for whichever pages did fetch. Reviewing source pages
(`--review-sources`) always runs agentic: judging whether a live site went
stale enough to deprecate needs a browse structured mode has none of.
