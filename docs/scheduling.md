# Scheduling

Single reference for what runs unattended, when, and which model each stage
spends. `README.md` and `DESIGN.md` point here; this file is the one to update
when the crontab changes.

## Installed crontab

User crontab of the service account on the machine hosting the repo. Successful runs are
silent — output only appears in `.state/cron.log` when something goes wrong.

```cron
PATH=/usr/local/bin:/usr/bin:/bin

# dbwiki (logbook) — successful runs are silent; failures land in cron.log
15 */2 * * *  cd /opt/logbook && uv run dbwiki run --lock-wait 3600 >> .state/cron.log 2>&1
30 23 * * *   cd /opt/logbook && uv run dbwiki run --consolidate --lock-wait 3600 >> .state/cron.log 2>&1
45 23 * * *   cd /opt/logbook && uv run dbwiki research --history --lock-wait 3600 >> .state/cron.log 2>&1
0 8 * * 1     cd /opt/logbook && uv run dbwiki lint --lock-wait 3600 >> .state/cron.log 2>&1
0 9 * * 1     cd /opt/logbook && uv run dbwiki research --lock-wait 3600 >> .state/cron.log 2>&1
0 9 1 * *     cd /opt/logbook && uv run dbwiki research --sources-only --review-sources --lock-wait 3600 >> .state/cron.log 2>&1
30 9 * * 1    cd /opt/logbook && uv run dbwiki research --caveats --lock-wait 3600 >> .state/cron.log 2>&1
0 10 * * 1    cd /opt/logbook && uv run dbwiki review >> .state/cron.log 2>&1
*/30 * * * *  cd /opt/logbook && python3 elk/scripts/emit_derived.py >> .state/elk/emitter.log 2>&1
*/10 * * * *  cd /opt/logbook && python3 n8n/scripts/watchdog.py --fix >> .state/watchdog.log 2>&1
5 * * * *     cd /opt/logbook && uv run dbwiki health --alert >> .state/cron.log 2>&1
50 * * * *    cd /opt/logbook && uv run dbwiki scores send >> .state/cron.log 2>&1
```

