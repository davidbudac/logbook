# User guide

How to run this thing day to day: setup, the commands you will actually type,
recipes for common jobs, and the rules a human sharing the wiki repo has to
respect. For what happens under the hood see [how-it-works.md](how-it-works.md);
for keeping it alive see [monitoring.md](monitoring.md).

---

## Setup

```sh
uv sync                                            # Python 3.13+, thin deps
cp config/dbwiki.yaml.example config/dbwiki.yaml   # or edit in place
export DBWIKI_ES_PASSWORD=...                      # never commit a real password
```

`config/dbwiki.yaml` is your deployment's file and is not meant to be
committed; the example ships the docker-elk lab default password. Set
`DBWIKI_ES_PASSWORD` in your shell **and** in the crontab's environment.

Optional extras:

```sh
pi install npm:pi-web-access  # only if research runs agentic through pi
```

What must exist on `PATH`: `uv`, `git`, a reachable Elasticsearch, and the
adapters you actually configured (`codex` and/or `claude` and/or `pi`). Cron
has none of your shell's PATH — see "Installing the schedule" below.

The wiki lives in `wiki/`, a **separate git repo**. If it is missing, every
mutating command fails with `wiki_missing`; clone it back beside `config/`
(or start an empty one from `wiki-template/`, see its README).

## The config, block by block

Only the parts you are likely to touch. Where a value differs from the
shipped example, it is the choice one long-running deployment settled on.

| block | key | what it decides |
|---|---|---|
| `elasticsearch` | `url`, `username`, `password` | the log source; password overridable by `DBWIKI_ES_PASSWORD` |
| `sources` | index patterns, `db_fields`, `patterns_file` | which indices are read and how events are classified |
| `compactor` | `rate_anomaly_*`, `silence_*`, `max_*` | how sensitive deltas are, how much verbatim text a digest may keep |
| `agents` | `adapter` (`codex`), `mode` (`structured`), `escalated_report` (`agentic`) | who does the LLM work, and how — see the table below |
| `agents.<adapter>` | `cheap`, `strong` | the two model tiers for that adapter |
| `agents` | `timeout_seconds` (1800), `feedback_retries` (1) | per-call bound; one in-run second chance |
| `agents` | `history_days` (90) | how far back the ingest prompt's database-history block reaches; `0` leaves the block out |
| `report` | `push` (true), `link_base` | whether commits are pushed; where the daily HTML links resolve |
| `portal` | `bind` (`127.0.0.1:8765`), `operator_email`, `lock_wait_s` (3.0) | where the incident workbench listens, who it commits as, how long a commit waits for the wiki lock |
| `health` | `stale_hours` (26), `stage_stale_hours`, `min_context` | when `dbwiki health` starts complaining |
| `alerts` | `enabled` (true), `sink` (`file` → `.state/alerts.jsonl`) | failure-only alerting |
| `langfuse` | `enabled` (true), `host`, `environment` | per-stage trace export **including prompt content** |
| `research` | `mode` (`structured`), `adapter` (`pi`), `limit` (3), `estate` | how error causes are looked up |
| `analyst` | `enabled` (absent → false) | ADR-0001 two-machine split |
| `redact` | `terms` | extra identifiers to anonymize for ADR-0002 offload |

**The switch people get wrong** is that there are four of them, and they are
independent:

- `agents.adapter` — the adapter for **agentic** stages only.
- `agents.mode: structured` — ingest and routine reports go through the
  `agents.pi` block and **ignore `agents.adapter` entirely**.
- `agents.escalated_report` — consulted only when mode is structured *and* the
  window is notable.
- `research.mode` — its own switch, unrelated to `agents.mode`.

`docs/scheduling.md` has the resulting per-stage routing table.

## Commands

