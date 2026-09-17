# ADR-0002 — Offload research to a lean, cloud-connected box that only ever sees anonymized data

Date: 2026-08-16
Status: accepted

## Context

ADR-0001 split the pipeline into an on-prem node and an "analyst node" joined
by the wiki git remote, and planned to move `research` (phase 2) to the
analyst. The analyst was designed as a full checkout of this repo plus a full
wiki clone: it reads digests, `errors/`, `incidents/`, `hosts/`, `databases/`.

Two things changed since:

- **The local model is now good enough for everything except research.**
  Ingest and reports (structured mode) run on LM Studio on the on-prem box
  and stay there. The only stage that still needs a cloud, web-capable agent
  is `research`: reading vendor docs about an error code and turning that into
  `## Reference` cause/action text, plus periodic review of source pages.
- **The threat model tightened for the cloud-connected process.** Whatever
  runs codex/claude must not be handed anything that identifies machines or
  databases: hostnames, IP addresses, DB/service names. The wiki repo itself
  may stay on GitHub (that is an accepted, separate risk — private repo,
  no third-party model reads it), but the *agent* on the cloud box must not
  get a wiki clone, because a wiki clone is exactly that identifying data.

Meanwhile the on-prem box should have **no web egress at all** except git to
GitHub. That kills `research.mode: structured` on-prem (it fetches
`docs.oracle.com` directly), so research has to leave the box in some form.

Research is a good fit for offloading because its *inputs* are almost entirely
non-identifying by nature — an error code is `ORA-12541` regardless of which
host raised it — and its *outputs* are external knowledge that, by the
provenance contract, never touches Occurrences or Resolution history.

## Decision

Add a third role, the **researcher**, and a second git repo, the **exchange**.
The researcher never clones the wiki and never holds ES or wiki credentials.
The on-prem node is the only writer of wiki content; it anonymizes what goes
out, validates what comes back, and writes the wiki page itself.

```
on-prem (LM Studio, ES creds, wiki + exchange creds, egress: GitHub only)
   │  select candidates → build request → REDACT → leak-check → enqueue
   ▼
exchange repo (GitHub)            requests/pending|claimed|failed, results/
   ▲                                       │
   │  results/<run_id>-<code>.json         │  claim (git is the lock)
   │                                       ▼
researcher (lean; codex/claude creds, exchange creds only, web egress)
   runs the agent with web access; returns JSON; sees only pseudonyms
```

### What is sensitive

