# ADR-0005 — Buy practitioner caveats with one web call that never sees the wiki

Date: 2026-09-09
Status: accepted

## Context

`research.mode: structured` is the only research path that has ever worked
unattended, and it can cite exactly one source. It builds
`https://docs.oracle.com/en/error-help/db/<code>/` from the error code,
fetches that page, and hands the text to a local model that turns it into
`{cause, action}`. One predictable URL per code is what makes the mode
deterministic, and it is also the mode's ceiling: nothing that needs a search
step can ever enter a `## Reference` section this way.

The wiki now holds 16 approved source pages under `wiki/sources/`. One of
them, `oracle-docs`, is reachable by building a URL. The other 15, Tanel
Poder, Jonathan Lewis, Oracle Base, MOS and the rest, are reachable only by
searching. They are also where the operational knowledge is. Oracle's own
text for ORA-1013 says the user pressed CTRL-C and to continue with the next
operation; the practitioner caveat a human added to that page says the DDL
may have committed server side anyway. Only one of those two paragraphs
changes what an operator does next.

The one path that could search was the agentic one, and it failed three runs
out of three on the local model: it browsed, it wrote prose it could not
cite, and the approved-domain rail refused the publication every time. The
lesson was not that the rails are wrong. It was that a model good enough to
summarise a page in hand is not good enough to run a bounded search and
answer in a schema.

ADR-0002 answered the same shape of problem by moving research off the box
entirely, through a redacted request queue. That machinery is built and is
the right answer for a stage that needs estate context: message templates,
occurrence counts, co-occurring codes. This stage needs none of that. It
needs an error code and two paragraphs the wiki has already published, both
of which are public Oracle text.

## Decision

Add one stage, `dbwiki research --caveats`, that runs the claude adapter with
web tools and nothing else, and give it no way to read the wiki.

**The call.** `harness.run_web_text` runs
`claude -p --output-format json --allowedTools WebSearch,WebFetch --model <model>`
with the process `cwd` set to a fresh temporary directory. The tool allowlist
grants no file access, and the working directory means that even a granted
Read or Glob would land in an empty scratch directory rather than in the wiki
checkout. Two independent barriers, because a tool allowlist is a flag that a
future edit could widen by accident and a working directory is not.

**What leaves the box.** The error code, the Oracle cause and action text
already published on the page, and the list of approved source slugs, tiers
and domains. That is the whole prompt.

**What never leaves.** Anything ADR-0002 calls sensitive: database names,
hostnames, IP addresses, service names, digest paths, incident text, the
`## Occurrences` table. None of it is read to build the prompt, so none of it
can be redacted wrongly. The stage reads the page through
`readmodel.parse_research`, which returns the cause and the action and
nothing else on the page.

**What comes back is data, not a diff.** The model answers with one JSON
object, `{"notes": [{"text", "source", "url"}]}`, at most three notes.
`research_caveats.parse_caveats_proposal` rejects a slug that is not one of
the approved sources it was given, a URL whose host is not that source's
domain or a subdomain of it, a note that is empty, longer than 600
characters, spans more than one line, or carries a markdown heading or a
wikilink. A second bad answer is a `HarnessError`, the same contract every
structured proposal keeps.

**Deterministic code writes every byte.** `research_caveats.apply_caveats`
sets `caveats_reviewed:` in the frontmatter and splices one
`**Practitioner note:**` paragraph per note into `## Reference`, each carrying
the `(source: sources/<slug>; url: <url>; accessed: <day>)` citation the lint
requires. It removes the note paragraphs it wrote before appending the new
ones, so a second pass replaces rather than accumulates, and it leaves every
other paragraph of the section byte identical: the `**Cause:**` and
`**Action:**` paragraphs the structured writer owns, and the hand written
`Practitioner caveat:` paragraphs a human added.

**The existing rails hold, unchanged.** Every page is its own transaction
with its own base and its own commit, the way `_structured_research` publishes.
`_validate` and `_research_problems` run per page, so the approved-domain
check that refused the agentic runs judges this stage's output too, from the
post write tree. A bad answer or a blocked lint restores its own page and
flags it; the rest of the run continues.

### Relationship to ADR-0002

This narrows ADR-0002's "no web egress from the on-prem box except git to
GitHub" for exactly one stage, and it is a narrowing rather than a reversal
because the reason for the rule does not apply here. ADR-0002 forbids egress
because the research request carried estate data that had to be redacted, and
a redactor is only as good as its vocabulary. This prompt has no estate data
to redact. It is three public strings and a list of blog domains, assembled
by code that never opens an incident page.

The offload path stays the answer for research that does need estate context.
If the operator later wants zero egress again, `caveats.enabled: false` is the
whole rollback and the wiki keeps every note already published.

## Consequences

- Paid tokens for the first time in this pipeline's routine schedule. The
  stage runs weekly at `limit: 2`, so the exposure is bounded at two searches
  a week, and the cost lands in `.state/agent_runs.jsonl` beside every other
  stage's for `dbwiki stats` to price.
- `caveats_reviewed:` sets the cadence at 180 days per page, matching the
  source pages' own review interval, so a page that has been reviewed is not
  bought again next Monday. Candidate selection also requires an active
  incident linking the code, so the spend follows what operators are actually
  hitting.
- The notes are model written prose about somebody else's blog post. They
  carry a citation to the page they came from and the card links it, so a
  reader who doubts a note can go read the source. That is the same standard
  the cause and action text is held to, and no higher.
- One more thing on the box needs claude credentials. The offload researcher
  was built precisely so this box would not, and this walks part of that back
  for one stage. The mitigation is the temporary working directory: the
  credentialed process cannot read the wiki even if the prompt were wrong.
- A source page that goes `fetchable: false` drops out of the prompt on the
  next run, and notes already citing it stay on the page until a human takes
  them off. That is the same behavior a deprecated source has for cause and
  action text today.
