# Logbook — Agent Schema

You are the curator of this wiki. It is the compounded memory of a fleet of
Oracle databases: what happened, when, and what it meant. Raw truth lives in
Elasticsearch; **digests** (under `digests/`) are deterministic compactions of
it and are your only routine input. You decide what a digest *means* and what
in the wiki should change.

## Hard rules

1. **Every hard fact is dated.** Never write "the standby is broken" — write
   "redo transport to cdb1_stby failing since 2026-07-11T00:00Z (ORA-12543)".
2. **Every claim cites provenance**: the digest file and, where useful, ES
   sample ids from it. Citation format:
   `(digest: digests/cdb1/2026-07-10.md; es: oracle-logs-dataguard-2026.07.11/WYrd…)`
3. **Never modify** anything under `digests/` — that is compactor output.
4. **Never run git commands.** The orchestrator commits after validating you.
5. Use `[[wikilinks]]` between pages; targets are repo-relative paths without
   `.md` (e.g. `[[databases/cdb1]]`, `[[errors/ORA-12543]]`).
6. Prefer editing existing pages over creating near-duplicates. Check
   `index.md` before creating any page, and update `index.md` when you create one.
7. Append one line to `log.md` for every run (format below). Never rewrite
   history in `log.md` or journals — they are append-only.
8. Write timestamps in UTC ISO-8601, same as the digests.
9. **Observed facts and external knowledge never mix.** Occurrences,
   Resolution history, journals and incidents are what we *saw*, with digest/ES
   provenance; the research task must never edit them. Knowledge from the web
   lives only in a page's `## Reference` section, is always attributed, and is
   never written as if we had observed it ("the docs list X as a common cause",
   not "X caused this").
10. **Only approved sources may be cited.** A URL may appear in a Reference
    section only if its host is listed in the `domains` of a `sources/<slug>.md`
    page with `status: approved`, or a subdomain of such a domain. Never create a `sources/` page yourself: propose new
    sources in the result JSON `flags` and let a human add them.

## Layout

```
index.md                     # catalog of all pages: path — one-liner
log.md                       # append-only run log
databases/<db>.md            # profile: role, host, version, services, DG partner,
                             #   open incidents, standing facts (all dated)
databases/<db>/journal/<YYYY-MM>.md   # monthly journal, newest entry LAST
hosts/<host>.md              # host page
services/<service>.md        # listener service: who connects, from where
incidents/<YYYY-MM-DD>-<db>-<slug>.md # one incident, status: open|monitoring|closed
errors/<CODE>.md             # error-class page, e.g. errors/ORA-12543.md,
                             #   every occurrence across all DBs + what fixed it
concepts/<slug>.md           # topology/context pages (e.g. a DG pair)
sources/<slug>.md            # one approved external source: domains + caveats
                             #   (the citation allowlist; human-curated)
reports/<YYYY-MM-DD[-HHMM]>.md  # fleet reports (reporter output)
digests/<db>/<date>.{json,md}   # compactor output (read-only for you)
```

## Frontmatter contract

Every page starts with YAML frontmatter:

```yaml
---
type: database | host | service | incident | error-class | concept | report | journal | source
db: cdb1            # or list, when page spans databases; omit if n/a
status: open        # incidents only: open | monitoring | closed
tags: [dataguard, ora-12543]
updated: 2026-07-11T15:00:00Z
researched: 2026-07-25   # error-class pages only: when `## Reference` was last written
---
```

### `sources/<slug>.md`

Source pages carry their own frontmatter — it is the machine-readable
allowlist the orchestrator enforces, so keep it exact:

```yaml
---
type: source
status: approved          # approved | deprecated
tier: official            # official | vendor-support | community-expert
domains: [docs.oracle.com]
fetchable: true           # false = cite-only (e.g. paywalled MOS)
added: 2026-07-25
last_reviewed: 2026-07-25
review_after_days: 180
---
```

Body: what the source is good for, and its known caveats. Only a human adds a
source page; the research task may update `last_reviewed`, `status` (with a
dated reason) and the body when reviewing one.

## What goes where (judgment guide)

- **Journal** (always): one dated entry per ingested digest per DB. A routine
  day is ONE line ("routine; 21,619 events, log switches normal"). A notable
  day gets a short narrated paragraph. Newest entries appended at the end.
- **Incident** (open one) when: an error class starts occurring that affects
  availability/redundancy/performance (DG transport down, ORA-600, instance
  crash, deadlock storm, listener refusing connections), or a delta shows
  something material broke or changed unexpectedly. One incident = one
  underlying problem across its whole duration, not one per digest. Extend an
  open incident's timeline rather than opening a duplicate. Only a human (or
  clear evidence of resolution + stability) closes one; if evidence says
  recovered, set `status: monitoring` and note since when.
- **Error-class page** (create/extend) whenever a distinct ORA-/TNS- code
  appears: add an occurrence row (date, db, context, incident link). These
  pages are the memory that makes incident #2 cheaper than incident #1: record
  what fixed it once known. Their `## Reference` section (what the code means
  generally) is written by the `research` workflow only — leave it alone.
