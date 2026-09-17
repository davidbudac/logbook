# Evidence provenance schema v1

`PROVENANCE_SCHEMA_VERSION = 1` (`src/dbwiki/lint.py`).

How a wiki claim points back at its evidence, and which parts of that contract
a machine checks. The deterministic checker is `dbwiki.lint.lint_wiki` — pure
Python, no LLM, no network. The agent lint keeps covering everything listed
under "Deferred to v2".

> **Port me.** This document lives here because the `wiki/` repository is not
> present in this checkout. When it is restored, this schema belongs in
> `wiki/AGENTS.md` (canonical for the agents), and this file should shrink to
> a pointer plus the rule table. Places where `wiki/AGENTS.md` is the real
> authority are marked **AGENTS.md** below; the shipped copy is
> `wiki-template/AGENTS.md`.

## Evidence kinds

Every claim on a wiki page is one of three kinds. The kind determines what
must anchor it.

| kind | meaning | anchor |
|---|---|---|
| `observed` | happened, and the log evidence says so | digest path + digest content hash (and the ES index/doc ids the digest carries) |
| `derived` | computed or inferred from observed evidence (rates, baselines, correlations) | the same digest anchor as the observations it derives from, plus the wording that marks it as derived ("suggests", "correlates with") |
| `external_reference` | knowledge from outside the estate: vendor docs, notes, articles | an approved source page id + the URL + the access date |

Hard boundary, unchanged from the research contract: an `external_reference`
never appears in an Occurrences or Resolution-history section, and is never
phrased as an observed fact. External research cannot alter observed
occurrence or resolution history.

### Anchoring an observed claim

The citation anchor for an observed claim is the digest that carries the
events, plus the digest's content hash:

    (digest: digests/cdb1/2026-07-10.md; hash: 3f9c1a2b4d5e6f70;
     window: 2026-07-10T00:00:00Z -> 2026-07-11T00:00:00Z; source: alert)

The hash is `Compactor.content_hash` (`compactor.py`) — the 16-hex digest of
the digest's semantic content, stable across ticks whose window moved but
whose events did not. ES index/document ids are already inside the digest
(`es_samples`); a page may repeat them but never instead of the digest path.

v1 checks only that a referenced `digests/<db>/<file>.{json,md}` path exists
(`digest-missing`). Verifying the hash against the digest file, and checking
that quoted ES samples exist in it, is v2 work.

**AGENTS.md**: the exact inline spelling of the digest anchor is a wiki
convention; the form above is this repo's proposal, not something the current
wiki pages evidence. Fix the spelling in `wiki/AGENTS.md`, then teach
`digest-missing` (and a future `provenance-missing` rule) that spelling.

### Anchoring an external reference

Evidenced by the current wiki and by `research.py`: external claims live under
`## Reference` and carry a source-page id, a URL, and an access date:

    Cause per the error reference. (source: sources/oracle-docs;
    url: https://docs.oracle.com/en/error/ORA-12543.html; accessed: 2026-07-25)

- `sources/<name>` must exist as `wiki/sources/<name>.md`;
- the URL's host must be, or be a subdomain of, a domain listed on a source
  page with `status: approved` (`research.approved_domains`);
- `accessed:` is an ISO `YYYY-MM-DD` date.

### Citing trace evidence

When the alert log names a trace file, the compactor puts a bounded excerpt of
that file in the digest's source section as `trace_evidence` (config
`trace_lookup`, `src/dbwiki/trace.py`). A quoted `ORA-` line from a trace file
is a log observation, exactly like a quoted alert line, so it is cited as
**observed** evidence — the AWR precedent above applies to inferred numbers,
not to text the database wrote.

Anchor it the same way as any observed claim: the digest path, its content
hash, and the entry's own `es_samples`, which name the trace documents rather
than the alert ones.

    The failing session was mid-fetch on a full scan when the block check
    failed. (digest: digests/cdb1/2026-07-11.md; hash: 8a1c07f2b93de145;
    es: .ds-logs-oracle.trace-default-2026.07.11-000001/tc-t2)

