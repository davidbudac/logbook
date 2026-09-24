# Changelog

Notable changes to Logbook, the machinery repo (the wiki repo keeps its own
history).
Loosely [Keep a Changelog](https://keepachangelog.com/): newest first, one
section per day something landed. The pipeline runs from `main`; dated
sections are what a version number would otherwise be, and the public
snapshots carry a version on top.

Quoted lines are dated measurements that used to live as comments in
`config/dbwiki.yaml`; the config now explains only what a key *does*, and the
"when and why we changed this value" record lives here.

## 2026-09-24 — backlog

- Workbench: a day's report and a digest now open with a *Changes* strip —
  the fleet's changes that day, or that database's — above the page body.
  `GET /api/page` carries `changes` (always; `[]` when none), read off the
  digest sidecars at the snapshot's revision and ordered by
  `daily_html.change_rows`, so it lists what the daily page's strip lists,
  capped at the same 25 (ingest-db-history #04).
- Workbench Agents: the per-model table groups through `stats.group` (new
  `agg` hook and a `model_only` key the CLI does not offer), so a model's
  stage count matches `dbwiki stats --by model` over the same window. A
  stage whose agent-run line the log holds twice now counts once, as the CLI
  already counted it (dedupe on `event_id`); otherwise the output is
  unchanged (incident-workbench #31).

- Agents: the unused `ollama` adapter is gone (harness, `--adapter` choices,
  health's binary probe, config examples, docs). Supported adapters are
  `codex`, `claude` and `pi`; a local model runs through `pi`. A config whose
  `agents.adapter` or `research.adapter` names anything else — `ollama`
  included — now fails at load with an error naming the supported adapters.

- Human feedback reaches Langfuse (langfuse #02). Every committed
  `dbwiki incident` / portal act on a machine-written page, and every
  review-inbox acknowledge/suppress, queues `human_accepted` (1, or 0 for a
  merge / a suppress) and `human_action:<kind>` scores in
  `.state/pending_scores.jsonl` — no Langfuse key on the portal, never a
  reason an act fails. The score lands on the trace of the stage that wrote
  the page (the page's newest `Run-ID:` commit, its subject's stage, the
  matching `agent_runs.jsonl` line), else on the run's session. The new
  `dbwiki scores send` posts the queue idempotently (`score_id` = the record's
  key; a partial failure keeps exactly the unsent ones) and is a no-op
  without Langfuse; cron runs it hourly at :50 (`scores-send`). `dbwiki
  health` warns — not red — when scores wait longer than
  `langfuse.score_backlog_hours` (24). The review synthesis stage is now
  exported as a `dbwiki-review` trace so inbox decisions have one to score.

- Telemetry: every ledger telemetry block names its `mode` (`agentic`,
  `structured`, `offload`, `caveats`, `history`); agentic ingests, reports,
  lint and research used to carry none. The run-health event's `telemetry`
  gains it the same way, and an analyst run's queue result record and
  agent-run line now say `agentic` (python-workarounds #07).
- `dbwiki health` accepts `--lock-wait` like every other locked command
  (it applies with `--alert`, the one form that locks). The crontab line is
  unchanged (#10).
- A report whose collection-health probe fails still runs without the
  block, but now says so: one `warning: collection-health block dropped
  from the report: …` line on stderr (#18).
- `dbwiki stats --since/--until <ISO|12h|7d>` bounds the window (agent-run
  lines and legacy ledger rows by their `at`; an unreadable bound exits 2),
  and `--by model` groups by model instead of tier. `stats.group` is the
  shared rollup; the default output is unchanged (incident-workbench #30).
- `agents.history_days` now reaches the escalated structured report and its
  fallback: `0` removes the database-history Material there too; before, it
  always used the 90-day default (ingest-db-history #03).
- Prompt files, leftovers: `orchestrate.HISTORY_RULE` and the researcher's
  prompt (`config/prompts/researcher/`) now live under `config/prompts/`,
  byte-identical (new snapshots pin both). `dbwiki lint` warns
  `generated-section-drift` when the wiki's `AGENTS.md` generated section no
  longer matches its prompt file; no markers, no finding. The test suite now
  refuses to start a real `claude`/`codex`/`ollama`/`pi`/`lms` (a stand-in
  under the temp dir still runs) (prompt-files #01).

## 2026-09-23 — verification fixes

A 24-issue code review, fixed across the pipeline, the agent harness,
research privacy, health/alerts/review and the portal. Each fix has its
regression tests.

- Ingest: the first tick after midnight re-compacts yesterday's whole day
  and ingests it, so the 23:30-24:00 tail reaches a digest (#04). Rate
  anomalies are also measured over a trailing
  `compactor.rate_anomaly_window_hours` (3) and the day's strongest one is
  restated by its later digests. Compactor groups keep codes apart and
  count what the cap dropped (#05). `content_hash` ignores a rate anomaly's
  moving numbers; ES timestamps compare as instants; `ORA-0600`-style codes
  match (#16). A numeric-string listener `return_code` reads as a number.
- Agent harness: result JSON is shape-checked; one rollback wrapper per
  stage; per-stage path allowlists and no hidden paths (#02). A timed-out
  agent's whole process group is killed and the tree settles before rollback
  (#03). `index.md`/`log.md` may not be deleted or emptied; queue fold-in and
  restores touch only their own paths (#15).
- Research privacy: the caveats prompt is redacted and leak-checked and fails
  closed (#07); many redaction pattern gaps closed, an independent leak
  scanner, `dbwiki redact --fuzz` (#08); exchange ids, slugs and citation
  links are validated before any write (#09, #10).
- Incidents: impossible instants and multi-line fields refused (#12); two
  different records in one second refused rather than overwritten (#13);
  lifecycle merge/extend/past-fix rules tightened and a resolve records
  `error_pages: false` when it declined the error-page rows (#14, ADR-0003).
- Health, alerts, review: research freshness is per mode (`research`,
  `research_caveats`, `research_history`), a run with nothing due counts,
  future-dated ES events are a problem (#17); undelivered alerts are kept and
  retried (#06); review delivery retries, reports failures and verifies
  STARTTLS certificates (#18); capped logs and review acks are written under
  a lock (#19); eval metrics no longer average failed items in (#21).
- Queue and git: stale analyst claims are reclaimed automatically, honouring
  `analyst.stale_hours`; git calls time out and survive non-UTF-8 output
  (#22). The on-prem tick pulls the wiki remote before the analyst fold-in,
  so analyst results arrive and on-prem pushes stop being rejected (#15).
  Stale research-exchange claims are reclaimed automatically too, by the
  researcher's next claim and by the on-prem research round.
- Portal: markdown rendering always terminates, and CI renders every template
  page under a timeout (#01, #23); ids and `Content-Length` are validated
  (#09, #11); the daily HTML keeps past days stable (#20);
  `portal.allowed_hosts` / `trusted_proxies` (#24). The runs screen's
  freshness rows follow health's per-mode research stages; action rows carry
  `error_pages`.

### Operator notes

Behaviour that changes on deploy:

- More wakes are possible. The first tick after midnight catches up
  yesterday's tail, one extra agent call per db whose yesterday digest is
  notable and changed after 23:30. The trailing 3 h rate-anomaly window can
  fire on nightly batch bursts that the day average hid: expect some new
  `rate_anomaly` wakes, or raise `rate_anomaly_window_hours` to 24 for the
  old behaviour.
- One-time hash change: digests carrying a rate anomaly or a `0x…` template
  re-hash once, so such a db may wake one extra time on deploy day.
- `dbwiki review` exits 1 when a delivery failed (retry with
  `dbwiki review --deliver-only --review-id YYYY-Www`) and 2 on a bad or
  unknown `--review-id`.
- STARTTLS now verifies the relay's certificate and host name: a relay with
  a self-signed certificate and `starttls: true` will fail delivery (visible
  in `dbwiki health`).
- `dbwiki health` goes red on ES documents dated more than 15 h in the
  future (a shipper writing local time as UTC).
- A failed alert send is no longer marked alerted: it is retried with
  backoff (30 min doubling to 12 h). With two sinks and one dead, the healthy
  one may get the alert twice.
- Agent stages may only write their own directories plus `index.md` and
  `log.md`: root pages such as `AGENTS.md`, `CLAUDE.md` or `README.md`, and
  any hidden path, are refused.
- The portal answers only the bind, its loopback spellings and
  `portal.allowed_hosts` (now case-insensitive); behind a proxy that rewrites
  `Host`, list it in `portal.trusted_proxies`. Unlisted names stay 403.
- `deploy/serve_wiki_html.sh` with no BIND waits for a tailscale address and
  then exits 1 (the unit restarts it); it no longer falls back to
  127.0.0.1.
- `dbwiki redact --fuzz` probes the redactor with the live vocabulary and
  prints how many terms came from each source (`wiki`, `registry`,
  `harvested`, `config`, `derived`); run it after changing `redact` config.
- Redaction learns host names the pipeline observes: the compactor records
  `host.name`, `host.hostname`, `agent.hostname`, listener/CRS diag-path
  hosts, `(HOST=…)` descriptor values and listener client hosts in the
  per-db registry (new optional `hosts` key; old registries load unchanged),
  and every outbound path redacts them with their short name, including an
  all-caps `DBSRV.CORPX` that used to read as `SCHEMA.OBJECT`. Their domains
  become known suffixes. Guarded: values under 3 characters, numbers,
  addresses, releases (`19c`), `localhost`/`__jdbc__`/`oracle`-style words
  and source-domain labels are not learned, and a letters-only name matches
  whole words only. The registry fills from the next compaction on, so
  `harvested 0` in `--fuzz` right after deploy is expected. Add to
  `redact.terms` only names no ES document has ever carried: a bare
  one-word host the pipeline never saw (`on dbsrv77`) is still not
  caught.
- With `analyst.enabled` and `report.push` both on, every `dbwiki run` now
  starts by pulling the wiki remote (`fetch` + `rebase --autostash -X ours`):
  the analyst's pushed `reports/<day>.md` wins a conflict with an unpushed
  on-prem version. A failed pull is undone, warned on stderr, noted as
  `analyst_pull_error` and flagged by `dbwiki health` until a tick pulls;
  the tick carries on. No change while `analyst.enabled` is off, as deployed.
- Research exchange (`research.mode: offload`): a claim older than
  `research.exchange.stale_hours` (researcher: `stale_hours`; default 26)
  goes back to `requests/pending/` with `attempts + 1`, or to
  `requests/failed/` (`stale_claim`) once attempts run out, instead of
  waiting for a human to move it. `dbwiki health` judges exchange claims by
  that key now, no longer by `analyst.stale_hours` (same 26 default). No
  change while research is not offloaded, as deployed.
- Research `nothing to do` runs are now recorded, and research staleness is
  split per mode, so `dbwiki health` can newly flag `research_caveats` or
  the weekly `research` as stale where the nightly history pass used to
  mask them.

## 0.1.0 — 2026-09-17, first public release

The repository goes public as Logbook (the CLI, package and config keep the
`dbwiki` name). Everything below this heading is the history that led here.

`dbwiki eval history` (2026-09-14). The history block is a belief until two runs
of the same days differ in a way a human can read. This replays the last
`--days` digests of one database out of a real wiki checkout through the
structured ingest chain twice, arm `none` with `history_days: 0` and arm
`history` with the configured window, and writes one markdown report. Each arm
runs on its own throwaway copy of the wiki whose incidents opened on or after
the replayed day have been deleted: an arm is scored partly on whether it would
open the incident that day deserves, so that incident must not already be
sitting in the tree it reads. Both arms are scored with the fixture eval's own
verdict, and beside it the report counts the dates, incident slugs and error
codes each arm's prose repeated out of the block it was given. It ends with the
days where the arms disagreed about `notable` or about the incident action,
which is the whole point of the command.

No Langfuse. The fixture eval keeps that transport; these items are one
operator's days rather than sanitized fixtures, and two arms are only
comparable inside one run against one wiki revision. Nothing is written to the
wiki, the ledger or `.state/` except the report.

The `after_change` delta (2026-09-14). When an error group's first occurrence
falls within `compactor.after_change_hours` (2) of a lifecycle change on the
same database, the digest carries a delta naming the latest such change: the
group's codes, the gap, the rule that matched and the alert-log line the
operator's change actually wrote. It renders under `## Deltas`, becomes its own
trigger reason, shows up in the portal's delta lines, and sits inside
`content_hash` like every other delta.

The point is to take one inference away from the model. The history block and
`## Changes` put a change and an error in front of it with nothing but prose
between them, and "the parameter change caused the ORA-1653" is exactly the
sentence a model writes when it has no fact to lean on. The compactor holds
both timestamps, so it states the timing itself and an `INGEST_TEMPLATE` rule
draws the line: say the error followed that change; say it was caused by it
only when the messages themselves say so. The delta escalates nothing on its
own, because an error group already asks for the strong tier.
`compactor.after_change_hours: 0` turns it off.

Unlike the history block, this one moves `content_hash`: a delta is content, so
every already-ingested window that holds an error within two hours of a change
re-hashes on upgrade and `dbwiki health` will report the drift. Expect one
re-ingest wave, or set the key to `0` until you want it.

Ingest reads the database's own history (2026-09-13). Every digest now carries
a `changes` list and a `## Changes` section, derived from the `lifecycle`-class
groups the pattern library already classifies, and `db_history.gather` reads
them back across `agents.history_days` (90) days together with the journal
headlines for days with events, the incidents opened or updated in that window
that have since been resolved and the day each was, the operator actions
recorded against them, and the `past_fixes` rows for today's codes. The open
incidents stay out of the block, because both prompts already list them
directly above it. `db_history.render` puts that block
between the error-page list and the digest in both ingest prompts, so the model
can say whether tonight's events are new, recurring, or follow a change
somebody made. No new model call: every line comes off files the pipeline
already writes. ADR-0006 has the shape and the alternatives.

The block is context, never evidence. One `INGEST_TEMPLATE` rule says so and
the block carries no evidence reference, but neither is a validator, so the
failure worth watching is a journal entry that restates July's incident as
tonight's fact. Caps bound the other risk: 5000 characters for the whole block
(6 fixes, 8 actions, 5 resolved incidents, 12 changes, 7 journal days), so
history cannot crowd the digest out of a cheap-tier context. The sections run
in that order, because the cap truncates the tail and the journal is the most
restateable of the five. Changes are folded one line per day and rule, so a
restart's eight to eleven lifecycle groups do not eat the budget on their own,
and each line quotes the alert-log line the rule actually matched rather than
whichever line the ES document opened with. `agents.history_days: 0`
leaves it out. Goldens with `lifecycle` groups change, because those groups
move from `### Notable` to `## Changes` in the markdown twin; the JSON
`notable` arrays, `content_hash` and `digest_codes` are untouched, so nothing
re-ingests on upgrade alone.

Trace-file evidence in the digest (2026-08-31). When a notable error event
names a `.trc` file ("Errors in file …", "Incident … dump file: …"), the
compactor looks that path up in `.ds-logs-oracle.trace-*` and puts a bounded
excerpt in the source section as `trace_evidence`, rendered as its own
`### Trace evidence` markdown block. Enriching at compact time rather than at
prompt time keeps the digest the one deterministic context artifact, so
structured ingest, escalated reports and agentic mode all see the same
evidence and a replay reproduces it. New `trace_lookup:` config block, off
when absent; new read-only `dbwiki es trace --path` drill-down; provenance
rules in docs/provenance.md, "Citing trace evidence".

The trace stream is deliberately *not* a `sources:` entry. Everything that
iterates `cfg.sources` assumes a compacted, watermarked, health-probed
source, and `health.py` would report the bursty trace stream as silent.

Two consequences worth watching. Trace documents reach Elasticsearch seconds
to minutes after the alert lines that name them, so a window compacted in
between hashes without them and hashes with them on a later tick. The hash
input is the document count, so a file filebeat ships one chunk at a time
costs one re-ingest per new chunk, bounded by `max_docs_per_path` (5);
same-day supersede makes each a rewrite, not a duplicate. And `dbwiki retry`
will report "recompact" for a failed window whose digest has since been
recompacted with trace evidence — arguably correct, since the content really
did change. A digest file left untouched keeps its old hash exactly, so
nothing re-ingests on upgrade alone.

> Excerpt budgets are 1500 characters per trace path and 6000 per digest, at
> most 6 paths and 5 documents per path. Measured against the live
> `cdb1_ora_7903.trc`: two shipped chunks excerpt to 327 characters, so the
> per-path budget bites only on a genuine call-stack dump.

Langfuse trace export turned on (2026-08-29). `langfuse.enabled: true`
against a self-hosted v4 stack at `/opt/langfuse`, published on this
node's tailnet address (`192.0.2.10:3000`, MagicDNS
`dbhost.example.net`) and on loopback — never on `0.0.0.0`, so the home
LAN is left out; keys are `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` lines
in the crontab environment. This is the first telemetry path here that stores
content — prompts and result JSON — which is why it is self-hosted and
loopback-only. See docs/langfuse.md, "This deployment", for the two gotchas:
the v4 `events_only` server has no `GET /api/public/traces` read API (query
ClickHouse `events_core`/`events_full` instead; the legacy `traces` table
stays empty by design), and structured research exports no prompt because it
has no single one.

> Verified end-to-end on an `awrwh` ingest: trace `dbwiki-ingest`, session =
> `run_id`, tags `[ingest, pi, cheap, structured]`, prompt in, result out,
> and all four scores (validation_ok, rolled_back, lint_findings,
> duration_s).

Local model server moved from LM Studio to unsloth studio (2026-08-29).
`agents.pi.provider: unsloth`, both tiers on
`unsloth/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF` — one resident 30B MoE
(3B active) replacing the `lfm2.5-2.6b` / `qwen3.8-27b` pair, so the JIT
model swap between a routine and a notable stage is gone. The server needs an
API key, which LM Studio did not:

- The health check and `n8n/scripts/watchdog.py` no longer hardcode
  `localhost:1234`. They resolve base URL and key from pi's own provider
  table (`~/.pi/agent/models.json`) for the configured provider, so
  repointing pi repoints the probes; `agents.pi.base_url` and
  `agents.pi.api_key_env` override that per config.
- The dependency renamed `lmstudio` -> `model_server` throughout: the health
  line is `model svr`, and the alert details are `model_server_unreachable` /
  `_model_missing` / `_context`. **Existing `dependency_failure` alerts
  re-fire once** — the fingerprint includes the detail.
- Without LM Studio's `/api/v0/models` there is no load state, only a
  catalogue, so the health line reports `served ctx:` from `/v1/models`
  instead of calling all six catalogued models "loaded".

> Nemotron is served at 98k context (`llama-server -c 98176`), over the 32k
> `health.min_context` floor. The per-model context files LM Studio needed
> (`config/lmstudio/`) have no equivalent here — context is set at launch.

Tier 0 hardening (2026-08-19). **Deploy notes for the live node** — after
`git pull`:

- Update the installed crontab to the lines in `docs/scheduling.md` (every
  wiki-mutating entry now passes `--lock-wait 3600`; `analyst --once` 600), or
  put `DBWIKI_LOCK_WAIT=3600` in the crontab environment. Without it an
  overlapping tick fails fast as `lock_busy` (exit 1, no compaction for that
  window) instead of racing the other run.
- Run `uv run dbwiki health` once by hand before the next tick: it now probes
  LM Studio (`agents.pi` model ids must match what `lms ps` reports), adapter
  binaries on `PATH`, the wiki upstream/unpushed count and free disk, and it
  flags stages overdue against `health.stage_stale_hours`. Any finding is
  exit 1 and — with `alerts.enabled: true` — a new alert fingerprint.
- `dbwiki run` exits 1 when any database or the report failed in the tick
  (it still renders the HTML and evaluates alerts first); wrappers keyed on
  `$?` change meaning.

Landed:

- `dbwiki run`: propagate stage failures instead of swallowing them, and exit
  non-zero when a tick did not do its work.
- `report` / `lint` / `research`: synthesize a result when the agent wrote none,
  from what the orchestrator already knows.
- Single-flight lock so two overlapping cron ticks cannot fight over the wiki
  working tree.
- `dbwiki stats` reads `.state/agent_runs.jsonl` (every attempt) rather than
  only the ledger (accepted ingests).
- Webhook alert sink beside `stderr` and `file`.
- `dbwiki health`: per-stage staleness, stage dependencies, and actionable
  hints on each finding.
- `dbwiki health`: stop reporting the generated `html/` tree as a dirty
  working tree.
- Atomic appends and atomic emit, so a crash cannot leave a torn JSONL line.
- `elk/scripts/emit_derived.py`: read both ingest-ledger shapes (the legacy
  flat one is what the live file is), report unreadable sources on stderr and
  exit 1.
- Config/secrets hygiene: no credential guidance buried in a comment, dated
  notes moved into this file, stray files out of the repo root.

## 2026-08-16

- Split the `pi` tiers across two models — `cheap: lfm2.5-2.6b`,
  `strong: qwen3.8-27b` — plus an n8n scaffold and the LM Studio per-model
  config notes.
  > cheap stays on lfm2.5 (empty digest 6-25 s), strong on qwen3.8-27b
  > (IQ2_XXS; 40-700 s per call, far better judgement on notable digests).
  > LM Studio JIT-loads and swaps them (3-8 s); the per-model defaults
  > (32k ctx, qwen: KV q8_0, 1 slot) live in
  > `~/.lmstudio/.internal/user-concrete-model-default-config/`.
- ADR-0002: offload research to a lean researcher via an anonymized exchange
  repo.

## 2026-08-14

- Reworked the ELK loop overview around individual rounds instead of
  meta-state.

## 2026-08-12

- Loop overview in ELK: run starts, the cron schedule, analyst queue state and
  alerts each ship as their own stream.

## 2026-08-10

- Fail loudly when `pi` never produced an answer.
- The analyst claims the oldest pending *day*, not the oldest enqueue.

## 2026-08-08

- Optional Langfuse trace export for agent stages (`langfuse.enabled`, off by
  default) — see `docs/langfuse.md`.
- Config: escalated reports back on codex/luna; langfuse block added.

## 2026-08-06

- Standalone HTML operator's guide, and the system-overview talk as a
  self-contained deck.

## 2026-08-05

- ADR-0001 and its phase 1: the on-prem/analyst split — analyst queue,
  escalated-report delegation, `dbwiki analyst`.
- Every agentic stage moved onto the local `pi`/lfm2.5-2.6b, then partly back
  the same day:
  > Set 2026-08-05 to baseline lfm2.5-2.6b end to end; was codex
  > (gpt-5.6-luna/terra). Flip back to codex if agentic quality is unusable.
  > Testing 2026-08-05: escalated reports on codex/gpt-5.6-luna; ingest and
  > routine reports stay structured on lfm2.5.
  > strong was gpt-5.6-terra; luna for the 2026-08-05 escalated-report test.
- Untracked a stray root `.agent-result.json`.

## 2026-07-31

- Switched the agentic stages from `claude` to `codex`.

## 2026-07-29

- Per-agent-run telemetry with token usage, shipped to ELK and the dashboard.
- Small-model support: feedback retries, structured research, structured
  escalated reports.
- Research flipped to structured mode:
  > flipped 2026-07-29 after live validation (25s, 3/3 pages, clean citations)

## 2026-07-28

- ELK shipper for the pipeline's own telemetry: filebeat, ingest pipelines,
  Kibana dashboard.
- Daily DBA HTML summary rendered into the wiki every tick.
- Same-day supersede: the latest ingest of a day replaces its wiki writes.
- Registry v2: per-source code keying, with v1 knowledge grandfathered in.
- Real-digest regression fixtures: 7 sanitized live digests plus goldens.
- Lint citations per logical reference item, not per physical line.
- Failure-only alerts turned on with the file sink:
  > enabled 2026-07-28 after first clean cron cycles
- Dropped the site-specific prefix from the repo and wiki-remote names.

## 2026-07-27

- WS0: live ingestion restored against the ECS schema drift; WS1: explainable
  agent triggering (`run --explain`).
- WS2: deterministic provenance linting. WS3: regression fixture corpus,
  goldens, adapter-free agent evaluation.
- WS4: run-health events, `dbwiki health`, non-destructive `dbwiki retry`.
- WS5: failure-only alerts (disabled by default), optional fleet-metric source
  and the AWR summary input contract. WS6: agent cost/quality telemetry in the
  ledger plus `dbwiki stats`.
- Structured ingest mode and structured routine reports — local models propose,
  deterministic code writes every file.
- `.state` file schemas versioned (v1) with backup-on-migrate; `pi` harness
  adapter added; offline CI workflow.
- Operationalized: wiki remote with auto-push, adapter/mode split, cron
  entries.
- Fleet-metric layout probed against the live cluster:
  > Layout matches `.ds-logs-oracle.metrics-*` as probed 2026-07-27 (ECS-only:
  > `oracle.database.name`, `oracle.metric.{type,name,unit,value}`,
  > `oracle.{tablespace,fra,metrics,dataguard}.*`; no legacy flat layout exists
  > for metrics).

## 2026-07-16

Earlier than this changelog's git-log window; kept because the config comment
carried the date.

- Elasticsearch schema drift on the log sources:
  > 2026-07-16 schema drift: ECS `oracle.database.name` replaced the legacy
  > `db_name`; both are listed so mixed windows keep working.
