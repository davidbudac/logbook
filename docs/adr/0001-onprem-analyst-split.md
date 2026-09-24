# ADR-0001 — Split the pipeline into an on-prem node and an analyst node

Date: 2026-08-05
Status: accepted

## Context

Everything runs on one machine today: the deterministic compactor, the
structured local-model stages (ingest, routine reports — LM Studio, currently
`lfm2.5-2.6b`), and the agentic cloud-model stages (escalated reports, lint,
research) via the codex/claude CLIs. That machine sits next to the databases
and holds both the Elasticsearch credentials *and* the cloud-LLM API keys.

Two pressures argue for a split:

- **Trust boundary.** Raw logs must stay on premises. The agentic stages
  already never see them — they read only digests and the wiki — but the
  cloud credentials and the cloud-bound process run on the DB-adjacent host,
  which is a wider surface than the data flow requires. On-prem also may not
  have (or want) general web egress, which `research` needs.
- **Model economics.** The 2026-08-05 measurements: structured ingest on the
  local model is reliable (16/16 first-attempt) and free; the agentic
  historical-context step is the report's whole value and needs a stronger
  model (`gpt-5.6-luna` acceptable, opus best, local model unusable —
  see `dbwiki stats`). Those two workloads have no reason to share a host.

Three existing properties make the split cheap:

1. Agentic stages consume only the digest + a wiki checkout; the wiki is a
   git repo with a shared remote. Git is already the replication channel.
2. The structured escalated report (`agents.escalated_report: structured`)
   is a validated, $0, seconds-fast fallback for exactly the stage we want
   to move off-host.
3. Reports and journal entries are same-day-supersede by design: a later,
   better version of `reports/<day>.md` replacing an earlier one is a normal
   operation, not a conflict.

## Decision

Split into two roles connected **only** by the wiki git remote.