Anything identifying a machine or a database: hostnames/FQDNs, IP addresses,
DB names, service names, SIDs, listener names, ES index names and doc ids
(they encode cluster/day/db), OS usernames, filesystem paths that embed any of
the above. Not sensitive: error codes, generic message templates, counts,
timestamps, product/version/feature facts ("Oracle 19c, Linux, Data Guard broker,
physical standby"). The last category is what makes research *effective* and
is deliberately kept.

### Redaction is deterministic and on-prem (`redact.py`)

No LLM in the loop; the redactor is code the on-prem box owns.

1. **Vocabulary** built per run from things the on-prem side knows exactly:
   `wiki/databases/*.md` and `wiki/hosts/*.md` slugs (and their frontmatter
   aliases), config `db_fields` values seen in the window, service names the
   compactor extracted, `client_host`, ES cluster/index names.
2. **Pattern sweep** for what the vocabulary might miss: IPv4/IPv6, FQDN-ish
   tokens, `(HOST=…)`, `(SERVICE_NAME=…)`, `SID=…`, `host=…`, ES index/doc id
   shapes, absolute paths under `/u0*`, `/opt`, `/home`.
3. **Consistent pseudonyms per request** — `DB_A`, `DB_B`, `HOST_A`, `IP_A`,
   `SVC_A` — so the agent can still tell "the primary" from "the standby".
   The mapping is written to `.state/redaction/<run_id>.json` **on-prem only**
   and used to de-map any pseudonym the result text carries back.
4. **Leak check, fail closed.** After redaction the outgoing payload is
   re-scanned against the vocabulary and patterns; a hit refuses the enqueue,
   records a `redaction-leak` health flag with the run_id, and leaves the page
   un-researched. Silence, not a partial payload.
5. **Drop, don't mask, what research does not need**: ES ids, digest paths,
   wiki links (`[[databases/…]]`, `[[hosts/…]]`, `[[incidents/…]]`) are
   removed rather than pseudonymized.

The redactor ships with fixture tests built from real-shaped wiki pages,
a digest and a registry (`tests/fixtures/redact/`), and a
`dbwiki redact <errors/CODE.md>` command (`--all`, `--candidates`,
`--source <slug>`) prints exactly what would leave the box, so an operator
can eyeball a request before turning offload on.

Findings from building it (2026-08-16, `redact.py` + `research_offload.py`):

- **Substring matching, not word boundaries.** `cdb1` inside
  `cdb1_tt00_2516.trc` or `dr1cdb1.dat` is still the database name; the
  safe direction is over-redaction, and `dbwiki redact` makes it visible.
- **The vocabulary must know the compactor's template spelling.** Message
  templates replace digit runs with `#` (`cdb#_stby`, `lab-dg#.localdomain`),
  which defeats both exact terms and the FQDN pattern; every term with digits
  gets its `#` variant, and the FQDN pattern accepts `#` inside labels.
- **Digit families.** The first live sweep leaked `ol9-19-dg3.localdomain1` —
  a sibling host no wiki page had ever named, with an off-list domain suffix.
  Every db/host/service term with digits now also contributes a family regex
  (`ol\d+-\d+-dg\d+`, `cdb\d+`), host hits swallow their domain tail, and a
  suffix followed by digits still counts as a suffix. This is the main reason
  the leak check alone is not enough: it shares the redactor's blind spots, so
  the vocabulary has to generalise, and the sweep over every current page
  (`dbwiki redact --all | grep`) is part of the acceptance, not just the tests.
- **Version numbers look like IPs.** `19.27.0.0`, `12.2.0.1` are kept
  (first octet 11/12/18/19/21/23 with a zero third component, or inside a
  five-part release string); `10.x` is always an address. `0.0.0.0`,
  loopback and `localhost` are kept.
- **Message samples are raw, not templates.** The request's
  `message_templates` are the raw first sample of each distinct template
  (redacted, deduped by template): templates lose the ORA-600 arguments and
  similar detail research actually needs, and the redactor handles the raw
  text anyway.
- **All `sources/` domains are exempt**, not just approved ones — external
  sites identify nothing about the estate, and deprecated citations must
  survive in `current_reference`.
- `SERVER=DEDICATED` is not a host; PDB names (`(PDBNAME=…)`) are;
  `CDB$ROOT`/`PDB$SEED` are not.
- **Short terms match whole words only.** An ES host called `es` turned every
  "estate"/"research" into a hit; terms under 4 characters get word
  boundaries, everything longer stays substring.
- OS user names come only from the `USER=` pattern (`oracle` as a vocabulary
  term would eat "Oracle Database"); paths under `/home`, `/u0*`, `/opt` are
  masked whole (`PATH_A`), the trailing punctuation kept.

### The exchange repo

A separate git repository containing only queue state. Same claim protocol,
same code as ADR-0001's `queue.py`, parametrised on a root path instead of
hard-wired to `wiki/queue/`.

```
requests/
├── pending/research-<CODE>-<run_id>.json
├── pending/source-review-<slug>-<run_id>.json
├── claimed/…
└── failed/…
results/<run_id>-<CODE>.json
```

**Research request** (`schema_version: 1`):

```
kind: research
run_id, code (ORA-12541), created_at, attempts
estate: {product: "Oracle Database", version: "19c", platform: "Linux x86-64",
         features: ["Data Guard broker", "physical standby"]}   # from config, static
synopsis:                            # all redacted
  message_templates: [...]           # normalized message text(s), pseudonymized
  occurrences: {count, first_seen, last_seen, spread_days}   # full timestamps are fine
  co_occurring_codes: ["ORA-16607", "ORA-16810"]
  context: [...]                     # Occurrence "Context" cells, redacted, ≤ N
  current_reference: "..."           # existing ## Reference, redacted (may be empty)
  resolution_state: "no confirmed fix" | "resolved: <redacted text>"
sources: [{slug, domains: [...], status, fetchable}]   # the approved registry
instructions: "..."                  # the research prompt text (static + code)
```

**Source-review request:** `{kind: source-review, slug, url, domains,
last_reviewed, review_after_days, previous_notes}` — no estate data at all.
**Source-review result:** `{slug, still_valid: bool, notes, checked: YYYY-MM-DD,
proposed_status?}`; on-prem updates the source page's `last_reviewed` (and
`status` only via the existing review rules), never the researcher.

