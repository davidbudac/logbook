# Langfuse trace export (optional)

`langfuse:` in `config/dbwiki.yaml` exports every attempted agent stage —
agentic and structured, success and failure — as one [Langfuse](https://langfuse.com)
trace: one generation-type observation per stage, recorded after the fact
from the same telemetry the ledger gets, linked to the prompt version it ran
under, and carrying one child per turn or tool call the agent CLI streamed.
Off by default, inert without the
`langfuse` package, and — like every telemetry path in this repo — a Langfuse
failure warns once on stderr and never fails or blocks a run
(`src/dbwiki/observability.py`).

## Setup

1. **A Langfuse instance.** Either self-hosted —
   `https://langfuse.com/self-hosting`, a docker-compose stack, which fits a
   machine already running the local ELK from `elk/` — or Langfuse Cloud
   (`https://cloud.langfuse.com`, free tier). Create a project and copy its
   public/secret API keys.

2. **The package** — a default dependency, installed by `uv sync`.

3. **Keys via environment** — preferred over keys in the config file, same
   stance as `DBWIKI_ES_PASSWORD`. The SDK reads them itself:

   ```sh
   export LANGFUSE_PUBLIC_KEY=pk-lf-...
   export LANGFUSE_SECRET_KEY=sk-lf-...
   ```

   For cron, put them in the crontab environment or a file the crontab
   sources — the export happens inside `dbwiki run`/`ingest`/`report`/
   `lint`/`research`/`analyst`, so whatever launches those needs the vars.
   (`public_key:`/`secret_key:` in the config block work too and win over
   the env when set; the secret in a checked-in file is on you.)

4. **Enable it** in `config/dbwiki.yaml`:

   ```yaml
   langfuse:
     enabled: true
     host: http://localhost:3000     # or https://cloud.langfuse.com
     #environment: onprem            # optional Langfuse environment label
   ```

5. **Register what code owns** (idempotent, safe to repeat):

   ```sh
   uv run dbwiki langfuse sync
   ```

   It creates the prompt versions ("Prompt versions") and the model price
   rows ("Model prices"). Nothing syncs during a pipeline tick; cron runs
   it daily at 08:05 (`langfuse-sync` in `config/schedule.json`), so a
   changed prompt block or price is registered within a day, or at once
   when you run it by hand.

6. **Verify**: run any agent stage, e.g. `uv run dbwiki ingest --db cdb1
   --date <day>` (or wait for the next cron tick), then open the Langfuse
   project — a `dbwiki-ingest` trace should be there. A misconfiguration
   shows up as a single `warning: langfuse export: ...` line on stderr
   (cron mail), never as a failed run.

## This deployment (2026-08-29)

Self-hosted Langfuse **v4**, stack at `/opt/langfuse` (upstream
`docker-compose.yml` + a local `docker-compose.override.yml`; secrets in a
0600 `.env`, never committed). `restart: always`, so it comes back with the
Docker daemon.

- **UI**: http://192.0.2.10:3000 — the tailnet address of this node
  (`dbhost`), equivalently http://dbhost.example.net:3000. Published on
  that address **and** loopback, but deliberately not on `0.0.0.0`: upstream
  publishes 3000 on every interface, which would put it on the home LAN too,
  and the traces carry real alert-log lines (see "Content stance" below).
  The tailnet is a private WireGuard mesh — 100.64.0.0/10 is CGNAT space, not
  routable from the internet — so this reaches your own devices only, unless
  someone enables Tailscale Funnel. Traffic is plaintext HTTP inside the
  tunnel, which Tailscale itself encrypts.
  The loopback entry is what dbwiki's own exporter uses (`langfuse.host` stays
  `http://localhost:3000`), so tracing does not depend on tailscaled.
  Binding a fixed IP means the container cannot start while `tailscale0` is
  down — after a reboot it will fail until Tailscale is up, then `restart:
  always` heals it. `NEXTAUTH_URL` must match the URL the browser uses; it is
  the tailnet IP, with `AUTH_TRUST_HOST=true` so the MagicDNS name works too.
  Upgrading to HTTPS via `tailscale serve` needs `sudo tailscale set
  --operator=$USER` once, which was not available here.