```sh
# compaction (no LLM, no lock, free)
uv run dbwiki compact --db cdb1 --date 2026-08-29     # one daily digest
uv run dbwiki compact --all --date 2026-08-29         # every database
uv run dbwiki backfill --from 2025-08-24 --to 2026-07-11
uv run dbwiki dbs                                     # discover databases

# the loop
uv run dbwiki run                                     # one adaptive tick
uv run dbwiki run --consolidate                       # force the daily report
uv run dbwiki run --explain [--json]                  # why would each db wake? (read-only)

# individual stages
uv run dbwiki ingest --db cdb1 --date 2026-08-29 [--dry-run] [--adapter pi]
uv run dbwiki report --date 2026-08-29
uv run dbwiki lint                                    # deterministic lint + LLM health check
uv run dbwiki lint --deterministic-only [--json]      # mechanical rules only, no LLM
uv run dbwiki research [--limit 3] [--dry-run]
uv run dbwiki research --sources-only --review-sources
uv run dbwiki research --caveats [--limit 2] [--dry-run]  # practitioner notes from the approved sources (web)
uv run dbwiki review [--explain] [--force]            # weekly attention review (--explain: read-only)
uv run dbwiki review --deliver-only                   # resend the week's review, skipping what already went
uv run dbwiki render-daily [--day 2026-08-29]

# the incident workbench in a browser
uv run dbwiki portal serve [--bind HOST:PORT]         # http://127.0.0.1:8765

# incidents from the command line (preview by default; --commit publishes)
uv run dbwiki incident list [--all]                   # open incidents, or all of them
uv run dbwiki incident show <slug>                    # status, window, action log
uv run dbwiki incident record-action <slug> --intent ... --summary ...
uv run dbwiki incident monitor <slug> --signal error_absent:ORA-00600 \
    --until 2026-09-01T00:00:00Z --intent ... --summary ...
uv run dbwiki incident extend <slug> --until ... --intent ...
uv run dbwiki incident resolve <slug> --summary ...
uv run dbwiki incident reopen <slug> --reason ...

# operations
uv run dbwiki health [--json] [--alert]
uv run dbwiki retry [--dry-run] [--db cdb1]
uv run dbwiki stats [--json] [--task ingest]
uv run dbwiki awr --file awr.json [--emit]
uv run dbwiki redact errors/ORA-12154.md [--all|--candidates|--source <slug>]

# drill-down into the raw evidence
uv run dbwiki es search --source alert --db cdb1 \
    --from 2026-08-29T00:00:00Z --to 2026-08-30T00:00:00Z --query 'ORA-*'
uv run dbwiki es get --index .ds-logs-oracle.alert-default-2026.08.20-000008 --id <docid>
uv run dbwiki es trace --path /u01/app/oracle/diag/rdbms/cdb1/cdb1/trace/cdb1_ora_7903.trc

# ADR-0001 / ADR-0002 (both off here)
uv run dbwiki analyst --once
uv run dbwiki-researcher --once|--watch
```

Anything that mutates the wiki takes the single-flight lock and fails fast if
another command holds it. Add `--lock-wait SECONDS` (or set `DBWIKI_LOCK_WAIT`)
when you would rather queue than fail.