**Result** (`results/<run_id>-<CODE>.json`):

```
schema_version, run_id, code, kind
cause: "...", action: "..."                       # markdown-free prose, ≤ 4000 chars each
references: [{source: slug, url, accessed: YYYY-MM-DD}]
related_codes: [...]
flags: [...]                                      # e.g. "propose source: <domain>"
telemetry: {...}                                  # the would-be agent_runs.jsonl record
```

Never markdown pages, never a diff. The researcher proposes; on-prem disposes.

### On-prem side: `research.mode: offload`

Per tick, where structured/agentic research runs today:

1. `select_error_candidates` / `sources_due_review` unchanged.
2. Build request, redact, leak-check, `enqueue_request(exchange, …)`;
   supersede any older pending request for the same `(kind, code|slug)`.
3. **Fold results**: for each `results/*.json`, de-map pseudonyms, then run the
   existing writer path — `apply_research` (frontmatter `researched:`,
   `## Reference` splice, `log.md` line) — followed by the existing checks
   unchanged: `_research_problems` (only `errors/` touched, only approved
   domains cited, no new source pages), deterministic lint, rollback on
   failure. Commit to the wiki with the request's `Run-ID:` trailer, delete
   the result file from the exchange, fold telemetry into
   `.state/agent_runs.jsonl` and the ledger. Results for unknown run_ids or
   codes without a page are moved to `failed/` with a reason.
4. Health: one queue block per exchange (pending age, stale claims, failed
   count) plus the `redaction-leak` counter.

`research.mode: structured` and `agentic` remain valid for setups with
egress; `offload` is the mode this ADR adds. With `offload` the on-prem box
never opens a socket except to GitHub.

### The researcher is a lean thing

Not "the same repo with a different config". A separate console script and
package (`dbwiki-researcher`, e.g. `src/dbwiki_researcher/`) that depends
only on: git, the adapter harness (`harness.py`'s run/retry/timeout logic —
extracted so it does not import ES/compactor/wiki code), the exchange queue
primitives, and a 10-line config (`exchange.path`, `adapter`, `model`,
`timeout`, `poll_seconds`). No ES client, no compactor, no LM Studio, no
wiki. Its whole loop:

```
dbwiki-researcher [--once | --watch]
  pull exchange → claim oldest request → build agent prompt from the request
  → run agent with web=True → parse .agent-result.json → validate against
  result schema (only approved domains, sizes) → write results/<…>.json,
  delete claimed request → commit, push (rebase-retry)
  failure: attempts+1; after 2 → failed/
```

The agent runs in a scratch directory containing nothing but the request; its
prompt states plainly that names are pseudonyms and must be echoed as-is.

### Relationship to ADR-0001

ADR-0001's analyst role stays as built for `report` requests but is not used
in this deployment (`analyst.enabled: false`; escalated reports are
structured on the local model). Its phase 2 (research on the analyst) is
**superseded** by this ADR: research goes to the researcher via the exchange,
not to an analyst with a wiki clone. The queue module is shared; the wiki's
`queue/` directory is untouched.