Two limits are worth knowing. The excerpt is a selection, not the file: only
the header lines and the `ORA-` lines with two lines of context survive the
budget, so a claim about what the trace does *not* contain is not anchored by
it. And the excerpt text is deliberately outside the digest's content hash
(only the path and document count are hashed), so a hash match proves which
trace files were found, not that the quoted lines were rendered by today's
excerpter.

### Citing performance evidence (v1 note)

Performance evidence — fleet-metric digests (config `metric_sources`,
`patterns/metrics.yaml`) and AWR summary digests (`docs/awr-input.md`) — is
numbers, not narrative. A metric sample says utilization was 99.96%; it does
not say the archiver stalled, and an AWR window that shows 44% of DB time on
one wait event does not say that wait caused the incident.

So on an incident page, performance evidence may appear **only as `derived`
evidence**, and only when both of these hold:

- **database identity matches** — the metric/AWR digest's `db` is the incident's
  database (not a host, not a service name that merely resembles it);
- **windows overlap** — the digest's `window` overlaps the incident's
  `date_range`.

It is anchored like any other derived claim: the digest path plus its content
hash, with wording that marks it as derived ("correlates with", "coincides
with"). Never "caused by".

    Session utilization stayed above 90% across the outage, which correlates
    with the connection failures below. (digest:
    digests/cdb1/2026-07-12.md; hash: 3f9c1a2b4d5e6f70;
    window: 2026-07-12T00:00:00Z -> 2026-07-13T00:00:00Z; source: metrics)

**No new deterministic rule in v1.** The linter already distinguishes evidence
kinds and already checks that a cited digest exists (`digest-missing`), which
is the whole mechanical part of the rule above: matching db identity and
overlapping windows cannot be checked without reading the digest's contents,
which is v2 work (see "Deferred to v2" — hash verification and sample
checking). Until then this is a contract for the ingest agent and the human
reviewer, not a rule id.

## v1 rule set

Stable, kebab-case rule ids. Only these are deterministic; the ids are part of
the contract (exceptions files and CI reference them).

| rule id | severity | what it catches |
|---|---|---|
| `frontmatter-malformed` | error | unterminated `---` block, invalid YAML, non-mapping frontmatter, or a `type:` outside the known set |
| `frontmatter-contradictory` | error | a date field (`updated`, `researched`, `last_reviewed`) earlier than `added`; `status: approved` with no `domains` |
| `wikilink-broken` | error | `[[target]]` that resolves to no page file |
| `page-orphaned` | warning | a page with no inbound wikilink that is also unreachable from `index.md` |
| `digest-missing` | error | a referenced `digests/<db>/<file>.{json,md}` that does not exist |
| `citation-malformed` | error | a `## Reference` line citing externally without the `(source: …; url: …; accessed: …)` shape, an unknown source page, an invalid access date, or a URL off the approved domains |
| `encoding-invalid` | error | a page whose bytes are not valid UTF-8, so nothing else about it can be read |

Severity semantics: `error` blocks — the CLI exits 1 and the orchestrator
rolls the agent's edits back before commit. `warning` is reported and never
blocks; orphanhood is a curation judgement (a page may legitimately wait for
its index entry) and would otherwise make every first-of-its-kind page fail.

Notes on the individual rules:

- **Link resolution.** `[[target]]` resolves against the wiki root first
  (`target`, then `target.md`), and otherwise by unique basename across the
  wiki — Obsidian's own behavior. An ambiguous basename (two pages with the
  same stem) does not resolve and is reported broken.
- **Orphan exemptions.** `index.md`, `log.md`, and everything under
  `digests/`, `reports/`, and `sources/` are exempt: they are entry points,
  machine output, or catalog pages reached by path rather than by link.
- **Digest links.** A `[[digests/…]]` wikilink is reported as `digest-missing`,
  not `wikilink-broken`, so a missing digest always has one rule id.
