# Monitoring and recovery

What to look at, what each signal means, and the exact command for every way
this pipeline is known to break. Companion to [user-guide.md](user-guide.md);
the schedule itself is in [scheduling.md](scheduling.md).

---

## The 30-second check

```sh
uv run dbwiki health          # exit 0 healthy · 1 failure/blocker · 2 state unusable
```

That one command reads the run-health log, the ingest ledger, the watermarks,
the wiki tree, the analyst/exchange queues and one read-only ES probe per
source, and prints every failure with **the exact recovery command underneath
it** (`-> …`). If it exits 0, nothing else needs looking at.

<!-- figure: triage | The health output names the cause and the consequence separately: fix the cause, then replay the consequence. -->

If it exits non-zero, work top-down through the blocks below.

## Reading `dbwiki health`

A real (unhealthy) run, annotated:

```
pipeline health 2026-08-29T20:49:23Z [UNHEALTHY] stale_hours=26

collection                                  ← is data still arriving at all?
  alert       ok    last event 2026-08-29T17:13:26Z [3.6h ago]
  listener    ok    last event 2026-08-29T17:13:46Z [3.6h ago]
  dataguard   ok    last event 2026-08-29T17:13:26Z [3.6h ago]
  recovery evidence: unknown — absence of events is not recovery

last success                                ← is every stage still completing?
  compaction  2026-08-29T20:16:15Z (from run) awrwh, cdb1, cdb1_stby, …
  ingestion   2026-08-29T16:38:13Z (from run) cdb1, cdb1_stby, dgnonc, dgnonc_s
  report      2026-08-29T16:38:13Z (from run) reports/2026-08-29-1615.md
  lint        2026-08-24T06:05:49Z (from lint) 2 finding(s)
  research    2026-08-29T13:56:35Z (from research) 3

dependencies                                ← can the next run even work?
  model svr   UNREACHABLE unsloth http://127.0.0.1:8888 [served ctx unknown]
  adapters    codex=ok git=ok pi=ok
  wiki remote push=on upstream=origin/main unpushed=0 user.email=…
  disk        60.38 GiB free under …/.state
  PROBLEM model_server: the local model server is not reachable …
    -> start the local model server (unsloth studio: `unsloth start`); …

watermarks                                  ← per-db progress
  cdb1        2026-08-29T20:15:00Z [0.6h]
  …

failed ingests                              ← what a rerun could fix
  digests/cdb1/2026-08-29.json harness_error [retryable] digest=present …
    -> dbwiki retry --db cdb1

digest backlog                              ← compacted but never ingested
  (none)

analyst queue                               ← ADR-0001 backlog (0 when unused)
  pending: 0

blockers                                    ← state that stops work entirely
  (none)
```

The example above is the single most common real failure: **the local model
server was down, so every structured ingest failed as `harness_error`**. Note
how it reads — the *dependency* block names the cause, the *failed ingests*
block names the consequence. Fix the cause, then replay the consequence.

Three distinctions the tool makes deliberately, and you should too:

- **source silence** (this source is quiet, another one is live) vs
  **collection failure** (nothing recent anywhere — the shipper died) vs
  **unknown** (the probe itself failed). They are different problems.
- **failure** vs **blocker**: a failure is something that went wrong and can be
  retried; a blocker is state that stops work until a human intervenes.
- **recovery is evidence, never absence.** It is only ever reported as
  `resumed_flow` (events after a gap), `operator_annotation` (a note you put in
  `.state/recovery_annotation.txt`) or `unknown`. Nothing auto-closes an
  incident.

Stage staleness is judged against `health.stage_stale_hours` — `run`,
`report` and `research_history` 26h, `lint`, `research`, `research_caveats`
and `review` 192h by default. A stage that has *never* succeeded is never
flagged. Every `dbwiki research` variant records as `research`; health keys
them by mode, so the nightly `research --history` succeeding no longer hides
a weekly agentic `research` that keeps failing (dry runs count for nothing).
A run that finds nothing due (no stale error page, no source due review, no
page due a caveat pass) still records an ok run of its mode, so a fully
researched wiki does not go stale. The workbench's runs screen shows the same
three research rows.

