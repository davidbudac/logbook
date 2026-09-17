# Overview

The entry point for the whole project: what it is, why it is shaped this way,
what it produces, and where to read next. Written 2026-08-29 against the
running configuration.

## What it is

**Logbook** turns a firehose of Oracle log events into a small, curated,
git-versioned markdown wiki that a DBA can actually read.

Every two hours it reads the day's alert / listener / dataguard events for
every database out of Elasticsearch and boils them down deterministically.
Only when the result contains something worth a human's attention does it ask
an LLM to write that up: a journal line, an error-class page, an incident, a
fleet report. The wiki is the product; Elasticsearch stays the source of truth
and every wiki claim points back into it.

Two repositories, side by side:

| | |
|---|---|
| **machinery** (this repo) | Python compactor, orchestrator, harness, CLI, pattern libraries, config, telemetry shipping |
| **wiki** (`wiki/`, its own git repo) | the curated knowledge — db profiles, journals, incidents, error-class pages, fleet reports, plus machine output under `digests/` and `html/` |

## The one big idea: compaction before cognition

There are ~1M log docs in the local cluster and 99% of them are the same three
lines repeating (`Archived Log entry NNNN added`, listener heartbeats). Letting
a model paginate raw hits would be slow and expensive, and would bury the
signal in the repetition.

So a **deterministic compaction layer sits between Elasticsearch and the LLM**.
Plain Python, no model: a (database × window) slice becomes a small structured
*digest* — counters for the routine, verbatim text plus ES doc ids for the
notable, and a list of deltas against everything seen before. A boring day is
~30 lines; an interesting day maybe 200. That is all the model ever reads.

The consequence that shapes everything else: **the machine decides what
happened, the model decides what it means.** "Notable" is a deterministic
verdict from the compactor, never a model's opinion — the LLM is never woken
just to conclude that a window was boring.

## The layers

<!-- figure: layers | Each layer only ever talks to the one below it. The model lives at layer 2 and never reaches past layer 1 — it reads digests, not Elasticsearch. -->

```
┌───────────────────────────────────────────────────────────────┐
│ 4. Consumers   daily HTML page · fleet reports · Q&A sessions │
│                · lint agent · Kibana / Langfuse telemetry     │
├───────────────────────────────────────────────────────────────┤
│ 3. Wiki (git)  databases/ journals/ incidents/ errors/        │
│                reports/ sources/ index.md log.md              │
├───────────────────────────────────────────────────────────────┤
│ 2. Agents      one harness contract, pluggable adapters       │
│                (codex | claude | ollama | pi), two modes:     │
│                agentic (model edits the wiki) and structured  │
│                (model returns JSON, Python writes the files)  │
├───────────────────────────────────────────────────────────────┤
│ 1. Compactor   ES query → classify → group → deltas →         │
│                digest JSON + MD twin, watermark, notable verdict │
├───────────────────────────────────────────────────────────────┤
│ 0. Raw sources Elasticsearch — oracle alert/listener/dataguard │
│                indices and data streams (immutable truth)      │
└───────────────────────────────────────────────────────────────┘
```

`DESIGN.md` is the long-form version of this diagram, including the parts that
were designed for but not built (per-`sql_id` pages, more sources, local
search).

## What it produces

1. **Digests** — `wiki/digests/<db>/<date>.json` + a readable `.md` twin.
   Machine-facing, regenerated idempotently, never edited by an agent.
2. **Wiki pages** — journals (always), error-class pages, incidents, db
   profiles. Every observed claim cites the digest it came from.
3. **Fleet reports** — `wiki/reports/<day>[-HHMM].md`, one per notable window
   and one per daily consolidation.
4. **A daily DBA page** — `wiki/html/<day>.html` + `index.html`, rendered
   deterministically every tick: *Needs attention*, *Worth a look*, collapsed
   *Routine*.
5. **Telemetry about itself** — run-health events, per-agent-run cost/duration,
   the ingest ledger; optionally shipped to ELK/Kibana and Langfuse.

## Example deployment

One deployment (the author's), as of 2026-08-29 (`config/dbwiki.yaml`, `crontab -l`):

| Stage | Path | Runs on |
|---|---|---|
| compaction | deterministic Python | free, local, every tick |
| ingest | structured | local Nemotron-3.5-Lightning-30B-A3B via `pi` → unsloth studio |
| report, routine window | structured | same local model |
| report, notable window | agentic | `gpt-5.6-luna` via `codex` (cloud) |
| lint | agentic — always | `gpt-5.6-luna` via `codex` |
| research, error pages | structured (deterministic fetch + summarize) | local model, no browsing |
| research, source review | agentic — always | `pi` + `pi-web-access` |

Schedule: a 2h adaptive tick, a 23:30 consolidation, weekly lint and research,
a monthly source review, and a 30-minute telemetry emitter. `docs/scheduling.md`
is the reference for the crontab and the model routing.

The two optional distributed shapes are **off** here: `analyst.enabled` is
unset (ADR-0001, agentic reports delegated to a workstation) and
`research.mode` is `structured`, not `offload` (ADR-0002, anonymized research
on a cloud box).

## Vocabulary

Words used precisely throughout the docs and the code:

- **digest** — compactor output, machine-facing (layer 1). **report** —
  reporter output, human-facing (layer 3). Never the same artifact.
- **notable** — the compactor's deterministic verdict that a window contains
  something outside the routine classes, or a delta. It is what wakes the LLM.
- **delta** — something never seen before or out of line with the baseline:
  `first_ever_code`, `silence`, `rate_anomaly`, `new_service`,
  `new_client_program`, `after_change`.
- **tier** — `cheap` or `strong`, chosen per database per window:
  `strong` when the digest carries incident-grade evidence.
- **agentic** vs **structured** — whether the model edits wiki files itself or
  returns one JSON proposal that deterministic Python writes.
- **rails** — the non-negotiable checks around every agent run: single-flight
  lock, clean-tree requirement, result validation, provenance lint, rollback,
  ledger entry.
- **ledger** — `.state/ingest_ledger.json`: per digest, whether it was ingested
  or failed, with the content hash and failure category `dbwiki retry` needs.
- **watermark** — per-db window end already processed; makes ticks incremental.
- **run id** — one id shared by the run-health event, the agent-run telemetry,
  the wiki commit's `Run-ID:` trailer and the Langfuse trace.

## Where to read next

| You want to | Read |
|---|---|
| understand the pipeline end to end | [how-it-works.md](how-it-works.md) |
| run it, day to day | [user-guide.md](user-guide.md) |
| watch it, and fix it when it breaks | [monitoring.md](monitoring.md) |
| know what is scheduled and which model each stage spends | [scheduling.md](scheduling.md) |
| know what an agent is allowed to write | `wiki/AGENTS.md` |
| know how claims are evidenced and linted | [provenance.md](provenance.md) |
| see the architecture rationale and the deferred extensions | `../DESIGN.md` |
| see what changed when, with measurements | `../CHANGELOG.md` |
