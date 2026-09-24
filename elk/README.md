# elk/ — dbwiki telemetry → local ELK stack

Ships everything the dbwiki pipeline writes about *itself* — run health, cron
output, the agent audit trail, per-db wake/skip decisions, per-digest ledger
events — into a local Elasticsearch, with a ready-made Kibana dashboard.
Everything needed to recreate the setup on another machine lives in this
folder; nothing outside the repo is modified except the user crontab.

Note the direction: dbwiki *reads* Oracle logs from this same ES cluster
(`logs-oracle.*`); this folder *writes* dbwiki's own telemetry back into it
(`logs-dbwiki.*`). Two independent concerns that happen to share a cluster.

## Architecture

```
 .state/run_health.jsonl ─────────────┐ (tail, _id = run_id)
 .state/cron.log ─────────────────────┤ (tail + multiline fold)
 wiki/log.md ─────────────────────────┤ (tail, _id = sha256(line))
                                      │
 .state/ingest_ledger.json ──┐        │
 .state/run_health.jsonl ────┤        │
                    (cron */30)       │
              scripts/emit_derived.py │
                             │        │
        .state/elk/db_runs.jsonl ─────┤ (tail, _id = event_id)
        .state/elk/ledger_events.jsonl┤ (tail, _id = event_id)
                                      ▼
                        filebeat (docker, this folder)
                                      ▼
                    ES http://localhost:9200  (5 data streams)
                    index template dbwiki-telemetry
                    default_pipeline: dbwiki-router → dbwiki-* pipelines
                                      ▼
                    Kibana dashboard "dbwiki — pipeline observability"
```

| Data stream | Source | One doc per |
|---|---|---|
| `logs-dbwiki.run_health-default` | `.state/run_health.jsonl` | dbwiki command invocation |
| `logs-dbwiki.cron-default` | `.state/cron.log` | cron output line (tracebacks folded) |
| `logs-dbwiki.audit-default` | `wiki/log.md` | agent LLM stage (`[UTC] task — summary`) |
| `logs-dbwiki.agent_runs-default` | `.state/agent_runs.jsonl` | attempted LLM stage, with tokens/cost |
| `logs-dbwiki.db_runs-default` | derived from run_health `dbs[]` | (run, database) pair |
| `logs-dbwiki.ledger-default` | derived from `ingest_ledger.json` | observed ledger entry change |
| `logs-dbwiki.run_starts-default` | `.state/elk/run_starts.jsonl` | command start (dedup key: `event_id`) |
| `logs-dbwiki.schedule-default` | `.state/elk/schedule.jsonl` | schedule entry per snapshot (dedup key: `event_id`) |
| `logs-dbwiki.queue_state-default` | `.state/elk/queue_state.jsonl` | analyst queue snapshot (dedup key: `event_id`) |
| `logs-dbwiki.alerts-default` | `.state/alerts.jsonl` | alert record (dedup key: sha256 of `fingerprint`\|`at`) |

`agent_runs.jsonl` is written by the pipeline itself (`health.record_agent_run`,
called from the orchestrator's telemetry choke point for every ingest / report /
lint / research attempt, success or failure). Each line carries duration,
exit code, prompt/stdout sizes, and flattened token usage — `input_tokens`,
`output_tokens`, `cost_usd`, with `usage_known: false` marking runs whose
adapter reported none. Usage comes from `claude --output-format json`, codex's
token-usage line, and `pi --mode json` (real counts from LM Studio, cost 0 for
local models).

The two derived streams exist because their sources cannot be tailed:
`ingest_ledger.json` is rewritten whole every run, and `run_health.jsonl`'s
per-db detail sits in an array that ES ingest pipelines cannot fan out into
separate docs. `scripts/emit_derived.py` (stdlib-only, idempotent, state in
`.state/elk/emitter_state.json`) does both conversions.

The emitter is loud about broken input: a source that exists but cannot be
read or parsed produces one `emit_derived: <source>: <reason>` line on stderr
and exit code 1, after the sources that *did* work have been emitted — so the
cron entry's `>> .state/elk/emitter.log 2>&1` now records why a stream went
quiet instead of the script returning 0 with nothing to show. A file that was
never created (no `run_health.jsonl` yet, no analyst `queue/`) is not a
problem, and with no `queue/` the queue snapshot is skipped entirely rather
than shipping a zeroed doc every 30 minutes forever. The ledger reader accepts
both `ingest_ledger.json` shapes — the versioned `{"schema_version", "entries"}`
wrapper *and* the legacy bare flat mapping that `src/dbwiki/state.py` still
tolerates (and that the live file still is); reading only `entries` used to
ship an empty ledger stream, silently.

**Dedup is the load-bearing design decision.** `run_health.jsonl` is trimmed
to its last 2000 lines (`health.py`, published atomically via tmp+rename, so
filebeat sees a new inode and re-reads from offset 0); the derived files are
rotated by truncate-and-rewrite, with the same effect.
Every doc from a rewritable source therefore carries a deterministic `_id`
(`run_id`, `event_id`, or a content hash) — ES data streams use `create` ops,
so re-shipped duplicates are dropped, never double-counted. Only `cron.log`
(pure `>>` append) relies on offsets alone.