A weekly-review email whose last attempt failed is a `review delivery
failed` problem (alert category `delivery_failed`, one per review) until
`dbwiki review --deliver-only --review-id YYYY-Www` delivers it; see
docs/user-guide.md for delivery.

A source's newest event is read up to now + 15h only (covers a shipper that
labels local time as UTC). Events dated further ahead are ignored for
collection state and reported as a `future-dated events` problem — one
mis-parsed line dated 2099 used to keep a dead source `ok` forever.

## Failure categories and what to do

Every recorded failure carries exactly one category. `[R]` = a plain rerun can
fix it, which is what `dbwiki retry` acts on.

| category | means | do |
|---|---|---|
| `harness_error` `[R]` | the adapter failed — most often the local model server is down or the answer never arrived | fix the dependency, then `dbwiki retry [--db X]` |
| `agent_timeout` `[R]` | the call exceeded `agents.timeout_seconds` | check model/server load; `dbwiki retry` |
| `no_result` `[R]` | the agent exited clean but wrote no result JSON | `dbwiki retry`; if repeated, the prompt or model is the problem |
| `validation_failed` `[R]` | the result broke the contract (missing page, touched `digests/`, no `log.md` line) | `dbwiki retry`; if repeated, look at `wiki/AGENTS.md` vs the model |
| `es_unreachable` | Elasticsearch did not answer | check the endpoint in `config/dbwiki.yaml`, then `dbwiki health` |
| `unsupported_schema` | the ES field layout drifted; a window looked empty for the wrong reason | compare `sources.*.db_fields` with the current mapping, recompact |
| `dirty_tree` | uncommitted non-machine changes in `wiki/` | commit or stash them, then `dbwiki health` |
| `commit_failed` | git refused (lock, remote) | fix the wiki repo by hand, then `dbwiki retry` |
| `wiki_missing` | `wiki/` is not there | restore the checkout beside `config/` |
| `lock_busy` | another dbwiki command held the single-flight lock | wait, or rerun with `--lock-wait SECONDS` — deliberately *not* auto-retried |
| `stage_stale` | a stage that used to succeed has not succeeded within its budget | read `.state/cron.log` and the crontab; run that stage by hand |
| `dependency_failure` | model server unreachable / small context, adapter missing from `PATH`, no git identity, no upstream, unpushed backlog, low disk | the hint names which one and gives the command |
| `unknown` | nothing above matched | read `.state/run_health.jsonl` for that run id |

## Runbook — the failure modes that actually happen

**1. Local model server down (structured stages fail in a block).**
Symptom: a run of `harness_error` ingests, `model svr UNREACHABLE` in the
dependencies block. It does not restart after a reboot and there is no
`@reboot` entry for it on purpose.

```sh
unsloth status
unsloth start
uv run dbwiki health          # model svr should now report a served context
uv run dbwiki retry           # replay the digests that failed meanwhile
```

**2. Served context too small.** Same block, `[served ctx …]` below
`health.min_context` (32k). An escalated prompt is ~8.6k input tokens and a
reasoning model spends thousands more thinking; at 8k it stops on `length`
before writing a character. Historically both failures surfaced as a confusing
`schema_version: expected 1, got None`; the adapter now says `provider
unreachable: …` or `hit its context limit …` instead.

Since 2026-09-08 this repairs itself. The watchdog runs `--fix` from cron
every 10 minutes, and a configured model loaded under `health.min_context`
(or not loaded at all) is reloaded through the studio's load API at 98176,
then re-read from the server's own model listing before the exit code is
decided. A healthy server is never reloaded.

```sh
tail -n 40 .state/watchdog.log          # the last round's report; `fix` says what it did
grep -c '"reloaded": true' .state/watchdog.log   # how often the studio came back small
python3 n8n/scripts/watchdog.py --fix   # force a repair now
uv run dbwiki retry                     # replay the digests that failed meanwhile
```