- **Bootstrap is headless**: `LANGFUSE_INIT_*` in `.env` create the org
  `dbwiki`, the project `dbwiki-pipeline`, and the API keys on first start —
  no click-through, and the keys are known before the stack is up.
- **Keys reach cron** as `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` lines
  in the user crontab's environment block, right after `PATH=`. Every dbwiki
  entry inherits them. Without them a stage warns once and skips the export.
- **MinIO's public port is 9095**, not the upstream 9090, which is taken on
  this host.

Two things that will bite whoever looks next:

- **The v4 server runs in `events_only` mode**, where the legacy read API is
  gone: `GET /api/public/traces` answers `404` with "not available on
  deployments running in Langfuse v4 events_only mode". Ingestion is
  unaffected — that endpoint is for reading. To check from a shell rather
  than the UI, query ClickHouse: `events_core` holds one row per observation
  with a truncated `input`/`output` preview, `events_full` the complete
  payload, `scores` the four scores. The legacy `traces` and `observations`
  tables stay empty by design; do not read their row counts as "nothing
  arrived".
- **Structured research exports no prompt** (`in_len: 0`). It is a loop of
  per-page prompts built inside `_structured_research`, so there is no single
  prompt to attach and `orchestrate` passes `prompt=None` for it deliberately
  (the comment at that call site says so). Ingest and the agentic stages do
  carry theirs.

## What one trace carries