## Consequences

- The cloud-model process sees error codes, message templates, counts, dates,
  product facts and pseudonyms. It never sees a hostname, IP, DB name, ES id
  or wiki path. If it is compromised or logs prompts, the blast radius is
  "which Oracle errors this estate hits" — not which estate.
- Redaction is only as good as the vocabulary + patterns; hence fail-closed
  leak check, `--show`, and fixtures. Any new identifying field the compactor
  starts extracting must be added to the vocabulary builder (a test asserts
  every `normalize.py` identity field is covered).
- The on-prem box loses all web egress except GitHub; structured research is
  therefore off there. If GitHub itself becomes unacceptable later, the
  exchange (and wiki) can move to an on-LAN Gitea with a push mirror — the
  protocol does not care where the remote is.
- Research and source review become eventually consistent (hours, whenever
  the researcher polls). Reference sections were already the least
  time-sensitive wiki content.
- Research quality may drop slightly versus an agent that could read the
  full incident page; mitigated by sending the redacted Context cells,
  co-occurring codes and estate facts. Measure with `dbwiki stats` as before
  (telemetry rides back in the result).
- Two repos to keep credentials for on-prem; one on the researcher.
- Rollback: `research.mode: structured` (needs egress) or `agentic` on any
  box with keys; the exchange can be ignored.

## Rollout

Follow-ups are tracked in the issue tracker.

1. **Done 2026-08-16.** `redact.py` + fixtures + `dbwiki redact`; swept every
   current `errors/*.md` (58 pages) — zero residual identifiers after the
   digit-family fix above. `research_offload.py` builds the request; the
   request builder is what `dbwiki redact` prints, so step 2 changes only
   where the JSON goes, not what it contains.
2. **Done 2026-08-16.** `exchange.py` — a sibling of `queue.py` sharing its
   primitives (record files, `_commit` with `Run-ID:` trailer, loser-backs-off
   claim, rebase-retry push) rather than a parametrised `queue.py`: the
   analyst queue's claim ordering and file naming are `(kind, day)`-shaped and
   the exchange's are `(kind, code|slug)`-shaped, and forcing one module to do
   both was worse than 200 lines of sibling code. Request/result validation
   and the writers live in `research_offload.py`; `Orchestrator._offload_research`
   is the round (fold, then enqueue). Additions the ADR text did not spell
   out: candidates already pending/claimed are not re-enqueued (they come back
   every run until a result folds); results the on-prem side cannot apply are
   moved to `requests/failed/result-<name>.json` with the reason (never
   silently dropped); source-review results only ever set `last_reviewed` —
   `still_valid: false` / `proposed_status` are surfaced as flags and a log
   line for a human; `dbwiki research` in offload mode always runs (results
   fold even when nothing is due). Health: `exchange` block + `redaction-leak`
   counter from `.state/redaction_leaks.jsonl`.
3. **Code done 2026-08-16; live end-to-end pending.** `src/dbwiki_researcher/`
   + `dbwiki-researcher` script + `config/researcher.yaml.example`. No harness
   extraction was needed: `harness.py` was already stdlib-only; the one
   entanglement was `queue._git` reaching into `orchestrate`, now a tiny
   `gitutil.py`, so importing the researcher loads exactly `dbwiki.harness`,
   `dbwiki.exchange`, `dbwiki.queue` (a test pins that list). The researcher
   validates the agent's answer against the *request* (approved sources and
   their domains ride in the request), the on-prem side re-validates against
   the wiki. Found on the way: `codex exec --search` is rejected by
   codex-cli 0.147 — `--search` is a top-level flag and now precedes `exec`
   in `harness._codex_cmd`. Still to do for this step: create the exchange
   repo on GitHub, run the researcher on the workstation against the lab's
   real requests, and read the first folded `## Reference` sections.
4. Flip `research.mode: offload` on-prem, remove web egress there.
5. Deferred: redaction of `incidents/` summaries as extra context, moving
   remotes off GitHub.