On the **analyst workstation** (ADR-0001, `analyst.enabled: true` only
matters on the on-prem side — this crontab entry is the same repo checked
out on the operator's machine, with cloud-LLM credentials and no ES access):

```cron
# dbwiki analyst — claims and runs one queued report request, if any
*/15 * * * *  cd /opt/logbook && uv run dbwiki analyst --once --lock-wait 600 >> .state/cron.log 2>&1
```

`--once` is the only mode in v1 (default and sole behavior); a tighter poll
interval than the on-prem tick is fine — `dbwiki analyst` with an empty
queue does one `git pull --rebase`, finds nothing pending, and exits 0.

`config/schedule.json` is the machine-readable mirror of the crontab above,
consumed by `elk/scripts/emit_derived.py` to compute each entry's next fire
time for the `schedule` telemetry stream — **update it whenever the crontab
changes**; the two are not derived from each other and nothing enforces they
stay in sync.

The explicit `PATH` matters: cron's default has none of `uv`, the model-server
CLI, or `codex`, and the schedule needs all three. `codex` lives under
linuxbrew, which an interactive shell has and cron does not — omit it and
agentic stages fail only when unattended.

| When | Command | Purpose |
|---|---|---|
| every 2h at :15 | `dbwiki run --lock-wait 3600` | adaptive tick — the main loop |
| daily 23:30 | `dbwiki run --consolidate --lock-wait 3600` | forced consolidation + daily fleet report |
| daily 23:45 | `dbwiki research --history --lock-wait 3600` | regenerate `## Past fixes` on every error page from our own incidents; no model, no web (`research.history.enabled`) |
| Mon 08:00 | `dbwiki lint --lock-wait 3600` | provenance lint + LLM health check |
| Mon 09:00 | `dbwiki research --lock-wait 3600` | ORA cause lookup, approved sources only |
| Mon 09:30 | `dbwiki research --caveats --lock-wait 3600` | practitioner notes from the approved sources; the one stage that talks to the web (`research.caveats.enabled`) |
| Mon 10:00 | `dbwiki review` | weekly attention review — selects what needs a human, narrates it, publishes to the workbench inbox |
| 1st of month 09:00 | `dbwiki research --sources-only --review-sources --lock-wait 3600` | re-review the whole source catalog |
| every 30 min | `elk/scripts/emit_derived.py` | derive ledger + per-db-run JSONL for the ELK shipper (`elk/README.md`); no LLM, stdlib only |
| every 10 min | `n8n/scripts/watchdog.py --fix` | liveness watchdog; reloads the model itself when the studio serves it under `health.min_context` (`.state/watchdog.log`) |
| hourly at :05 | `dbwiki health --alert` | health assessment; one alert per new failure fingerprint, never a repeat while it persists |
| hourly at :50 | `dbwiki scores send` | post the human-feedback scores portal/CLI/inbox acts queued in `.state/pending_scores.jsonl` to Langfuse; idempotent, silent when nothing is queued, a no-op without `langfuse.enabled` or keys (`docs/langfuse.md`, "Human feedback") |
| every 15 min (oracle@lab primary) | `~/chaos/oracle_chaos.sh tick` | lab-only chaos injector: breaks one thing, repairs it later (`deploy/chaos/README.md`) |

The hourly `health --alert` line carries no `--lock-wait`, on purpose:
`--alert` writes `.state/alerts.json`, so `cli._with_lock` takes the
single-flight lock for it, and without the flag the wait falls back to
`lock.lock_wait_default()`, which is 0 unless `DBWIKI_LOCK_WAIT` is set. An hour
whose :05 lands inside a long tick therefore exits `lock_busy` and skips that
round rather than queueing behind it; the next hour covers it, and a real
failure persists until it is fixed, so nothing is lost. Export
`DBWIKI_LOCK_WAIT` in the crontab to make it wait instead. That also changes
`dbwiki review`, the other line with no explicit `--lock-wait`.

`research` is deliberately **not** part of the tick: its cost is bounded and
independent of log flow, and new error codes become candidates on their own
(their pages carry no `researched:` date yet).

`review` runs last of the three Monday entries, after lint (08:00) and
research (09:00), because both of those hold the hour-long lock and the review
deliberately takes none — it carries no `--lock-wait` for the same reason,
since passing one would claim a lock it never asks for. It reads the wiki at a
pinned head and writes only `.state/review/`, so it cannot collide with a tick
that is mid-commit. A missed week stays missed: the ISO week is the
idempotency key, so a late run still publishes that week's review once, and a
second run in the same week publishes nothing unless you pass `--force` — it
only retries deliveries the audit does not hold as delivered (so nothing is
sent twice).

Exit codes: 0 published (or already published) and every delivery went; 1
the review failed (`REVIEW FAILED ...`) **or** it was published but at least
one delivery attempt failed — stderr then names the count and the retry
command (`dbwiki review --deliver-only --review-id YYYY-Www`), and
`dbwiki health` reports the failure (and `health --alert` alerts on it) until
a retry succeeds. The crontab line appends to `.state/cron.log` and ignores
the exit code, so nothing else changes for cron; the n8n `dbwiki-review`
workflow is a dry-run echo today, and a live one would mark that execution
failed, which is the point. 2 is a bad `--review-id`.

## The 2h tick, step by step

Not "run an agent every two hours". Deterministic Python decides; the LLM is
woken only for windows that earned it (`src/dbwiki/cli.py`, `cmd_run`).

1. **Discover + compact.** Every database found in ES gets its day-so-far
   window (00:00 → now) compacted into a digest. Regenerated each tick and
   idempotent. No LLM, no cost.
2. **Decide** (`src/dbwiki/trigger.py`, `decide`) → `wake`, `skip`, or
   `force_consolidation`, with one reason code per cause:
   - `already_ingested` / `content_unchanged` → skip, nothing new since last run
   - notable events or deltas → **wake**
   - `--consolidate` on a routine window → `force_consolidation`
   - otherwise → skip, `routine_only`
3. **Ingest** each woken database: the agent proposes wiki edits, which are
   validated, committed, and rolled back as a unit on failure.
4. **Report** if anything notable was ingested, or on the consolidation tick.
5. **Monitoring** (`src/dbwiki/monitoring.py`, `evaluate_all`). Every incident
   whose status is `monitoring` has its recovery signal evaluated against the
   digests covering its window. One verdict file per incident lands in
   `.state/monitoring/<incident>.json`, machine-owned, so gathering evidence
   never touches `wiki/`. The verdict is `met`, `not_met` or `insufficient`,
   and it only informs; resolving an incident stays a human decision. A
   failure here is noted on the run record and the tick carries on. It
   runs before the render so the operator's page shows this tick's
   verdicts.
6. **Render** `wiki/html/<day>.html` + `index.html` — every tick, including
   ticks where every database was skipped, so today's operator page is never
   stale. The 23:30 tick naturally produces the day's final version.
7. **Alerts** last, judging the state the tick leaves behind (off by default).

`dbwiki run --explain [--json]` replays steps 1–2 only: no agent call, no
watermark or ledger write, no digest emit, no run-health event. It is the
supported way to ask "why would each database wake or skip right now?".

## One command at a time

Every command that mutates the wiki working tree — `run`, `ingest`, `report`,
`lint`, `research`, `retry`, `analyst`, `backfill` — holds a single-flight
lock (`fcntl.flock` on `.state/orchestrator.lock`, `src/dbwiki/lock.py`) for
its whole duration. They share one working tree, and the stages that own it
validate and roll back against `git status`: a manual `dbwiki research` run
against a tick whose report agent was still writing saw the half-written
report as a foreign change, failed its own path-allowlist check, and rolled
the tree back — deleting the report out from under the agent still writing it
(the single-flight lock, `src/dbwiki/lock.py`).

`--lock-wait SECONDS` is how long to wait for the holder. The default is **0
— fail fast**, which is what you want at a terminal: `another dbwiki command
holds the lock (pid 4711, run, since …)` on stderr, exit 1, and a run-health
event categorized `lock_busy` (deliberately *not* retryable — waiting for the
other command to finish is your call, not the retry loop's). The crontab
passes `--lock-wait 3600` instead, since a tick that waits out a long report
is exactly right and a skipped tick is not; `dbwiki analyst` uses 600, its
poll interval being 15 minutes. `DBWIKI_LOCK_WAIT` sets the default for a
whole environment. The read-only invocations — `run --explain`, `ingest
--dry-run`, `lint --deterministic-only`, `research --dry-run`, `retry
--dry-run` — never take the lock at all, nor do `compact`, `health`, `stats`,
`dbs`, `es`, `awr`, `render-daily` or `review`.

The lock is released by the process dying, whatever the reason, so nothing
can strand it and there is no timeout to tune. `run` records how long it
waited as `lock_wait_s` in its run-health event: ticks queueing up behind a
slow agent show there before they show anywhere else.

The incident workbench (`dbwiki portal serve`) runs from this same checkout
and takes this same lock. It takes the lock per commit rather than for its
lifetime, capped by `portal.lock_wait_s` (3 s) rather than by `--lock-wait`. A
click that meets a tick answers `423`, writes nothing, and is recorded as a
`lock_busy` validation instead of a failed run, so `dbwiki health` stays green.
The systemd unit deliberately does not set `DBWIKI_LOCK_WAIT`, because an
ambient hour would turn that click into an hour-long hang instead of a 423.
`deploy/README.md` has the unit and the install steps.

## Structured-first, agentic supersede (analyst delegation)

With `analyst.enabled: true` (ADR-0001), step 4's escalated case changes: the
tick writes the structured placeholder report (deterministic, seconds-fast,
free) and enqueues the same window's agentic prompt as a `queue/pending/`
request instead of running the agentic adapter here — the on-prem node holds
no cloud-LLM credentials to run it with. The queued request carries the
*entire* assembled prompt, so the analyst workstation needs nothing but its
own wiki checkout and cloud-LLM keys to act on it.

`dbwiki analyst --once` on the workstation claims the oldest pending day, and
the newest request within it — an older still-pending request for the same day
is deleted in the claim commit, since a later window's report subsumes an
earlier one, and a backlog spanning several days drains oldest day first. It
runs the claimed request through the same rails as any other report (validate,
lint, rollback), and on success overwrites `reports/<day><suffix>.md` in place.
Because this is the same path the placeholder wrote, git history keeps both
versions and the live page silently upgrades from structured to agentic
whenever the workstation gets to it — no coordination beyond the commit
itself. **Step 1** in the next `dbwiki run` tick (before compaction) first
pulls what the analyst pushed, then folds the analyst's telemetry
(`queue/results/*.json`, written there because the analyst cannot reach the
on-prem `.state/`) into `.state/agent_runs.jsonl` and deletes the files; a
`run_id` already present is skipped, so replaying wiki history never
double-counts a result.

The pull (`queue.pull_before_fold`) runs only when `analyst.enabled` and
`report.push` are both on — a tick that never pushes shares no remote with an
analyst. It is `fetch` plus `rebase --autostash -X ours` onto the upstream
branch: the digests/ and html/ output the tick keeps uncommitted rides along,
and where both nodes changed `reports/<day><suffix>.md` the **analyst's**
pushed version is kept over the on-prem one that never made it out (during a
rebase git calls the upstream side "ours"). The fetch is bounded like a push
(60 s). Any failure — remote unreachable, a conflict `-X ours` does not
settle (one side deleted what the other changed), uncommitted edits the pulled
commits collide with — is undone completely: the rebase is aborted, HEAD and
the working tree are what they were, no stash entry is left behind. The tick
then warns on stderr, notes `analyst_pull_error` on its run-health event, and
carries on without the pull; `dbwiki health` names the failure until a later
tick pulls. Without this pull, every on-prem push after the analyst's first
one was rejected non-fast-forward and the results never folded in.

## Which model each stage spends

Two independent switches, and their interaction is the thing people get wrong:

- `agents.adapter` (currently `codex`, both tiers `gpt-5.6-luna`) — the
  adapter for **agentic** stages, where the model reads and edits the wiki
  itself. This is a cloud adapter, so agentic stages cost money and need
  `codex` on `PATH`; the structured stages below do not touch it.
- `agents.mode` (currently `structured`) — when set, ingest and *routine*
  reports instead go through `structured.generate`, which **always uses the
  `agents.pi` block and ignores `agents.adapter` entirely**
  (`src/dbwiki/structured.py`). Currently that is unsloth studio running
  Nemotron-3.5-Lightning-30B-A3B, i.e. local and free.
- `research.mode` (currently `structured`) — its **own** switch, independent
  of `agents.mode`: `structured` skips the web-capable agent for error-page
  lookups and has deterministic code fetch + a cheap `pi` call summarize
  instead (`src/dbwiki/research_structured.py`). Source-page review
  (`--review-sources`) always runs agentic regardless of this setting, and
  takes `research.adapter` (currently `pi`) rather than `agents.adapter` — so
  it stays local while the other agentic stages went cloud.
- `agents.escalated_report` (currently `agentic`, the default) — a **third**
  switch, only consulted when `agents.mode: structured` *and* the window is
  escalated (notable): `structured` opts that report into the structured
  path too, instead of the agentic fallback, with the strong `pi` tier and a
  deterministically assembled "Material" context pack (notable digests,
  latest same-day prior report, open incidents, error-class pages — see
  `structured.build_escalated_report_prompt`) standing in for the agent
  reading the wiki itself.

So with today's config the fleet is **split**: the high-volume stages run
local and free on `lfm2.5-2.6b`, while the two stages that need to read the
wiki itself went back to cloud `gpt-5.6-luna` for the escalated-report test
started 2026-08-05.

| Stage | Path | Model actually used |
|---|---|---|
| ingest | structured | local lfm2.5 via `pi` |
| report, routine window | structured | local lfm2.5 via `pi` |
| report, notable window | agentic (`agents.escalated_report: agentic`) | `gpt-5.6-luna` via `codex` (`agents.adapter: codex`) |
| lint | agentic — always | `gpt-5.6-luna` via `codex` |
| research, error pages | structured (`research.mode: structured`) | deterministic fetch + local lfm2.5 via `pi` |
| research, source review | agentic — always | local lfm2.5 via `pi` + `pi-web-access` (`research.adapter: pi`, not `agents.adapter`) |

A notable window's report stays agentic by default whatever `agents.mode`
says: attaching the window to existing history is the report's whole value
and needs a model that can read the wiki. Flip `agents.escalated_report:
structured` to route it through the deterministic writer and the strong `pi`
tier instead — the Material context pack stands in for the wiki-reading step.
Error-page lookups already run that way: `research.mode: structured` routes
them to local lfm2.5 via `pi`, so no web-capable adapter is needed for that
path. Only source-page review still requires one.

**Tier** is per database, not per tick: `digest_needs_escalation()` picks
`strong` when the digest carries an error-class notable group, `cheap`
otherwise. Since 2026-08-16 the `pi` tiers differ (`cheap` lfm2.5-2.6b,
`strong` qwen3.8-27b — a notable digest gets the 27B model), while both
`codex` tiers are `gpt-5.6-luna`. Escalation also decides the adapter for
the report itself, via `agents.escalated_report`.

`agents.timeout_seconds` (1800) bounds every agent call.

`agents.history_days` (90) bounds the other direction: how many days of this
database's own history — past fixes, operator actions, resolved incidents,
changes, journal headlines, in that order — the ingest prompt carries
alongside today's digest. The block is capped at 5000 characters whatever the
window, and `0` leaves it out.

## What the local stages need to be up

The structured stages — which is ingest, every routine report, and error-page
research — all reach the local model server through `pi`. Since 2026-08-29
that server is **unsloth studio** (`unsloth start`), which fronts a
`llama-server` on an ephemeral port with a stable OpenAI-compatible API at
`http://127.0.0.1:8888/v1`. Two things about it are load-bearing, and both
have already caused a silent multi-day outage:

- **The server must be running.** It does not restart itself after a reboot,
  and there is deliberately no `@reboot` line for it (the LM Studio one was
  removed 2026-08-15 and nothing replaced it). After a reboot: `unsloth
  start`. `n8n/scripts/watchdog.py` flags a down server or a small-context
  model, and so does `dbwiki health`. The watchdog repairs the second case
  on its own (`--fix`); a server that is not running at all it can only
  report, because the studio is what it would have to talk to.
- **The served context must be large enough.** The escalated cdb1 prompt is
  ~8.6k input tokens and Nemotron is a reasoning model that spends thousands
  of tokens thinking before it answers. At an 8k context it stops on `length`
  before writing a single character. Nemotron is served with 98k
  (`llama-server -c 98176 --fit-ctx 98176`), comfortably over the 32k
  `health.min_context` floor. Context is a property of how the studio
  launched the model — there are no per-model config files to install the way
  LM Studio needed (`config/lmstudio/` is the record of that older setup).
  Since 2026-09-08 the watchdog's `--fix` reloads the model through the
  studio API when the served context drops under the floor, so a studio
  relaunched with a small `-c` self-corrects within ten minutes instead of
  failing every strong-tier ingest until someone notices.
- **One model, both tiers.** `agents.pi.cheap` and `strong` are the same
  Nemotron 30B-A3B: a MoE with 3B active parameters, fast enough for the bulk
  and strong enough for a notable window. That retires the two-model GPU
  swap the LM Studio setup needed (`lfm2.5-2.6b` + `qwen3.8-27b`, 3-8 s each
  way) — nothing is evicted or reloaded between stages.

**Where the server lives is pi's business, not dbwiki's.** `pi` resolves the
provider named in `agents.pi.provider` (currently `unsloth`) from its own
table, `~/.pi/agent/models.json`, which carries the base URL and the API key
unsloth studio requires. `dbwiki health` and the watchdog read that same file
rather than keeping a second copy of the endpoint, so repointing pi repoints
the probes with it. Both accept an override — `agents.pi.base_url` and
`agents.pi.api_key_env` (the *name* of an env var, never a key) — for a
server pi is not configured for.

Both failures used to surface as `ProposalError: schema_version: expected 1,
got None` — a message about the *proposal*, for a run where no model was ever
reached or no answer was ever written. `harness.pi_stream_error` and
`harness.pi_no_answer` now catch each case at the adapter boundary and say
`provider unreachable: …` or `hit its context limit …` instead. Either way
the run fails as `harness_error`, the digest remains, and `dbwiki retry`
replays it once the server is healthy.

Agentic stages need `codex` on `PATH`. Cron gets an explicit `PATH=` that must
include `/home/linuxbrew/.linuxbrew/bin`, or notable reports and lint die with
`FileNotFoundError: 'codex'` while succeeding when you run them by hand.

## Watching it

- Is the model server up, and with what context length? A stopped server is
  the single most likely cause of a run of `harness_error` ingests, so check
  it first: the `model svr` line of `dbwiki health` answers both (its
  `dependencies` block probes the server whenever a route uses `pi`, plus the
  adapter binaries on `PATH`, the wiki's remote and git identity, and free
  disk). `n8n/scripts/watchdog.py` keeps its own copy of that check for the
  n8n experiment, which also watches the crontab and the tick schedule.
  Serving state straight from the source: `unsloth status`.
- `.state/cron.log` — stdout/stderr of scheduled runs; silent when healthy.
- `.state/orchestrator.lock` — who holds the single-flight lock right now
  (`{"pid", "command", "since"}`); a stale-looking file is not a stuck lock,
  the flock is gone with the process that wrote it.
- `.state/run_health.jsonl` — one structured event per run, facts only: per-db
  decision, reason codes, tier, telemetry, validation outcome, commit.
- `.state/agent_runs.jsonl` — one line per attempted LLM stage (ingest,
  report, lint, research) with duration and input/output token usage; the
  source of the `dbwiki — agent runs` Kibana dashboard.
- `.state/elk/run_starts.jsonl` — one line per command *started* (written by
  `run_recorder` before the command does any work); a start with no matching
  `run_health` finish is a run still in flight or one that died mid-way.
- `.state/elk/schedule.jsonl` / `.state/elk/queue_state.jsonl` — derived by
  `elk/scripts/emit_derived.py`: computed next-fire times for each
  `config/schedule.json` entry, and analyst-queue pending/claimed/failed
  snapshots.
- `dbwiki health [--json]` — collection, staleness, failures, backlog; exit 0
  healthy, 1 failure/blocker, 2 state unusable.
- `dbwiki stats [--json]` — cost and quality per task, adapter, and tier.
- `dbwiki retry [--dry-run]` — re-ingest digests whose failure a rerun can fix.
- Kibana dashboard `dbwiki — pipeline observability` — all of the above,
  continuously shipped to the local ELK stack by `elk/` (see `elk/README.md`).
- Langfuse (optional, `langfuse.enabled`) — one trace per attempted agent
  stage with the prompt, result JSON, usage/cost and validation/rollback
  scores; the content-level complement to the counts-only lines above. The
  cron environment must carry `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`
  for the export to run (`docs/langfuse.md`). The same keys let the hourly
  `dbwiki scores send` post the queued human-feedback scores; the portal
  never needs them.
- `python3 n8n/scripts/watchdog.py` — the checks none of the above make:
  model server reachable with the configured model at ≥32k context, the tick not
  older than its schedule says, `config/schedule.json` present in the
  installed crontab. Exit 1 + JSON when something is off. With `--fix` it also acts: a
  configured model that is loaded under `health.min_context`, or not
  loaded at all, is reloaded through the studio's own load API at
  `--context` (98176) and re-checked from the server's model listing
  before the exit code is decided. A healthy server is never reloaded.
  Cron runs it every 10 minutes into `.state/watchdog.log`.

## n8n next to cron (exploration)

`n8n/` holds a side-by-side scaffold that puts the schedule above under n8n
(one workflow per `config/schedule.json` entry, ssh to the host, error
workflow, the watchdog every 15 min). Imported inactive, dry by default; the
crontab stays authoritative. What it would and would not buy is written up
in `n8n/README.md`.

See `docs/provenance.md` for the lint rules and `README.md` for the full
command list.