- **Undecodable pages.** A page that is not UTF-8 stays in the page
  inventory, so `[[links]]` to it still resolve and it is never also
  `wikilink-broken`; only its own text goes unread, so its links are not
  checked and it is not orphan-reported. One unreadable page costs that page
  a finding rather than costing the rest of the wiki its lint.
- **`researched` vs `updated`.** Deliberately *not* a contradiction. A page is
  routinely updated after it was researched — `research.py`'s own staleness
  model depends on `updated` moving forward independently. Only "earlier than
  `added`" is contradictory (the page cannot predate itself).
- **Page types.** Evidenced by this repo's code and tests: `error-class`,
  `source`, `incident`. The rest of the known set (`database`, `journal`,
  `host`, `service`, `concept`, `report`, `index`, `log`) is inferred from the
  wiki layout in `DESIGN.md`; `wiki-template/AGENTS.md` carries the authoritative list.

## Grandfathering

The wiki holds ~311 pages written before this contract. They are grandfathered
through a checked-in exceptions file at the wiki root, not by retrofitting
provenance to all of them:

    # <wiki_root>/.lint-exceptions
    # path<TAB>rule-id   suppresses one rule for one page
    # path               suppresses every rule for one page
    errors/ORA-00600.md	citation-malformed
    concepts/legacy-topology.md

Suppressed findings are still produced and counted (`suppressed: true` in JSON
output, `[suppressed]` in text output) — they simply do not fail the run. New
and newly-touched pages must lint clean: the orchestrator lints only the paths
an agent stage changed, so a grandfathered page that an agent edits is only
excused for the rules listed against it.

## Deferred to v2

The agent lint (`dbwiki lint`, the LLM stage) keeps covering these
semantically until they become deterministic. They need free-form prose
interpretation and would be brittle as mechanical rules:

1. **Undated standing claims** — "the standby lags by ~2s" with no window or
   as-of date.
2. **External claims in observed sections** — an `external_reference` placed
   in Occurrences/Resolution history rather than `## Reference`. (v1 only
   checks lines *inside* `## Reference`.)
3. **Stale or unapproved sources** — research citing a source that has since
   been deprecated or re-reviewed. `research.select_error_candidates` already
   detects this for scheduling; turning it into a lint finding is v2.
4. **Report coverage** — a fleet report that omits an open incident from its
   window.

**Incident state transitions** have left this list. Asking the agent lint
whether an `open -> resolved` was justified by the evidence was the wrong
question: an agent stage cannot make that transition at all any more.
`orchestrate._incident_problems` compares every changed `incidents/` page
against the base revision and refuses the publication when `status:`, the
`monitoring:` window, a `## Action` record or a `## Resolution history` row
differs, so the only writer of those fields is the human-issued lifecycle
service (`dbwiki incident`, ADR-0003). Whether a *human's* closure cites
recovery evidence rather than absent events is a review question, not a lint
rule.

Also v2, from the anchoring section above: verifying a cited digest hash
against the digest file, and checking that quoted ES sample ids appear in the
digest.

## Interfaces

```python
from dbwiki.lint import lint_wiki

findings = lint_wiki(wiki_root, only_paths=None)   # list[Finding]
findings[0].to_dict()
# {"file": "errors/ORA-1.md", "rule": "wikilink-broken", "severity": "error",
#  "message": "...", "hint": "...", "suppressed": false}
```

`only_paths` restricts *reporting* to those paths; the link graph is always
built over the whole wiki, so orphan and wikilink checks stay correct.

```sh
uv run dbwiki lint                       # deterministic lint, then the agent lint
uv run dbwiki lint --deterministic-only  # mechanical checks only, no LLM
uv run dbwiki lint --json                # findings as JSON
```

Exit codes: `0` clean, `1` unsuppressed `error` findings (the agent lint is
skipped — mechanical checks are the gate, and the agent must not paper over
them), `2` the wiki root does not exist.