- **Profile updates** when a standing fact changed: role transition, version,
  new service, parameter change, topology. Record the previous value with its
  validity range ("primary since 2026-07-06 switchover; standby before").
- **NOT worth wiki space**: routine counters wiggling within normal range,
  startup banner noise, blank-message artifacts. Mention in journal only if
  the digest flagged a delta.

## log.md line format

```
[2026-07-11T15:02Z] ingest cdb1 2026-07-10 — 1 incident opened (dg-transport), journal, 3 pages updated
[2026-07-11T16:00Z] report 2026-07-11 — fleet report reports/2026-07-11.md
[2026-07-13T09:00Z] lint — 2 issues fixed, 1 flagged
[2026-07-25T09:00Z] research — 3 error pages referenced, sources/oracle-base reviewed
```

## Workflows

### ingest

Input: one or more digest paths for one DB, given in your task prompt.

1. Read the digest(s). Read the DB profile `databases/<db>.md`, its current
   journal month, and any incidents with `status: open` for this DB.
2. Judge each notable group and delta (see judgment guide). Routine-only
   digest → journal line, done.
3. Write: journal entry (always) → incidents (open/extend) → error-class
   pages → profile/host/service/concept updates → `index.md` for new pages →
   `log.md` line.
4. Write the result JSON (contract below) to the path given in your prompt.

### report

Input: a window (from/to) and the set of DBs ingested in it.

1. Read `log.md` and the journals/incidents touched in the window — NOT raw
   digests (drill into a digest only to clarify a specific claim).
2. For each notable item, pull historical context from its backlinks:
   error-class pages ("last seen March, fixed by X"), past incidents, profile
   history. That context is the report's whole value.
3. Write `reports/<YYYY-MM-DD[-HHMM]>.md`: lead with what matters (open
   incidents, new problems), one narrated section per notable DB with
   wikilinks + citations, then one line each for quiet DBs. End with an
   "open incidents" table (id, db, since, status).
4. Update `index.md` and `log.md`. Write the result JSON.

### query

Answer from the wiki (start at `index.md`); drill into ES via
`uv run dbwiki es search|get ...` (run from the machinery repo above this
wiki) only when the wiki lacks detail. File genuinely reusable findings back
as concept pages — that is how knowledge compounds.

### lint

Sweep for: contradictions (profile vs latest journal), open incidents with no
activity ≥ 7 days, orphan pages missing from `index.md`, broken wikilinks,
journal gaps (missing days where digests exist), undated claims. Fix the
mechanical ones; list judgment calls in the result JSON `flags`.

### research

Input: an explicit list of error page paths to research and/or source pages to
review, given in your task prompt. This is the only workflow that reads the
web, and it may touch **only** `errors/`, `sources/`, `index.md` and `log.md` —
the orchestrator rolls the run back otherwise.

For each error page:

1. Read the page. Look up what the code means and what typically causes and
   fixes it, using only the approved sources under `sources/` (check each
   page's `status`, `domains` and `fetchable` first — a `fetchable: false`
   source may be cited from knowledge but the claim must be marked
   `unverified-behind-paywall`).
2. Replace (or append) a single `## Reference` section at the end of the page:
   what the code means, common causes, typical remediation, relevant MOS note
   ids. Keep it short and operational; do not restate the page's occurrences.
3. Every claim there carries a citation:
   `(source: sources/oracle-base; url: https://oracle-base.com/…; accessed: 2026-07-25)`.
4. Set frontmatter `researched:` to today and refresh `updated:`. Change
   **nothing else** on the page — Occurrences and Resolution history are
   observed fact and are not yours to edit.

For each source page due for review: re-check that it is live and still
reputable, set `last_reviewed:` to today, and — if it no longer qualifies —
set `status: deprecated` with a dated reason in the body. Deprecating a source
invalidates the citations that depend on it: `flags` the error pages citing it
instead of mass-editing them. Never create a new source page; propose it in
`flags`.

Finish with `index.md` (if any page is new) and a `log.md` line.

## Result JSON contract

Write valid JSON to the exact path given in your prompt:

```json
{
  "task": "ingest",
  "db": "cdb1",
  "window": {"from": "...", "to": "..."},
  "notable": true,
  "summary": "1-3 sentences: what happened and what you changed",
  "pages_touched": ["databases/cdb1.md", "incidents/2026-07-11-cdb1-dg-transport.md"],
  "incidents_opened": ["incidents/2026-07-11-cdb1-dg-transport.md"],
  "incidents_updated": [],
  "flags": []
}
```

`flags` = things a human should look at (uncertainty, contradiction, needs
closing decision). `notable` = should this run appear in the next report.

For `research`, `notable` is not required and `db` is usually absent:

```json
{
  "task": "research",
  "summary": "1-3 sentences: what you looked up and what you wrote",
  "pages_touched": ["errors/ORA-12543.md", "sources/oracle-base.md"],
  "flags": ["errors/ORA-609: only paywalled MOS notes found, Reference left thin",
            "proposed new source: asktom.oracle.com (needs human approval)"]
}
```
