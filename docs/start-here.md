# Start here

The short version: what the pipeline does for you, what it needs from you,
and the handful of jobs that are actually yours. Every job here links to the
page that covers it properly.

## What it does for you

You have millions of Oracle alert, listener and dataguard events in
Elasticsearch and no time to read them. This pipeline reads them for you every
two hours, decides *deterministically* whether anything happened, and only then
asks an LLM to write it up — as a journal line, an incident, an error-class
page, a fleet report — into a git-versioned markdown wiki you can actually read.

<!-- figure: pipeline | Deterministic Python decides what happened; a model is only woken to say what it means. -->

```
Elasticsearch  ~1M docs/day
      -> Compactor   deterministic python, no model
      -> Digest      ~30 lines for a boring day
      -> Trigger     skip (most windows) | wake
      -> Agent       cheap or strong tier
      -> Rails       validate, lint, commit or roll back
      -> Wiki        markdown + git
      -> html/index.html   the page you read
```

## Who does what

| | does | never does |
|---|---|---|
| **the machine** | reads ES, classifies, counts, spots deltas, decides *notable*, writes digests and the daily HTML, commits or rolls back | interpret |
| **the model** | says what a notable window *means*, attaches it to history, writes prose | decide what happened, touch git, invent a citation |
| **you** | read the daily page, fix the causes health reports, approve sources, keep the wiki tree committed | hand-edit `digests/` or `html/`, close an incident because events stopped |

## Your first ten minutes

```sh
uv sync                                   # python 3.13+, thin deps
export DBWIKI_ES_PASSWORD=...             # never commit a real password
uv run dbwiki health                      # is everything it needs actually up?
uv run dbwiki dbs                         # which databases does ES know about?
uv run dbwiki run --explain               # what would a tick do right now, and why?
uv run dbwiki ingest --db cdb1 --date 2026-08-29 --dry-run   # see the prompt, spend nothing
```

Only `compact` (which writes digests) and the mutating stages touch disk; every
command above is safe to run on a live deployment. Full setup, config and the
command list: [user-guide.md](user-guide.md).

## The jobs you will actually do

<!-- figure: jobs -->

| I want to | do this | covered in |
|---|---|---|
| see what happened overnight | open `wiki/html/index.html` | [user-guide.md](user-guide.md) |
| know why a database produced nothing | `dbwiki run --explain` | [user-guide.md](user-guide.md) |
| fix a batch of failed ingests | `dbwiki health`, fix the cause, `dbwiki retry` | [monitoring.md](monitoring.md) |
| understand an ORA code we have hit before | read `wiki/errors/<CODE>.md` | [user-guide.md](user-guide.md) |
| re-do one day for one database | `dbwiki compact` then `dbwiki ingest` | [user-guide.md](user-guide.md) |
| change which model a stage spends | edit `agents.*`, then `dbwiki health` and `dbwiki stats` | [user-guide.md](user-guide.md) |
| work out why the 06:15 tick did nothing | `.state/cron.log`, then `dbwiki health` | [monitoring.md](monitoring.md) |
| bootstrap a year of history | `dbwiki backfill --from … --to …` | [user-guide.md](user-guide.md) |

## A day in the life

Cron does the work; your part is the two lanes at the bottom. Nothing here
needs you unless something is wrong.

<!-- figure: rhythm | The schedule is the authority — docs/scheduling.md documents it, config/schedule.json mirrors it for the telemetry emitter. -->

```
every 2h :15   dbwiki run             compact -> decide -> ingest woken dbs -> report -> render HTML -> alerts
23:30          dbwiki run --consolidate   the day's fleet report, whether or not anything was notable
Mon 08:00      dbwiki lint            provenance + an LLM health check over the wiki
Mon 09:00      dbwiki research        fill in Reference sections for error pages
Mon 09:30      dbwiki research --caveats   practitioner notes from the approved sources (web)
Mon 10:00      dbwiki review          what needs a human this week -> the workbench inbox
1st of month   dbwiki research --sources-only --review-sources
every 30m      elk/scripts/emit_derived.py   telemetry snapshots

you, daily     glance at wiki/html/index.html
you, on noise  dbwiki health, then the runbook in monitoring.md
```

## Four rules that keep it safe

1. **Never hand-edit `wiki/digests/` or `wiki/html/`.** Machine output; your
   edit is overwritten and an agent that touches them is rolled back.
2. **Never leave the wiki tree dirty.** Uncommitted changes outside those two
   directories abort the next agent run (`dirty_tree`) — because a failed run
   rolls the tree back and would take your edits with it.
3. **`wiki/sources/` pages enter by human commit only.** Research may cite an
   approved source's domains; it may not approve one.
4. **Silence is not recovery.** Nothing auto-closes an incident; recovery needs
   a resumed flow or your note in `.state/recovery_annotation.txt`.

## Where to go next

| You want to | Read |
|---|---|
| the concepts and the vocabulary, precisely | [overview.md](overview.md) |
| run it day to day | [user-guide.md](user-guide.md) |
| keep it alive and fix it | [monitoring.md](monitoring.md) |
| the mechanism, stage by stage | [how-it-works.md](how-it-works.md) |