**On-prem node** (DB-adjacent; today's machine): cron ticks, compactor,
structured ingest, routine *and placeholder* reports, HTML rendering, health,
alerts, watermarks, ledger. Holds ES credentials and git credentials.
**Holds no cloud-LLM credentials.**

**Analyst node** (operator workstation): the agentic stages — escalated
reports (phase 1), lint and research (phase 2). Holds cloud-LLM credentials
and git credentials. **Holds no ES credentials; cannot reach the databases.**

### Structured-first, agentic supersede

The on-prem tick never blocks on, or waits for, the analyst. On an escalated
window it:

1. writes the **structured escalated report immediately** (the existing
   `escalated_report: structured` path) so `reports/<day><suffix>.md` and the
   daily HTML are never stale, then
2. **enqueues an analysis request** for the same window.

When the analyst later processes the request, its agentic report **overwrites
the same report path** (same-day supersede; git history keeps the structured
version). If the analyst never runs — workstation off, keys revoked — the
pipeline has already degraded gracefully to the structured report, and only
`dbwiki health` complains about queue age.

### The queue is a directory in the wiki repo

`queue/` joins `digests/` and `html/` as machinery-owned wiki content:
agents never write it as wiki content, lint never reads it, and every state
transition is a commit with the usual `Run-ID:` trailer.

```
queue/
├── pending/report-<day><suffix>-<run_id>.json    # awaiting an analyst
├── claimed/…                                     # an analyst is working on it
├── failed/…                                      # gave up after N attempts
└── results/<run_id>-<event_id>.json              # telemetry riding back
```

**Request file** (`schema_version: 1`): `kind` (`report`; later `lint`,
`research`), `run_id`, `day`, `suffix`, `window {from,to}`, `notable_dbs`,
`created_at`, `attempts`, and the **full agentic prompt text** assembled
on-prem — the request is self-contained because the prompt's only inputs
(the window's ingest results and the collection-health note) live in
on-prem state the analyst does not have. Everything else the agent needs is
the wiki checkout itself. Never a credential, never a raw log line beyond
what digests already carry.

**Claim protocol** — git is the lock:

1. `git pull --rebase`.
2. Pick the **oldest day** pending, and within it the **newest** request;
   delete older pending requests for the same `(kind, day)` in the claim
   commit (supersede — a 20:15 window subsumes the 18:15 one). Oldest day
   first drains a multi-day backlog in reading order; enqueue time is not
   the key, because an analyst node that was down while the on-prem side
   kept ticking comes back to a queue whose enqueue order says nothing
   useful. (Amended 2026-08-10: cross-day ordering was originally left
   unspecified, and the implementation used enqueue time until then.)
3. Move the file `pending/ → claimed/`, stamping `claimed_by` (hostname) and
   `claimed_at`; commit; **push**. A rejected push means someone else moved
   first: rebase, and if the file is gone from `pending/`, skip it.
4. Run the agent through the **existing rails unchanged**: clean-tree check,
   `feedback_retries`, result validation, deterministic lint, rollback.
5. On success, one commit carries: the report, deletion of the claimed
   request, and a telemetry event in `queue/results/`. Push (rebase and
   retry on rejection — the analyst touches only `reports/` and the queue,
   so conflicts with ingest commits are rare and rebase-resolvable).
6. On failure: roll back the wiki edits, increment `attempts`; after 2
   attempts move the request to `failed/` with the error category, push.

**Telemetry rides the same channel.** The analyst cannot write the on-prem
`.state/`, so it commits the would-be `agent_runs.jsonl` record (plus the
ledger enrichment fields) as `queue/results/<run_id>-<event_id>.json`. The
next on-prem tick folds results into `.state/agent_runs.jsonl` and the
ingest ledger, deletes the files, and commits — `dbwiki stats` and the ELK
shipper see analyst runs exactly like local ones, one tick late.

**The on-prem side pulls before it folds.** (Amended 2026-09-23: the
original protocol only ever pulled on the analyst side, so after the
analyst's first push every on-prem push was rejected non-fast-forward and
`queue/results/` never reached the fold-in.) With `analyst.enabled` and
`report.push` on, each `dbwiki run` starts with `git fetch` + `git rebase
--autostash -X ours @{u}` (`queue.pull_before_fold`). A same-path conflict
on `reports/<day><suffix>.md` keeps the **analyst's** pushed version — the
agentic report supersedes the placeholder, never the other way round; during
a rebase upstream is git's "ours". Any failure is undone in full (rebase
aborted, HEAD, tree and stash list as before), warned about, noted on the
run-health event and named by `dbwiki health`; the tick continues without the
pull.

### Configuration

```yaml
analyst:
  enabled: false     # on-prem: true = enqueue escalated windows (and write
                     # the structured placeholder) instead of running the
                     # agentic adapter locally
  stale_hours: 26    # health: age at which a pending/claimed request is
                     # flagged; failed/ is always flagged
```

The analyst node runs the same repo and config; `dbwiki analyst [--once]`
is the entry point (cron or a watch loop), and `agents.adapter` /
`agents.<adapter>.strong` on *that* machine choose the cloud model. With
`analyst.enabled: false` everywhere, the system behaves exactly as before
this ADR.

### Health

`dbwiki health` gains one queue block, reported as telemetry (never as
database state): oldest pending age, claimed entries older than
`stale_hours` (a crashed analyst), and the `failed/` count with error
categories. Claim expiry was first deferred to a human hand-moving the file
back to `pending/`; since 2026-09-23 `claim()` does it itself
(`queue.reclaim_stale`, same `stale_hours` budget): a stale claim returns to
`pending/` with `attempts + 1`, or goes to `failed/` (`stale_claim`) once its
attempts run out.

## Consequences

- The DB-adjacent host loses its cloud API keys; the workstation never
  learns ES exists. The only shared credential surface is the wiki remote.
- The data crossing the boundary is digests + wiki content — unchanged from
  today (the wiki already lives on GitHub). If policy tightens to "no
  verbatim log lines off-prem", the fix is a redaction pass in the
  compactor, orthogonal to this ADR.
- Escalated analysis becomes eventually-consistent: the deep report lands
  when the workstation is on. The structured placeholder bounds the damage;
  the HTML page and routine reporting lose nothing.
- Two writers on the wiki remote. Mitigated by path partitioning (analyst:
  `reports/` + `queue/`; ingest: everything else) and rebase-retry; a
  same-path race on `reports/<day>.md` resolves as last-push-wins, which
  same-day supersede semantics already accept — except that the on-prem
  pull keeps the analyst's pushed report over an unpushed on-prem one.
- `dbwiki stats` tier comparisons now mix hosts; the record's `adapter` /
  `model` fields already carry the distinction.
- Replaying history (`backup-*` branches, wiki resets) must not resurrect
  stale queue entries: requests reference `run_id`s, and the fold-in step
  ignores results for run_ids already in the ledger.

## Rollout

1. **Phase 1 (this ADR's implementation):** escalated reports only —
   queue module, enqueue in `orchestrate.report`, `dbwiki analyst --once`,
   telemetry fold-in, health block, docs. `analyst.enabled: false` default.
2. **Phase 2:** `lint` and `research` requests (research also moves web
   egress off-prem; `--review-sources` follows).
3. **Deferred until proven necessary:** claim-expiry automation, webhook
   /push-triggered analysts (poll/cron suffices), redaction pass,
   multi-analyst arbitration beyond git's push lock.