`fix.reloaded` false with verdict `not_served` or `unreachable` means the
watchdog cannot help and item 1 applies. The report is indented JSON, one
object per round, so read it with `tail -n 40` rather than a per-line tool.

**3. `codex` missing from cron's PATH.** Agentic stages (notable reports,
lint) die with `FileNotFoundError: 'codex'` *only* when unattended, because
your interactive shell has linuxbrew on `PATH` and cron does not. Fix the
`PATH=` line in the crontab; `dbwiki health`'s `adapters` line verifies it.

**4. Collection failure — the feed stopped.** `collection` shows
`collection_failure` (nothing recent anywhere) rather than `source_silent`
(one quiet source). The fix is upstream: restart the filebeat shipper on the
database host. The pipeline detects this; it cannot repair it. Watermarks stop
advancing, and no incident is closed on the strength of the silence.

**5. Dirty wiki tree.** `dirty_tree` blocks every agent stage, because a failed
run rolls the tree back and would take your uncommitted edits with it. Commit
or stash anything outside `digests/` and `html/`.

**6. Lock contention.** `another dbwiki command holds the lock (pid …, run,
since …)`. Expected when you type a command during a tick. Wait, or use
`--lock-wait`. A stale-looking `.state/orchestrator.lock` file is *not* a stuck
lock — the flock dies with the process.

**7. Unpushed wiki commits piling up.** `wiki remote … unpushed=N` above the
threshold (10). `cd wiki && git push`, or set `report.push: false` if this
deployment has no remote.

**8. A stage silently stopped running.** `stage_stale` plus a `last success`
timestamp far in the past. Read `.state/cron.log`, check `crontab -l` against
`docs/scheduling.md`, run the stage by hand once.

**9. Portal down.** Symptom: the browser does not connect at
http://127.0.0.1:8765, or the workbench stops answering part-way through a
form. `dbwiki health` confirms it in the `dependencies` block. That block
carries a `portal` line only when the config has a `portal:` block, so an
install without one never reports the workbench at all.

```
  portal      UNREACHABLE http://127.0.0.1:8765/api/health
  PROBLEM portal: the incident workbench is not reachable at http://127.0.0.1:8765/api/health (nothing else is affected; incident work needs the CLI)
    -> systemctl --user restart dbwiki-portal (or `uv run dbwiki portal serve`); until then use `dbwiki incident ...`
```

```sh
systemctl --user restart dbwiki-portal
journalctl --user -u dbwiki-portal -n 50     # why it stopped
```

Until it answers, use the `dbwiki incident` CLI
([user-guide.md](user-guide.md)), or commit a page by hand in `wiki/` for
something the CLI has no verb for. Two consequences are worth knowing before
you do. A commit made out of band, by the CLI or by hand, appears in the
incident's page history in the workbench once it is back, because that history
is `git log` over the page and not portal state. And a portal commit that was
never pushed rides the next tick's push, so an unreachable remote is not an
emergency.

A `423` in the browser is not this failure. Another command holds the wiki
lock, almost always the 2h tick. Nothing was written, nothing failed, and
`dbwiki health` stays green. Wait a moment and click again.

## Alerts

Off by default; **on** here (`alerts.enabled: true`, `sink: file` →
`.state/alerts.jsonl`). Sinks: `stderr` (one JSON line, lands in cron mail),
`file`, `webhook` (one JSON POST, 5s timeout, no retries — ntfy/Slack/Teams
shape), or several via `sinks: [...]`.

Alerts **consume** the health assessment; they never decide what a failure is.
`dbwiki run` and `dbwiki health --alert` evaluate it against
`.state/alerts.json`, keyed by fingerprint (category + db + source + detail):

- a new fingerprint alerts **once**;
- the same failure next tick updates `count`/`last_seen` silently;
- a **material escalation** — the category for a (db, source) changing, e.g.
  `source_silent` → `collection_failure` — is a new fingerprint, so it alerts
  again with `reason: escalation`;