Every mutating `incident` subcommand previews by default: it prints the diff,
writes nothing, and publishes only when you repeat it with `--commit`, which
lands the whole change as one git commit. The preview never takes that lock,
so it is safe to run while a tick is working. Only `--commit` takes it. The
five mutating subcommands share `--actor EMAIL`, `--base SHA` (a full sha,
defaulting to the wiki's HEAD), `--at ISO_Z` (defaulting to now), `--commit`,
`--notes-file PATH`, and `--lock-wait SECONDS`. A slug is the incident page's
name, `2026-08-05-cdb1-something`. `incidents/<slug>.md` and a full path to
the page are accepted too.

The fields differ per subcommand:

- `record-action` needs `--intent` and `--summary`, and also takes
  `--ticket`, `--outcome`, `--rollback`, `--evidence`, and `--notes`.
- `monitor` needs `--signal`, `--until`, `--intent`, and `--summary`, and also
  takes `--ticket`, `--evidence`, and `--notes`.
- `extend` needs `--until` and `--intent`, and also takes `--notes`.
- `resolve` needs `--summary`, and also takes `--residual-risk`, `--ticket`,
  `--evidence`, `--notes`, and `--no-error-pages`.
- `reopen` needs `--reason`, and also takes `--evidence` and `--notes`.

`--evidence` repeats, once per page cited. `--signal` is `KIND:PAYLOAD` split
on the first colon, and the kinds are `error_absent:CODE`,
`event_present:REGEX`, `flow_resumed:SOURCE`, and `manual:TEXT`.

Exit codes: 0 published or nothing to do, 1 the wiki lock is busy or the
preview has blocking lint findings, 2 the command is not legal from the
incident's current status (or the text or the actor is unusable), 3 the base
moved, 4 lint blocked the commit, 5 the wiki tree is dirty. Every `--commit`
also writes one run-health event under command `incident`, naming the
incident, the subcommand, the actor, the base, the `at`, and the resulting
sha.

## The incident workbench

`dbwiki portal serve` puts a browser in front of the same incident lifecycle
the `dbwiki incident` subcommands drive. It is the normal way to record an
action and resolve an incident. The CLI stays the fallback, for when the
service is down and for anything you script.

Configure it with a `portal:` block in `config/dbwiki.yaml`. Every key has a
default, but `dbwiki health` reports the workbench only when the block is
there, so write it out even if you change nothing:

```yaml
portal:
  bind: 127.0.0.1:8765   # loopback only; serve refuses any other address
  operator_email: ""     # empty -> the wiki repo's git config user.email
  lock_wait_s: 3.0       # a commit's wait for the wiki lock before it answers 423
  push: true             # follows report.push unless you set it here
```

Those are the defaults, except `push`, which has no default of its own and
follows `report.push`.

The advisory tools are a separate top-level block, because they are not the
portal's: a run is a file under `.state/`, and its ceiling is read from the
same ledger `dbwiki health` writes. Every key has a default, and the whole
block may be absent:

```yaml
advisory:
  enabled: true          # false turns every tool off; the panel says so
  max_concurrent: 2      # model calls this process holds at once
  retain: 200            # run files kept, newest first
  window_h: 24           # the spend window every row inherits
  max_runs: 20           # runs per row per window
  max_cost_usd: null     # null is a count ceiling only
  timeout_s: 120         # per call; never agents.timeout_seconds
  tier: cheap            # which agents.pi tier a row resolves its model from
  tools:                 # the same keys again, per row, overriding the above
    explain-incident: {tier: strong}
    draft-note: {enabled: false}
```

An unknown tool id or an unknown tier is refused when the config is read, so
the server does not start rather than failing on an operator's first click.

Setting `max_cost_usd` means what it says: a window holding a run no adapter
priced refuses the next click with `cost_unknown` rather than treating that
run as free. Leave it `null` until you have read what a click actually costs
out of `.state/advisory_runs.jsonl`.

Where the weekly review goes is a third top-level block. It is separate
because it is not the portal's either: `dbwiki review` runs from cron and
writes its own audit. Every key has a default, and the whole block may be
absent:

```yaml
delivery:
  enabled: true            # false delivers nothing at all
  external_enabled: false  # the switch that lets anything leave the box
  smtp_host: ""            # the relay
  smtp_port: 25
  starttls: false
  from_address: ""         # the envelope sender
  subject_prefix: "[dbwiki] "
  timeout_s: 10.0          # per send
  portal_base: ""          # the base URL a body links back to
  recipients: []           # one row per person told
```

Those are the defaults. `external_enabled: false` means nothing leaves the
box: the review is published to `.state/review/`, the inbox screen reads it
there, and no mail is sent. Leave `portal_base` empty unless the portal is
reachable at an address other than loopback. A body with no `portal_base`
carries no link rather than one into the reader's own `127.0.0.1`.

A recipient row is `{id, address, channels, content_class, role}`. The `id`
is yours to pick and is what the audit keys on, so changing it re-sends.
`channels` is `[inbox]`, `[email]`, or both. The one content class is
`internal_full`, which sends the whole review to someone already trusted with
the wiki.

The config is refused when it is read, and the refusal names the key. An
unknown `content_class`, an unknown channel, an unknown role, a missing or
duplicate recipient `id`, and an empty `address` on a recipient that names
the email channel are all refusals. So is `external_enabled: true` with an
empty `smtp_host`, with an empty `from_address`, or with no recipient naming
the email channel, which would be delivery configured to reach nobody.

Sending is idempotent, keyed on the review and the recipient. A relay that is
down costs the review nothing: the failure is recorded in
`.state/review/log.jsonl` and `uv run dbwiki review --deliver-only` resends
exactly what is missing.

Start the workbench by hand:

```sh
uv run dbwiki portal serve
```

On the cron host it runs as a systemd user service instead.
`deploy/README.md` has the unit and the install steps.

Then open http://127.0.0.1:8765. Use that address rather than `localhost`.
The server binds `127.0.0.1`, and a browser that resolves `localhost` to
`::1` fails to connect before any check runs.

The Links tab is your own board of everything the pipeline runs on: Kibana
dashboards, Langfuse, the services on the box, the repositories. It is
`config/links.yaml`, a `title`, an `intro` and a list of `sections`, each
holding `links` of `{name, what, url, tag}` with `tag` one of `tailscale`,
`github` or `artifact`. Backticks in `intro`, `note` and `what` are code
spans. The file is optional; without it the tab says where to write one. A url
that is not http or https, a link with no name or no url, and an unknown tag
are all refused when the file is read, and the refusal names the link. The
board is read once, when the server starts, so restart the portal after
editing it.

The flow, start to finish:

1. The queue lists the open incidents, in the order the fleet report puts
   them in.
2. Open one. You get its status, its action log, the monitoring facts behind
   it and the page's git history, and only the verbs that are legal from its
   current status.
3. Pick a verb and fill in the form. The fields are the same ones the CLI
   flags set.
4. Read the diff. You get the unified diff the commit would make, any lint
   findings, any uncommitted wiki changes the commit does not own, and the
   `dbwiki incident ...` line that would publish the same thing from a
   terminal. Nothing has been written yet.
5. Publish. The whole change lands as one git commit, the same one `--commit`
   would make, and writes the same run-health event with `surface: portal`
   instead of `cli`.

The incident screen also carries a panel of advisory tools. Opening it asks
the server what each tool would read and what its window has left to spend;
that answer costs no model call, so you see the size of the material and the
state of the ceiling before you spend anything. Running one is the only
button on this workbench that costs money. An answer is a proposal: it is
labelled as one, it cites only paths the material actually carried, it says
what it cost, and nothing about it reaches a commit unless you edit it into a
field and publish it yourself.

The **Agents** tab answers what the loops themselves did. Pick 24, 48 or 168
hours. The table at the top rolls the window up by model: how many stages
each ran, how many passed validation and how many did not, the tokens and the
wall time, each sum beside the count of lines that reported it. Below it is
one lane per tick, drawn against the window's clock, with a bar per stage
coloured by the model that ran it and outlined in red where the stage failed
or was rolled back; hover a bar for the task, the database, the tokens and
the duration. Beside each lane are the incident pages that tick committed,
and each one is a link to the case. The join behind the lanes is the
`Run-ID:` trailer every machine commit carries, which is the same id
`.state/agent_runs.jsonl` keys every stage by; a commit without one is a hand
edit and belongs to no tick. The incident screen reads the same join the
other way, under **Which ticks wrote this page**: every tick that has written
the page in the last 30 days, its stages, and a link to the run. Both halves
say what bounds them — the ledger's caps above the table, the wiki revision
in the strip — because an absence can come from either.

The four refusals, and what each means for what you do next.

- **409 means the wiki moved under you.** Three causes. A tick committed
  between your preview and your publish, so nothing was written and you
  re-preview. Someone has an uncommitted edit in `wiki/`, which has to be
  committed or restored first. Or the incident page itself has never been
  committed, which you meet when you open it rather than when you publish.
  Commit the page with the CLI, or wait for the next tick to commit it.
- **422 means the workbench refused the content.** The lint blocked the diff,
  or the verb is not legal from the incident's current status, or a field is
  missing or malformed, or a resolve arrived with an empty residual-risk
  paragraph.
- **423 means another command holds the wiki lock.** Almost always the
  two-hourly tick. Nothing was written, nothing failed, and `dbwiki health`
  stays green. Wait a moment and click again.
- **429 means an advisory tool declined to spend.** Either every slot is
  busy, in which case the run holding one is on the panel with its own
  status, or the row has reached a ceiling for its window. Nothing was spent
  and no evidence was read either way.

## Recipes

**"Why did nothing happen for database X?"**

```sh
uv run dbwiki run --explain | grep -A5 '^X:'
```

Read-only, no side effects, and it uses exactly the code path the real tick
does. Typical answers: `content_unchanged` (nothing new since the last
ingest), `routine_only` (nothing worth waking a model for), or a `wake` line
listing the deltas and notable groups that would trigger it.

```
cdb1: wake [strong]
  - first_ever_code: {"value": "ORA-16649", "first_seen": "2026-08-29T08:04:00.816Z"}
  - notable_class: {"rule": "tns_error", "class": "error", "count": 32}
```

**"Some ingests failed — fix them."**

```sh
uv run dbwiki retry --dry-run        # what would be replayed, and why not
uv run dbwiki retry                  # replay the retryable ones
uv run dbwiki retry --db cdb1
```

Non-destructive: it only replays digests that still exist, whose content hash
still matches, and whose failure a rerun can fix. Fix the *cause* first (a
down model server, a missing adapter) — see [monitoring.md](monitoring.md).

**"Bootstrap history / re-derive digests."**

```sh
uv run dbwiki backfill --from 2025-08-24 --to 2026-07-11
```

Compaction only, and idempotent — it never calls a model. Ingesting that
history is a separate, chronological pass.

**"Re-do one day for one database."**

```sh
uv run dbwiki compact --db cdb1 --date 2026-08-29
uv run dbwiki ingest  --db cdb1 --date 2026-08-29
```

Re-ingesting a day rewrites that day's journal entry, incident update and
occurrence row rather than duplicating them.

**"Show me the prompt before spending a model on it."**

```sh
uv run dbwiki ingest --db cdb1 --date 2026-08-29 --dry-run
uv run dbwiki research --dry-run
```

**"What is this ORA code, and have we seen it before?"**

Look at `wiki/errors/<CODE>.md` — occurrences across databases, contexts, what
fixed it last time, and a `## Reference` section if research has run. If the
page has no `researched:` date, it is already a candidate for the next weekly
research run; force one page sooner with `uv run dbwiki research --limit 1`
after checking `--dry-run`.

**"Record a remediation and resolve an incident."**

```sh
uv run dbwiki incident show 2026-08-05-cdb1_stby-tns-12564-ora-16603
uv run dbwiki incident record-action 2026-08-05-cdb1_stby-tns-12564-ora-16603 \
    --intent 'restart the standby listener' \
    --summary 'restarted LISTENER on cdb1_stby; TNS-12564 stopped' \
    --evidence digests/cdb1_stby/2026-08-05.md
# read the diff, then publish the exact line it prints:
uv run dbwiki incident record-action 2026-08-05-cdb1_stby-tns-12564-ora-16603 \
    ... --base <sha> --at <timestamp> --commit
uv run dbwiki incident resolve 2026-08-05-cdb1_stby-tns-12564-ora-16603 \
    --summary 'listener restarted; no TNS-12564 for 48h' --commit
```

The preview prints the unified diff, any lint findings, any uncommitted wiki
changes it does not own, and last a `preview only. publish with:` line that
repeats the whole invocation with `--base` and `--at` filled in. Copy that
line to publish. Pinning `--at` is what makes a retry converge instead of
writing a second action record.

A `base moved` refusal (exit 3) means the wiki's HEAD moved between the
preview and the commit, usually because a cron tick committed. Nothing
reached the wiki. Re-preview against the new base and publish again.

`resolve` does two more things. It moves the incident's bullet from
`## Open incidents` to `## Resolved incidents` in `index.md`, and it writes a
`## Resolution history` row into every `errors/<CODE>.md` the incident links.
`--no-error-pages` suppresses the rows.

The incident workbench above is the normal way to do all of this. The CLI is
the fallback, for when the service is down and for anything you script.

**"Add a new external source."**

Source pages are **human-committed only** — an agent that creates one gets
rolled back. Write `wiki/sources/<slug>.md` with `status: approved`, `tier`,
`domains`, `fetchable`, `last_reviewed`, `review_after_days`, commit it in the
wiki repo, and research may cite those domains from then on.

**"Check what would leave the box before enabling research offload."**

```sh
uv run dbwiki redact errors/ORA-12154.md      # one page
uv run dbwiki redact --candidates             # everything the next run would send
```

Prints the exact anonymized request JSON (pseudonym mapping on stderr).
Nothing is sent.

**"Feed an AWR summary in."**

```sh
uv run dbwiki awr --file awr.json          # validate + print the digest
uv run dbwiki awr --file awr.json --emit   # write it beside the compactor's digests
```

Contract v1 is files-only (no live Oracle) and carries `sql_id`s, never SQL
text — see [awr-input.md](awr-input.md).

## Reading the wiki

Start at the top and drill down:

<!-- figure: drilldown | Every step down is closer to the evidence; the last one is the digest every claim above cites. -->

1. **`wiki/html/index.html`** — the daily DBA page. *Needs attention* first
   (notable databases and every still-open incident), then *Worth a look*
   (first-ever codes, new services/programs, rate anomalies, silence — always
   telemetry, never asserted as database state), then a collapsed *Routine*
   table. Links resolve on the wiki's git remote, where markdown renders.
2. **`wiki/reports/<day>.md`** — the fleet report: what happened, with
   historical context ("last seen 2026-03-14, fixed then by …").
3. **`wiki/databases/<db>.md`** and `databases/<db>/journal/<YYYY-MM>.md` — the
   profile and the day-by-day record.
4. **`wiki/incidents/<date>-<db>-<slug>.md`** — timeline, evidence, diagnosis,
   status (`open` / `monitoring` / `resolved`).
5. **`wiki/errors/<CODE>.md`** — the cross-database memory that makes the
   second incident cheaper than the first.
6. **`wiki/digests/<db>/<date>.md`** — the evidence any of the above cites.
   From there, `dbwiki es get` reaches the raw document.

`index.md` is the catalog, `log.md` the append-only audit line per agent stage.

## Rules for humans sharing the repo

- **Never hand-edit `digests/` or `html/`.** They are machine output; agents
  are rolled back for touching them and your edit will be overwritten.
- **Never leave the wiki tree dirty.** Any uncommitted change outside
  `digests/`/`html/` aborts the next agent run (`dirty_tree`) — commit or stash
  before the next tick. Editing wiki pages by hand is fine; leaving them
  uncommitted is not.
- **`sources/` pages enter by human commit only.**
- **Do not close an incident because the events stopped.** Recovery needs
  explicit evidence — a recovery event, resumed expected flow, or an operator
  note in `.state/recovery_annotation.txt`.
- Both repos push independently: the machinery repo is yours, the wiki repo is
  pushed by the pipeline when `report.push: true`.

## Changing models or adapters

1. Edit the tier under `agents.<adapter>` (or `agents.pi` for structured
   stages), or flip `agents.mode` / `agents.escalated_report` / `research.mode`.
2. `uv run dbwiki health` — the dependencies block verifies the adapter binary
   is on `PATH` and, when any route uses `pi`, that the local model server is
   up with the configured model at a usable context.
3. Try one stage by hand before trusting cron: `dbwiki ingest --db <db>
   --date <day>` or `dbwiki report --date <day>`.
4. `uv run dbwiki stats` after a few days: acceptance rate, rollback rate,
   median duration and cost per (task, adapter, tier). No verdict is offered
   until both tiers have ≥5 runs.
5. Record the measurement in `CHANGELOG.md` — that is where the reasoning
   behind the current settings lives.

The local structured stages need the model server **running** (`unsloth start`
— it does not come back by itself after a reboot) and serving a context large
enough for an escalated prompt; `health.min_context` floors that at 32k.

## Installing the schedule

The crontab is the authority; `docs/scheduling.md` is its documentation and
`config/schedule.json` its machine-readable mirror (used by the ELK emitter —
**update it whenever the crontab changes**, nothing enforces the sync).

```cron
PATH=/usr/local/bin:/usr/bin:/bin

15 */2 * * *  cd <repo> && uv run dbwiki run >> .state/cron.log 2>&1
30 23 * * *   cd <repo> && uv run dbwiki run --consolidate >> .state/cron.log 2>&1
0 8 * * 1     cd <repo> && uv run dbwiki lint >> .state/cron.log 2>&1
0 9 * * 1     cd <repo> && uv run dbwiki research >> .state/cron.log 2>&1
0 9 1 * *     cd <repo> && uv run dbwiki research --sources-only --review-sources >> .state/cron.log 2>&1
30 9 * * 1    cd <repo> && uv run dbwiki research --caveats >> .state/cron.log 2>&1
0 10 * * 1    cd <repo> && uv run dbwiki review >> .state/cron.log 2>&1
*/30 * * * *  cd <repo> && python3 elk/scripts/emit_derived.py >> .state/elk/emitter.log 2>&1
```

The explicit `PATH` is load-bearing: cron's default has neither `uv` nor
`codex` (linuxbrew), and agentic stages then fail *only* when unattended.
Add `--lock-wait 3600` to the mutating entries if you would rather a tick
waited out a slow agent than skipped itself. Successful runs are silent;
anything that appears in `.state/cron.log` is worth reading.
