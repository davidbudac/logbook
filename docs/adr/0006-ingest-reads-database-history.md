# ADR-0006 — Ingest reads the database's own history, as context never evidence

Date: 2026-09-13
Status: accepted

## Context

The ingest stage judges one day and remembers nothing. `build_prompt` packs
the digest for that window, the database's *open* incidents, and whether each
code in the digest already has an error page. That is the whole prompt. The
agentic path is worse off: it names the digest and leaves the model to find
the rest, which on a local model means it does not find it. So every night the
same question gets answered from scratch. Is this ORA-16820 the first one, the
fourth this month, or the one somebody already wrote a fix for? The digest
cannot say, and the model has nothing else.

The wiki knows. It has held the answer for months, in four places the ingest
prompt never reads:

- `databases/<db>/journal/<month>.md`, one headline per day the pipeline had
  something to say about,
- incidents, including the resolved ones, and every operator act recorded
  against them as a `## Action` block: record-action, resolve, reopen,
- `past_fixes`, which already derives what was tried for a code and whether it
  held,
- administrative changes. The pattern library classifies `ALTER SYSTEM SET`,
  `ALTER DATABASE MOUNT|OPEN`, tablespace and datafile DDL, redo config and
  instance startup/shutdown as class `lifecycle` (`patterns/alert.yaml`), so
  the compactor has been recognising every parameter change and every bounce
  all along. Those groups sit inside `### Notable` beside the errors, the
  day's journal entry rarely names them, and nothing reads them across days.

`advisory.py` has bounded readers over journal, actions, occurrences and
neighbours, but only the portal's click tools call them. The operator gets the
history on a web page; the model that writes the page does not.

## Decision

Two deterministic additions to the nightly pipeline. No new model call, and
every line comes off files the pipeline already writes.

**Changes become a first-class view of the digest.** `changes.changes_of` is
the one derivation in the codebase: every `lifecycle`-class group in every
source of a digest, ordered by timestamp. `compactor.compact` writes the
result as a `changes` list at the top of the `.json`, `digest_md` renders it
as a `## Changes` section and stops repeating those groups under `### Notable`,
and `changes.recent` reads the digests back off disk for the last N days. The
`.json` `notable` arrays keep their shape, so `digest_codes`, `daily_html` and
`content_hash` are untouched, and a digest written before this change still
yields its changes because `recent` derives them with the same function when
the key is absent.

**A history block in both ingest prompts.** `db_history.gather` builds one
frozen `DbHistory` for one database from the wiki tree: recent changes,
journal headlines for the days that had something to say, the incidents opened
or updated in the window that are no longer active with the day each was
resolved, the operator action records against them, and the `past_fixes` rows
for the codes in today's digest. The still-open incidents are deliberately
absent, because both prompts already print every one of them for this database
directly above the block. `db_history.render` turns it into one block placed
after the error-page list and before the digest, in `structured.build_prompt`
and in the agentic prompt in `Orchestrator.ingest`. The sections run past
fixes, operator actions, resolved incidents, changes, journal. That order is
the cap's order: `_cap` truncates the tail, so the lists that answer "has this
been fixed before" lead and the journal, the most restateable of the five and
the least actionable, goes last. Caps are module constants next to
`structured.MAX_MATERIAL_*`: 5000 characters for the block, 6 fixes, 8
actions, 5 incidents, 12 changes, 7 journal days, every line at `MAX_LINE`.
Changes are folded one line per day and rule before the cap applies: a single
shutdown-plus-startup fires eight to eleven lifecycle groups, and unfolded one
restart would spend the whole change budget. Each change line is the line the
rule's regex actually matched, recorded by the compactor as the group's
`headline`, because one ES alert document is many alert-log lines and the
first is rarely the one that names the change. A day whose digest records no
events at all, or exists, is not notable and has no changes, contributes no
line, so a quiet week costs nothing. The window is `agents.history_days`,
default 90; `0` leaves the block out.

**Context, never evidence.** This is the rail the rest of the decision hangs
on. The history block is prose about other days, assembled from pages the
model is about to edit, and the one thing it must never become is a source the
model cites. `INGEST_TEMPLATE` gains a rule saying so: use the block to judge
whether today's events are new, recurring, or follow a recorded change; never
cite it as evidence for today and never restate it as a fact of this digest.
The agentic prompt carries the same sentence. The rule is backed by shape as
well as instruction. Evidence in this pipeline is a digest path or an ES
document id. The block carries neither. It names incident pages, which are the
wiki's own prose about other days, so a model that ignores the rule still has
no evidence reference to copy out of the block.

### What we did not build

**A new extractor for change lines in the compactor.** The pattern table
already classifies these lines and is the thing operators edit when a rule is
wrong. A second matcher would drift from it, and the first day the two
disagreed about whether a line was a change, no one could say which was right.
`changes_of` reads the classification instead of repeating it.

**History for agentic mode only, on the theory that an agent can read the
files itself.** Live config runs `agents.mode: structured`. The structured
model gets one text prompt with no tools and no file access, which is the
whole point of the mode. The prompt is the only channel there is, so a
file-reading answer would ship the feature to the path that is switched off.

**A `readmodel.Snapshot` per database per tick, reusing the `advisory`
readers verbatim.** A snapshot is a whole-wiki build: `git ls-tree`, a batch
read, a `git log` pass. That is the wrong price for four short lists on every
database on every tick, and the `advisory` readers want a `Target.incident`
subject that ingest does not have, since at ingest time there may be no
incident at all. `db_history` calls the same parsers directly
(`readmodel.parse_journal`, `incidents.load_incidents`, `past_fixes`) and pays
for what it reads.

**A `history_note` field in the result schema.** The model would write prose
about the past into a structured result, and the journal entry and the
incident body already carry exactly that prose. Nothing to add, one more field
to validate.

## Consequences

- The ingest prompt grows by up to 5000 characters on a database with a busy
  history. That is bounded by construction rather than by the shape of the
  wiki, which matters because the cheap tier is a local model with a fixed
  context, and the digest must not be crowded out by last month.
- The failure mode to watch is a model that takes the block as today's news
  and writes July's incident into tonight's journal entry. The prompt rule is
  the first defence and the absence of any evidence reference is the second,
  but neither is a validator. Read the first week of journal entries on a
  database with real history before trusting it on all of them.
- Existing goldens with `lifecycle` groups change: those groups move out of
  `### Notable` into `## Changes` in the markdown twin. The JSON `notable`
  arrays, `content_hash` and `digest_codes` do not change, so nothing
  re-ingests on upgrade alone and every other consumer keeps its shape.
- `agents.history_days: 0` is the whole rollback, per config rather than per
  database. The digest `changes` view stays either way, because it is a view
  of data the compactor already produced.
- The block is only as good as the journal. A database whose journal entries
  are thin gets thin history, and the fix is upstream in what the ingest
  writes, not here.