- a fingerprint that disappears is dropped and counted as `recovered` in the
  run-health event. There is no success alert, and a healthy run does not
  touch the state file.

Each alert carries: `run_id`, `fingerprint`, `category`, `db`, `source`,
`reason`, `escalated_from`, the bounded `window` fact behind it, `first_seen`,
`at`, `last_success` per stage, and `recovery_hint` — the exact command. Never
a log message, a prompt, a page body or a credential. An unknown sink name is
a configuration error: the run warns, counts a `sink_error`, and leaves the
state untouched so the finding alerts again once the config is fixed.

## The telemetry, and where to read it

| file / surface | one record per | good for |
|---|---|---|
| `.state/cron.log` | line of scheduled output | silent when healthy — anything here is worth reading |
| `.state/run_health.jsonl` | command invocation | per-db decisions, reason codes, tier, validation outcome, commit, `lock_wait_s` |
| `.state/agent_runs.jsonl` | attempted LLM stage | duration, tokens, cost, attempts, rollback |
| `.state/ingest_ledger.json` | digest | ingested/failed, content hash, error category |
| `.state/elk/run_starts.jsonl` | command *start* | a start with no matching finish = still running, or died mid-way |
| `.state/elk/schedule.jsonl`, `queue_state.jsonl` | 30-min snapshot | next fire times, analyst queue depth |
| `.state/alerts.jsonl` | alert | the file sink |
| Kibana `dbwiki — pipeline observability` | — | all of the above, continuously shipped by `elk/` |
| Langfuse (`langfuse.enabled: true`) | attempted agent stage | the **content** view: prompt in, result JSON out, usage/cost, scores (`validation_ok`, `rolled_back`, `lint_findings`, `duration_s`) |
| `dbwiki stats [--json] [--task X] [--since 12h] [--until ISO] [--by model]` | — | cost and quality per (task, adapter, tier or model, mode), optionally over a time window |

Everything except Langfuse is counts and identifiers only — never a prompt, a
log message, a page body or a credential. Langfuse deliberately carries content
(prompts embed real alert-log excerpts), so point it at a host you trust with
that; the cron environment must carry `LANGFUSE_PUBLIC_KEY` /
`LANGFUSE_SECRET_KEY` for the export to run. All telemetry is best-effort: a
capture failure warns on stderr and lands in the run-health event, but never
blocks or rolls back an otherwise-good run.

`dbwiki stats` output to read for a model decision:

```
task      adapter  tier    mode        n    ok   fail  rollback  med_s   cost  lint_defect  att
ingest    pi       cheap   structured  271  227  44    0.162     14.4    0     0            1.01
ingest    pi       strong  structured  190  131  59    0.311     18.2    0     0            1.01
report    codex    strong  agentic     32   32   0     0         92.7    unk   0            1.03
```

Missing usage is reported as `unknown` beside a `cost_n` sample size, never
estimated. No cheap-vs-strong verdict is offered until both sides have ≥5 runs.

## Extra watchdog

```sh
python3 n8n/scripts/watchdog.py           # exit 1 + JSON when something is off
python3 n8n/scripts/watchdog.py --fix     # and repair a small-context model
```

Covers the checks `dbwiki health` does not make: that the tick is not older
than its schedule says, and that `config/schedule.json` matches the installed
crontab. `n8n/` itself is an inactive side-by-side experiment; the crontab
stays authoritative, and cron runs the `--fix` form every 10 minutes
(runbook item 2). Without `--fix` the script only reports; with it, the one
failure it can repair is a configured model served under
`health.min_context` or not loaded at all.

## Rhythm

- **Daily** — glance at `wiki/html/index.html`; it is the product.
- **When cron mail or `.state/cron.log` moves** — `dbwiki health`, then the
  runbook above.
- **Weekly** — the Monday lint run's findings, and `dbwiki stats` if you are
  tuning models.
- **Monthly** — the source-catalog review run; deprecating a source makes every
  page citing it stale, so it gets re-researched automatically.
- **After a reboot** — `unsloth start`, then `dbwiki health`. Nothing restarts
  the model server for you.
