# AWR summary input contract v1

How bounded Oracle AWR aggregates enter the pipeline as evidence.

v1 is **files only**: an operator (or an external, read-only extraction job)
writes one JSON file per (database, snapshot window); `dbwiki awr` validates it
and turns it into a digest. No live Oracle connection, no AWR HTML, no SQL
text, no thresholds.

```sh
uv run dbwiki awr --file /tmp/cdb1-4711-4712.json           # validate + print
uv run dbwiki awr --file /tmp/cdb1-4711-4712.json --emit    # also write it
```

`--emit` writes `<digest_dir>/<db>/<day>-awrT<hhmm>-<hhmm>.{json,md}` — the
compactor's layout with an `awr` suffix, so one day can hold the log digest and
several AWR windows side by side. It refuses when the wiki repo is absent:
digests are the wiki's raw sources, not free-standing files.

Implementation: `src/dbwiki/awr.py` (`load_awr_summary`, `awr_digest`,
`emit_awr_digest`). Every validation failure names the offending field.

## Why aggregates and not the report

The compactor exists because an LLM must never paginate raw evidence
(`DESIGN.md`). AWR is the same problem one level up: a full report is tens of
thousands of tokens of mostly-constant text. So the contract accepts only what
a digest would keep anyway — the snapshot window, DB time, top-N waits, top-N
SQL by elapsed time, and load-profile scalars — and caps each top-N list at
**20 rows**. Anything larger is rejected, not truncated: silently dropping rows
would make the digest's content hash lie about its input.

## The file

```json
{
  "awr_schema_version": 1,
  "db": "cdb1",
  "instance": "cdb1",
  "snapshot": {
    "begin_id": 4711,
    "end_id": 4712,
    "begin_time": "2026-07-12T10:00:00Z",
    "end_time": "2026-07-12T11:00:00Z"
  },
  "db_time_s": 1832.4,
  "elapsed_s": 3600.0,
  "load_profile": {
    "db_time_per_s": 0.51,
    "executes_per_s": 120.4,
    "redo_bytes_per_s": 184320.0,
    "user_calls_per_s": 44.2
  },
  "top_waits": [
    {"event": "db file sequential read", "time_s": 812.0,
     "pct_db_time": 44.3, "waits": 91234},
    {"event": "log file sync", "time_s": 190.5, "pct_db_time": 10.4}
  ],
  "top_sql": [
    {"sql_id": "9babjv8yq8ru3", "elapsed_s": 402.1, "execs": 1200},
    {"sql_id": "b7k2m9x4nq1pz", "elapsed_s": 118.0, "execs": 3}
  ],
  "collected": {"tool": "awr-extract.sh", "collected_at": "2026-07-12T11:05:00Z"}
}
```

| field | required | rule |
|---|---|---|
| `awr_schema_version` | yes | must be exactly `1` |
| `db` | yes | non-empty string; the database name as it appears in the logs |
| `instance` | no | non-empty string |
| `snapshot.begin_id` / `end_id` | yes | integers, `end_id > begin_id` |
| `snapshot.begin_time` / `end_time` | yes | ISO-8601 timestamps, `end_time > begin_time` |
| `db_time_s` | yes | number ≥ 0 |
| `elapsed_s` | yes | number > 0 |
| `load_profile` | no | object of numeric **scalars** only (no nesting, no strings) |
| `top_waits[]` | yes | ≤ 20 rows; `event` (string), `time_s` (≥ 0), `pct_db_time` (0–100), optional integer `waits` |
| `top_sql[]` | yes | ≤ 20 rows; `sql_id` (string), `elapsed_s` (≥ 0), `execs` (integer ≥ 0) |
| `collected` | no | object of strings (provenance); the loader adds `file` |

Unknown fields are **rejected**, at every level. That is what keeps SQL text
out: a `sql_text`, `text`, `sql_fulltext`, `statement` or `sql` key inside a
`top_sql` row fails with its own message, and any other unexpected key fails as
"not part of AWR contract v1". A wiki page may name a `sql_id`; it may not
quote the statement.

## The digest it produces

`awr_digest(summary)` returns the same shape the compactor emits — `db`,
`window {from, to, day}`, `generated_by: dbwiki-awr/<version>`,
`pattern_versions`, `sources`, `deltas`, `totals`, `notable` — so it renders
through `digest_md.render_md`, is hashable with `Compactor.content_hash`, and
lints like any other digest. Two additions: `awr_schema_version`, and an `awr`
block carrying the snapshot ids, instance and `collected` provenance.

Each aggregate row becomes one group of class `performance` under
`sources.awr.notable`:

| rule | template carries | count |
|---|---|---|
| `awr_window` | snap ids, `db_time_s`, `elapsed_s`, every load-profile scalar | rounded `db_time_s` |
| `awr_top_wait` | event name, `time_s`, `pct_db_time`, `waits` | rounded `time_s` |
| `awr_top_sql` | `sql_id`, `elapsed_s` | `execs` |

`Compactor.content_hash` hashes `deltas` plus each group's
`(rule, template, count)`, so every number that came out of AWR participates in
the hash — re-emitting the same window with different numbers changes it, and
re-emitting the identical file does not. `es_samples` is empty by design: the
provenance of an AWR row is the file, not an ES document.

**`notable` is always `false`.** v1 applies no thresholds to AWR numbers — the
same rule as `patterns/metrics.yaml`: "44% of DB time on `db file sequential
read`" is a fact, "that is bad" is interpretation. An AWR window is context for
correlation and never on its own a reason to wake an agent. `deltas` is always
empty for the same reason.

## Citing it in the wiki

Performance evidence may be cited on an incident page only as `derived`
evidence, with matching database identity and an overlapping window — see
`docs/provenance.md`, "Evidence kinds".

## Deferred (not part of v1)

- **Live extraction.** No `DBA_HIST_*` queries run from this repo. The
  candidate AWR SQL must first be validated **read-only against a test
  database** — proven read-only, proven to produce
  exactly the fields above, and proven bounded — before it is pointed at
  anything else. Until then the file is produced outside this repo.
- ASH, per-SQL plan-hash history, and `sql/<sql_id>.md` pages (`DESIGN.md`,
  "Extensions").
- Baselines and deltas across AWR windows (`awr_top_wait` regression vs the
  previous snapshot). Needs several validated windows on disk first.