All parsing lives in ES ingest pipelines, not filebeat: the index template
sets `default_pipeline: dbwiki-router`, which dispatches to one pipeline per
stream (`elasticsearch/pipelines/`). Parse failures tag the doc
`dbwiki_pipeline_failure` and keep the raw message — nothing is dropped.

Known limitation: `cron.log` lines carry no timestamps, so their `@timestamp`
is filebeat's read time — accurate while tailing live, wrong for a first-time
historical backfill (all pre-existing lines land at setup time).

## Files

```
docker-compose.yml            filebeat container (host network → localhost:9200)
filebeat.yml                  the five inputs + dedup config; no parsing here
.env.example                  copy to .env; stack version, hosts, credentials
setup.sh                      idempotent: pipelines + template + kibana import
elasticsearch/
  index-template.json         logs-dbwiki.* data streams, mappings, router hookup
  pipelines/dbwiki-*.json     router + one ingest pipeline per stream
kibana/
  build_dashboards.py         source of truth for all Kibana saved objects
  dbwiki-dashboards.json      generated by setup.sh (committed for reference)
scripts/
  emit_derived.py             ledger + per-db-run JSONL emitter (cron */30)
```

## Setup on a new machine

Prerequisites: docker with compose v2, python3, an ELK stack ≥ 8.x reachable
over HTTP (tested against [docker-elk] 9.4.2 on the same host), and this repo
with its cron jobs producing `.state/` telemetry.

```sh
cd elk
cp .env.example .env          # set ES/Kibana hosts + credentials, stack version
./setup.sh                    # pipelines, index template, dashboards, first emit
docker compose up -d          # start the filebeat shipper
```

Then add the emitter to the user crontab (also listed in `docs/scheduling.md`):

```cron
*/30 * * * * cd /path/to/logbook && python3 elk/scripts/emit_derived.py >> .state/elk/emitter.log 2>&1
```

Order matters once: `setup.sh` must run before filebeat's first shipment, or
the first docs are indexed without mappings/parsing. If that happens, delete
the streams and registry and start over:

```sh
docker compose down -v        # -v drops the filebeat registry volume
curl -u "$ES_USER:$ES_PASSWORD" -XDELETE "$ES_HOST/_data_stream/logs-dbwiki.*"
./setup.sh && docker compose up -d
```

`setup.sh` is safe to re-run any time — everything it does is a PUT/overwrite.
Re-run it after editing pipelines, the template, or `build_dashboards.py`.

## The dashboards

- `http://localhost:5601/app/dashboards#/view/dbwiki-pipeline` —
  **dbwiki — pipeline observability**. Four bands, top to bottom:
  command-level health (run counts, outcomes, error categories, duration by
  command), per-database behaviour (wake vs skip, the reason codes, event
  volumes), agent/ledger detail (stage timings, per-digest statuses), and the
  two raw streams (cron log, agent audit trail).
- `http://localhost:5601/app/dashboards#/view/dbwiki-agent-runs` —
  **dbwiki — agent runs**. One row per LLM stage attempt: token and cost
  totals, runs/duration/tokens over time by task, a per-model breakdown
  (runs, avg duration, tokens, cost), problem runs (validation failures,
  rollbacks, timeouts) and the raw run list.
- `http://localhost:5601/app/dashboards#/view/dbwiki-loop-overview` —
  **dbwiki — loop overview**. The agent loops themselves, round by round:
  a per-loop status table (last run, last outcome, run/failure counts,
  median duration per command), what's scheduled next (from
  `config/schedule.json` via `emit_derived.py`), rounds over time stacked
  by loop, failed rounds by error category, every recorded round as a
  list, and the agent stages inside each round (joinable on `run_id`).
  A compact bottom strip keeps the operational signals: recent/in-flight
  starts (a start with no matching `run_health` doc is still running or
  died), the queue snapshot, and alerts over time by category.

Dashboards are generated by `kibana/build_dashboards.py` — edit that, not the
Kibana UI, and re-run `setup.sh` (fixed object ids make re-imports overwrite
in place; UI edits are lost on the next import). The script's output is
imported via the saved-objects `_bulk_create` API deliberately: the `_import`
API treats hand-written objects without migration stamps as ancient and runs
legacy Lens migrations that reject modern state.

## Verifying / troubleshooting

```sh
docker logs dbwiki-filebeat --since 5m          # shipper health
curl -su "$ES_USER:$ES_PASSWORD" "$ES_HOST/_cat/indices/.ds-logs-dbwiki*?h=index,docs.count"
curl -su "$ES_USER:$ES_PASSWORD" "$ES_HOST/logs-dbwiki.*/_count?q=tags:dbwiki_pipeline_failure"
```

- **No docs at all**: check filebeat can reach ES (`docker logs`), and that
  `.env` credentials are right. The container uses host networking, so
  `localhost:9200` in `.env` means the host's ES.
- **Docs but unparsed fields**: filebeat started before `setup.sh`; see the
  reset recipe above.
- **`dbwiki_pipeline_failure` tagged docs**: an ingest pipeline threw; the raw
  message survives on the doc. Fix the pipeline JSON, re-run `setup.sh`.
- **db_runs/ledger streams stale**: the emitter cron entry is missing or
  failing — check `.state/elk/emitter.log`.
- **Duplicate-looking counts**: should not happen by design; if it does, check
  that the `_id`-bearing inputs in `filebeat.yml` still set `document_id`.

[docker-elk]: https://github.com/deviantony/docker-elk