- **Trace** `dbwiki-<task>` (`ingest`, `report`, `lint`, `research`), with
  `session_id` = the pipeline `run_id`. One scheduler tick shares one
  `run_id` across all its stages, so a tick reads as one Langfuse session;
  the same id is in the wiki commit's `Run-ID:` trailer, the ledger, and
  `.state/agent_runs.jsonl` → ELK, so a trace correlates with all of them.
  The trace **id** is seeded from the ledger line's `event_id`
  (`observability.trace_id`, the SDK's `create_trace_id(seed=...)` rule), so
  the workbench links each agent stage to its exact trace from the ledger
  alone (`portal.links.langfuse` in `config/dbwiki.yaml`). Stages exported
  before 2026-09-15 carry random ids, so their stage links open nothing.
- **Tags**: task, adapter (`codex`/`claude`/`ollama`/`pi`), model tier
  (`cheap`/`strong`), mode (`agentic`/`structured`).
- **`release`** = the short git sha of the *application* checkout (not the
  wiki's), read once per process, so a quality regression can be pinned to
  the commit that shipped it. **`environment`** = `langfuse.environment`
  (`onprem` here, `analyst` on the workstation node).
- **One generation observation** named after the task: the exact prompt as
  input and the agent's result JSON as output; model, token usage
  (`input`/`output`) and cost when the adapter reported them ("unknown" is
  omitted, never estimated — same rule as `dbwiki stats`); and metadata
  mirroring the ledger telemetry block: `run_id`, `db`, duration, timeout
  flag, attempts, pages touched, incidents opened/updated, validation and
  rollback outcome, lint findings, exit code, prompt/stdout byte counts.
  A failed stage is `level: ERROR` with `validation failed`/`rolled back`
  as the status message. Codex reports its tokens in the `codex exec --json`
  event stream — one `turn.completed` per turn, summed over the run — so
  `codex` and `ollama` runs carry token counts; codex reports no price, so
  their cost stays unknown and no cost is estimated for them.
  It links the **prompt version** of the static
  instruction block it ran under (see "Prompt versions"), so Langfuse can
  break the scores down by wording.
- **One child observation per turn**, when the adapter streamed them:
  assistant turns as generations carrying their per-turn usage, tool calls
  as tools, each with a ≤200-character preview as its input. `pi --mode
  json` and `codex exec --json` stream them; `claude
  --output-format json` does not, so claude stages stay flat. The harness
  caps the list, and a capped one says `steps_truncated: true` in the parent
  metadata.
- **Scores** on the trace, so Langfuse's dashboards can plot quality over
  time: `validation_ok` (boolean), `rolled_back` (boolean),
  `lint_findings` (numeric), `duration_s` (numeric).

Timing caveat: the observation is created after the run finishes, so the
span's own start/end are meaningless (~0s). The run's real duration is the
`duration_s` score and metadata field.

## Prompt versions

The instruction blocks change by commit, and a trace is worth much less if
you cannot tell which wording produced it. Each is stored as a Langfuse
prompt and linked from the generation that ran it
(`src/dbwiki/promptreg.py`):

| prompt | text |
|---|---|
| `dbwiki/ingest-structured` | `structured.INGEST_TEMPLATE` |
| `dbwiki/report-structured` | `structured._REPORT_CONTRACT` |
| `dbwiki/report-escalated` | the escalated contract plus `_ESCALATED_RULES` |
| `dbwiki/research-structured` | `research_structured._RESEARCH_CONTRACT` |
| `dbwiki/agents-md` | the wiki's `AGENTS.md`, for every agentic stage |

Code stays the source of truth and nothing is ever fetched to run: editing a
prompt in the Langfuse UI changes no pipeline behaviour, only a commit does.
Registration compares before it writes, because `create_prompt` with
identical text makes a new version every call; a stage whose text still
matches the stored `production` version reuses it. The ingest template keeps
its variable parts as `{{db}}`, `{{day}}` and `{{codes}}` mustache
placeholders so the stored prompt reads as a prompt in the UI; `build_prompt`
fills them with `str.replace`, never `str.format`, since the JSON contract is
full of braces.

Escalated and routine structured reports run different wording under the same
task and mode. What tells them apart in the telemetry is `model_tier:
strong`, which the orchestrator pins for every escalated report.

`dbwiki langfuse sync` registers all of them at once, so the versions exist
before the first run rather than one run later.

## Model prices

Langfuse ships around 100 built-in price rows and none of them match a local
unsloth build or the codex model, so token counts arrive with no cost
attached. `langfuse.models:` lists the rows this project owns and `dbwiki
langfuse sync` upserts them through `/api/public/models`:

```yaml
langfuse:
  models:
    - name: unsloth/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF
      match: "(?i)^unsloth/"
      input_per_million: 0
      output_per_million: 0
```

`match` is the regex Langfuse tests against the generation's model name, and
prices are USD per million tokens (the API takes per-token; the sync
divides). Model rows are immutable in the API, so a changed price is a delete
followed by a create; an identical row is left alone, and Langfuse's own
built-in rows are never deleted. Run the command by hand after changing the
block — nothing syncs during a pipeline tick, and an unreachable Langfuse
fails the command loudly and no run at all.

A local model priced at 0 is a real price. The codex row ships at 0 as a
marked *placeholder*: until someone fills in the vendor's list price, codex
runs read as free on every dashboard, which is a lie rather than a gap.

## Content stance

The rest of this repo's telemetry (ledger, `.state/agent_runs.jsonl`, ELK)
is counts and identifiers only — never a prompt, a log message, a page
body, or a credential. The Langfuse export is the deliberate exception:
content is Langfuse's actual value — "why did *this* ingest write a bad
incident page" needs the prompt and the result, which the ledger
deliberately does not keep — so the export carries both. Know what that
means: ingest prompts embed digest content, digests embed real alert-log
lines, and report prompts embed incident-page excerpts — all of it lives on
the Langfuse host. Point `host` somewhere you trust with that; with
Langfuse Cloud that content leaves your network. Credentials never appear
in prompts or results, so they never appear in traces either.

## Analyst node (ADR-0001)

The export runs wherever the stage runs. With the analyst split enabled,
on-prem structured stages export from the on-prem node and the analyst's
agentic report exports from the workstation — both under the same `run_id`
session, since the queued request carries the on-prem `run_id`. Each node
therefore needs its own network path (and keys) to the Langfuse host; a
node without them just warns and skips, it does not queue traces. The
on-prem node stays cloud-credential-free with a self-hosted Langfuse; using
Langfuse Cloud from the on-prem node would put a cloud credential on it —
your call whether that violates the spirit or only the letter of the split.

## What it is not

Per-turn tracing now exists for the adapters that stream (see "One child
observation per turn" above), but it is only as deep as their stdout. A
`claude` stage stays flat, because `--output-format json` emits one final
object; `stream-json` would fix that and is not wired up. Nothing here
instruments the model's own reasoning, and the previews are capped at 200
characters, so a child observation tells you which tool ran with roughly what
argument, not the full exchange.

## Experiments — `dbwiki eval` (issue 01)

The trace export above answers "what did this run do". It cannot answer
"which local model should run it", because every trace is a different day's
digest. `dbwiki eval` fixes the input so the model is the only variable.

**The dataset.** `dbwiki-ingest`, seven items, built by
`src/dbwiki/evaluate.py: dataset_items` from `tests/fixtures/real_digests/`
joined to `tests/fixtures/golden/real/<name>/decision.json`:

```
input           {db, digest}          the sanitized digest, verbatim
expected        {outcome, model_tier, codes}
metadata        {source}              the fixture path it came from
id              the fixture name      so a re-sync upserts, never duplicates
```

The corpus is the same one `tests/test_real_digests.py` guards: both trigger
outcomes, both tiers, a 1,753-event storm, a 67-group day that trips the
prompt cap, and two days where nothing happened. `dbwiki eval sync` publishes
it. Re-run it whenever a fixture changes.

**The run.** `dbwiki eval run [--model M] [--provider P] [--name N]
[--only ITEM]` replays each item through the real ingest chain —
`build_prompt` -> `propose` -> `apply_proposal` — with both `agents.pi` tiers
pinned to `M`, each item in its own throwaway wiki under `/tmp`. The
operator's wiki, the ledger and `.state/` are untouched, so it is safe to run
while a scheduler tick is due. `--only` takes one item at a time, which is how
you sanity-check a newly loaded model without spending an hour.

**The scores** are mechanical facts about the proposal; no model grades
another model.

| score | what it catches |
|---|---|
| `parsed_ok` | the contract held, after the one retry `propose` allows |
| `apply_ok` | the deterministic writers accepted it |
| `codes_allowlisted` | share of proposed error codes the digest carries; 1.0 when it proposed none. Below 1.0 is invention |
| `incident_consistent` | it did not ask to update an incident page that is not open |
| `flags` | how much the rails had to flag for a human |
| `duration_s`, `input_tokens`, `output_tokens` | straight from the harness telemetry; "unknown" stays unknown and is left out of the averages |

A failed item is a measurement, not a crash: the error lands in the row and
the run continues.

`SCORES` in `evaluate.py` names each of these once, and both the printed table
and the Langfuse evaluators are generated from it — a score cannot exist in
one and be missing from the other.

**Where it lands.** With `langfuse.enabled`, the run is a
`client.run_experiment` on the `dbwiki-ingest` dataset, run name
`<model> <YYYY-MM-DD>`, metadata `{model, provider, app_sha}`, one Evaluation
per score, `max_concurrency=1` because there is one local model server and
each item takes minutes. Experiment traces arrive under
`environment=sdk-experiment`, not the `onprem` environment the pipeline
export uses, so they never mix with production traces. Langfuse is the
transport and never the measurement: no keys, no package, or an unreachable
server all degrade to the same printed table with one stderr warning.

**The second dataset stays local.** `dbwiki eval history` replays a real wiki's
own digests through the same chain twice to measure what the database-history
block changes (docs/how-it-works.md, section 4). It publishes no dataset, runs
no experiment and exports no traces, deliberately: its items are days out of
one operator's wiki rather than sanitized fixtures, and its two arms are only
comparable inside a single run against a single checkout. A dataset and a run
name would promise a comparison across runs that the data cannot support, and
the wiki revision the report's header names is the only thing that makes two of
those reports mean the same. The report file is the artifact; the fixture eval
above keeps the transport.
