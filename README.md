<p align="center"><img src="logo-transparent.png" alt="Logbook" width="360"></p>

# Logbook

**An evolving knowledge base built from database logs.**

[![ci](https://github.com/davidbudac/logbook/actions/workflows/ci.yml/badge.svg)](https://github.com/davidbudac/logbook/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)

Logbook reads Oracle alert, listener and Data Guard logs out of Elasticsearch,
works out deterministically whether anything happened on each database, and
only then asks an LLM to write it up. The result is a git-versioned markdown
wiki: a journal per database, incident pages, error-class pages with cause
and past fixes, fleet reports, and one HTML page per day for the on-call DBA.

The shape is Karpathy's [LLM wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
(raw sources / wiki / schema) adapted for a repetitive, high-volume event
stream: deterministic classification *before* the model, controlled
publication *after* it.

```
Elasticsearch  ~1M docs/day
      -> Compactor   deterministic Python, no model
      -> Digest      ~30 lines for a boring day
      -> Trigger     skip (most windows) | wake
      -> Agent       cheap or strong model tier
      -> Rails       validate, lint, commit or roll back
      -> Wiki        markdown + git
      -> html/index.html   the page you read
```

## Status

Solo-maintained, v0.1.0, the first public release. It has run daily against
one Oracle fleet since July 2026, with the settings and measurements recorded
in [CHANGELOG.md](CHANGELOG.md). Expect to adapt `patterns/` and `config/` to
your own log shipping. Issues and pull requests are welcome; there is no
support commitment.

The project is Logbook. The CLI, Python package, config file and telemetry
prefixes are `dbwiki`, and stay that way.

## Why this shape

Most of a database log is noise. Letting a model paginate raw Elasticsearch
hits is slow, expensive, and drowns the signal. So the pipeline is split into
three layers with a hard boundary between them:

| Layer | What it does | Never does |
|---|---|---|
| **Compactor** (`src/dbwiki/compactor.py`) | classifies every event against ordered regex libraries (`patterns/`), counts the routine, keeps the notable verbatim, spots deltas against a per-database registry: first-ever ORA codes, new services, rate anomalies, silence, errors right after a change | interpret |
| **Agents** (`harness.py`, `orchestrate.py`, `structured.py`) | say what a notable digest *means*, attach it to the database's history, write prose. Runs `claude`, `codex`, `ollama` or `pi` as a subprocess, agentic or structured (one JSON answer, deterministic code writes every file) | decide what happened, touch git, invent a citation |
| **Rails** (`orchestrate.py`, `lint.py`, `provenance`) | validate the result, run the deterministic lint, commit or roll the wiki back, record telemetry | let an unverified page land |

The wiki itself is a separate git repository. Every commit carries a run id
that joins to the ingest ledger, the run-health log and the trace exporter.

## What it produces

- `journal/<db>.md`, one line per notable window, in the database's own words.
- `incidents/`, opened and updated by the model, resolved only by an operator.
- `errors/<CODE>.md`, one page per error code: occurrences, researched cause
  and action from approved sources, and a `Past fixes` table regenerated from
  the incident corpus.
- `reports/<day>.md`, a fleet report per tick.
- `html/<day>.html`, a deterministic daily page: needs attention, worth a
  look, routine.
- An incident workbench and weekly review inbox served by `dbwiki portal`.

## Does this fit your setup?

- **Your Oracle alert, listener and Data Guard logs are already in
  Elasticsearch.** Logbook reads, it does not ship. It expects the ECS-shaped
  documents filebeat's Oracle module produces (or the legacy layout, see
  `sources.*.db_fields` in the config); `elk/` has the index template, ingest
  pipelines and a docker-compose stack that reproduce the layout it was
  built against.
- Python 3.13+, [`uv`](https://docs.astral.sh/uv/) and `git`.
- At least one agent CLI on `PATH`: `claude`, `codex`, `ollama`, or `pi` with a
  local model server. Only the adapters you configure need to exist.
- A second git repository for the wiki, see below.

Runtime dependencies are `requests`, `pyyaml` and `langfuse` (the trace
exporter, inert unless enabled). Everything else is stdlib.

## The wiki repo

The knowledge base is a separate git repository checked out at `wiki/`,
because the model writes into it and the orchestrator commits or rolls back
every run there. It has a contract, `AGENTS.md`, that every agent writes
against, and a fixed tree of page types. `wiki-template/` ships both:

```sh
cp -r wiki-template wiki
git -C wiki init && git -C wiki add -A && git -C wiki commit -m "empty wiki"
```

Point `report.link_base` in the config at wherever you push it, and every
daily page links back to the wiki it was rendered from.

## Quick start

```sh
git clone https://github.com/davidbudac/logbook.git
cd logbook
uv sync
cp -r wiki-template wiki && git -C wiki init        # the knowledge base, its own repo
cp config/dbwiki.yaml.example config/dbwiki.yaml    # then edit; git ignores it
export DBWIKI_ES_PASSWORD=...

uv run dbwiki health                       # is everything it needs actually up?
uv run dbwiki dbs                          # which databases does Elasticsearch know?
uv run dbwiki compact --db cdb1 --date 2026-07-10   # one digest, no model
uv run dbwiki run --explain                # what would a tick do right now, and why?
uv run dbwiki ingest --db cdb1 --date 2026-07-10 --dry-run   # see the prompt, spend nothing
uv run dbwiki run                          # one adaptive tick
```

Only `compact` and the mutating stages touch disk. Every `--explain` and
`--dry-run` is safe on a live deployment.

## Commands

| Command | Does |
|---|---|
| `dbwiki run [--consolidate]` | one adaptive tick: compact, wake the model only for notable windows, render the daily HTML |
| `dbwiki compact`, `backfill` | digests without a model |
| `dbwiki ingest`, `report`, `lint`, `research` | the model stages, each behind the same validate / lint / rollback rails |
| `dbwiki health [--alert]` | collection, staleness, failures, backlog, dependencies; exit code says how bad |
| `dbwiki retry` | re-ingest failed digests, non-destructively |
| `dbwiki stats` | cost and quality per task, adapter and model tier |
| `dbwiki review` | weekly attention review into the workbench inbox |
| `dbwiki portal serve` | the incident workbench |
| `dbwiki eval`, `dbwiki redact`, `dbwiki awr` | evaluate models on real digests, preview what leaves the box, feed AWR summaries |

The full list with every flag is in [docs/reference.md](docs/reference.md).

## Documentation

| | |
|---|---|
| [docs/start-here.md](docs/start-here.md) | what it does for you, your first ten minutes, the jobs that are yours |
| [docs/overview.md](docs/overview.md), [docs/how-it-works.md](docs/how-it-works.md) | the idea and the pipeline end to end |
| [docs/user-guide.md](docs/user-guide.md), [docs/monitoring.md](docs/monitoring.md) | setup, config, the workbench, `dbwiki health`, the runbook |
| [docs/reference.md](docs/reference.md) | every knob, state file and telemetry field |
| [docs/scheduling.md](docs/scheduling.md) | the crontab, the wake/skip decision, which model each stage spends |
| [docs/adr/](docs/adr/) | design decisions: on-prem/analyst split, anonymized research offload, incident lifecycle, review delivery, practitioner caveats, history in the ingest prompt |
| [DESIGN.md](DESIGN.md) | the architecture and its rationale |
| [CHANGELOG.md](CHANGELOG.md) | what changed when, with the measurements behind current settings |
| [docs/site.html](docs/site.html) | the five guides as one page with diagrams; `uv run python docs/build_site.py` regenerates it |

`index.html` and `website/` are the project landing page; open the repo root
with `python3 -m http.server` to see it locally.

## Repository layout

```
src/dbwiki/             compactor, orchestrator, adapters, structured writers, portal
src/dbwiki_researcher/  the off-box researcher (ADR-0002)
patterns/               ordered regex libraries per log source
config/                 dbwiki.yaml.example, schedule.json, local-model presets
wiki-template/          AGENTS.md and the empty page tree a new wiki starts from
tests/                  pytest suite with golden digests and real-digest fixtures;
                        portal_shots.py renders workbench screenshots (not a test)
docs/                   guides, references, ADRs
elk/                    index template, ingest pipelines, Kibana dashboards, telemetry shipper
n8n/                    optional n8n workflows generated from config/schedule.json
deploy/                 systemd user units, chaos scripts for a lab
website/, index.html    the project website
```

## Development

```sh
uv sync
uv run pytest tests/
uv run ruff check .
```

[CONTRIBUTING.md](CONTRIBUTING.md) has the rules that save a round-trip:
goldens, formatting, the generated site. [SECURITY.md](SECURITY.md) says what
to keep private when you deploy.

## Privacy

Prompts embed real log lines. Nothing leaves the machine unless you point an
adapter at a hosted model, enable the Langfuse exporter, or turn on research.
Research can run fully off-box on anonymized requests (`dbwiki redact` shows
exactly what would be sent). See [docs/adr/0002](docs/adr/0002-offload-research-anonymized.md).

## License

[MIT](LICENSE).
