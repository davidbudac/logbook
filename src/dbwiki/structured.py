"""Structured (non-roaming) ingest — the DESIGN.md 2a fallback for local
models: the model judges, deterministic code writes.

Contract v1 (ingest, plus routine reports; notable reports, lint and research
stay agentic): one text-only model call returns one JSON object; this module
validates it and then makes every file edit itself. The model never names a
path, never opens a file, never touches `log.md`, and never closes an
incident. Codes it did not see in the digest are dropped into `flags` instead
of being written, and its prose is flattened (wiki-link syntax, headings, dead
digest paths) before it lands on a page — so everything written here is
lint-clean by construction and the orchestrator's rails should never fire.
"""

import datetime as dt
import json
import re
from pathlib import Path

from .digest_md import render_md
from .harness import HarnessError, run_text
from .incidents import (INCIDENT_DIR, Incident, active, link_codes,
                        load_incidents, read_incident)
from .lifecycle import INDEX_OPEN
from .lint import DIGEST_REF_RE
from .pagetext import (day_block_replace, log_append, oneline,
                       section_append, section_line_replace,
                       set_frontmatter)

STRUCTURED_SCHEMA_VERSION = 1

MAX_SUMMARY = 200
MAX_TITLE = 120
MAX_LINE = 300          # one table cell / one bullet of model prose
MAX_DIGEST_CHARS = 12000
DEFAULT_HISTORY_DAYS = 90   # days of database history in the prompt; 0 = none

CODE_RE = re.compile(r"\A[A-Z]{2,6}-\d{1,6}\Z")
SLUG_RE = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")
ACTIONS = ("none", "open", "update")

_TABLE_HEAD = "| day | db | note | evidence |\n|---|---|---|---|"
_LINK_RE = re.compile(r"\[\[([^\]]*)\]\]")
_INDEX_SECTION = {"database": "Databases", "journal": "Journals",
                  "error-class": "Error classes", "incident": INDEX_OPEN,
                  "report": "Reports"}


class ProposalError(ValueError):
    """The model's answer is missing, malformed, or breaks the v1 contract.
    Every message names the offending field so the retry can quote it."""


def digest_codes(digest: dict) -> list[str]:
    """Every error code the digest actually carries (notable groups plus
    first-ever-code deltas) — the only codes an error page may be written for."""
    codes: list[str] = []
    for s in (digest.get("sources") or {}).values():
        for g in s.get("notable") or []:
            for c in g.get("codes") or []:
                if c and c not in codes:
                    codes.append(c)
    for d in digest.get("deltas") or []:
        v = d.get("value")
        if d.get("type") == "first_ever_code" and v and v not in codes:
            codes.append(v)
    return codes


def all_open_incidents(wiki_root: Path) -> list[Incident]:
    """Every unresolved incident, any database — the open-items context a fleet
    report needs. Position in this list is the number the report prompt shows
    and the report proposal refers back to, so prompt and writer must read it
    from the same unchanged tree."""
    return active(load_incidents(Path(wiki_root)))


#: Everything about the ingest prompt that does not depend on the digest: the
#: framing, the JSON contract and the rules. `{{db}}`, `{{day}}` and
#: `{{codes}}` are mustache placeholders so Langfuse can show this text as one
#: versioned prompt with variables (docs/langfuse.md, `promptreg`); the code
#: stays the source of truth and fills them with `str.replace` — never
#: `str.format`, the contract is full of braces.
INGEST_TEMPLATE = """You are ingesting one daily Oracle log digest for database {{db}} (day {{day}}) into an operations wiki.
Judge it. Do NOT write files and do NOT name file paths — a program applies your answer.

Reply with ONE JSON object and NOTHING else: no prose before or
after it, no markdown code fences, no explanation.

{
  "schema_version": 1,
  "summary": "one line, at most 200 characters; a longer one is cut at a word and flagged",
  "notable": true or false,
  "journal_entry": "2-6 sentences of plain prose; no headings, no bullets",
  "error_updates": [{"code": "ORA-00600", "note": "what this code did today"}],
  "incident": {"action": "none" or "open" or "update",
               "slug": "short-kebab-slug; REQUIRED when action is open, else null",
               "title": "one-line incident title; REQUIRED when action is open, else null",
               "body": "incident prose; REQUIRED when action is open or update, never null for those",
               "existing_page": "incidents/<file>.md; REQUIRED when action is update, else null"},
  "flags": ["anything a human should look at"]
}

With "action": "none", every other incident field is null. Do not choose
"open" or "update" unless you are also writing the fields they require.

Rules:
- "notable": true only when something happened that an on-call DBA would want to know about.
- "error_updates": only codes from this digest ({{codes}}). Any other code is dropped.
- "incident": "open" only when the evidence shows a real problem and no open incident below already covers it; "update" to add today's evidence to one of them (copy its path into "existing_page"); otherwise "none". Never propose closing or resolving an incident.
- "flags": short strings; use [] when there is nothing to flag.
- A "Trace evidence" block is detail about the groups that reference it, not a separate event; never count it as one.
- A "context" block under a group is the log lines immediately before/after that group's first occurrence: use it to judge cause and effect, never count its lines as events or cite them as separate occurrences.
- Never state a fact the digest does not show.
- The "Database history" block is what this database did on earlier days. Use it to say whether today's events are new, recurring, or follow a recorded change; never cite it as evidence for today and never restate it as a fact of this digest.
- An "after a change" delta is a timing fact computed from the log: say the error followed that change; say it was caused by it only when the messages themselves say so."""

NO_CODES = "none — leave the list empty"


def build_prompt(db: str, digest: dict, wiki_root: Path, *,
                 history_days: int = DEFAULT_HISTORY_DAYS) -> str:
    """The whole ingest task in one text prompt: the contract, the dedup
    context (open incidents, existing error pages, the digest's codes), what
    this database did over the last `history_days` days, and the rendered
    digest. The model gets no file access, so everything it may need to judge
    this digest has to be in here.

    The history block sits between the dedup context and the digest, and is
    absent entirely — not blank, not `(none)` — when the wiki remembers
    nothing, so a first-day database's prompt is exactly what it always was."""
    from .db_history import gather, render
    root = Path(wiki_root)
    day = digest["window"]["day"]
    rel_md = f"digests/{db}/{day}.md"
    codes = digest_codes(digest)
    inc_lines = "\n".join(f"- {i.path} — {i.title} (status: {i.status})"
                          for i in all_open_incidents(root) if i.db == db)
    err_lines = "\n".join(
        f"- {c}: errors/{c}.md "
        + ("already exists" if (root / f"errors/{c}.md").exists() else "does not exist yet")
        for c in codes)
    md = render_md(digest)
    if len(md) > MAX_DIGEST_CHARS:
        md = md[:MAX_DIGEST_CHARS] + "\n\n_(digest truncated for this prompt)_\n"
    static = (INGEST_TEMPLATE
              .replace("{{db}}", db)
              .replace("{{day}}", day)
              .replace("{{codes}}", ", ".join(codes) if codes else NO_CODES))
    history = render(gather(root, db, today=dt.date.fromisoformat(day),
                            codes=codes, days=history_days))
    return (
        f"{static}\n\n"
        f"Open incidents for {db}:\n{inc_lines or '- (none)'}\n\n"
        f"Error pages for this digest's codes:\n{err_lines or '- (no codes)'}\n\n"
        + (f"{history}\n\n" if history else "")
        + f"Digest ({rel_md}):\n---\n{md}---\n"
    )


_REPORT_CONTRACT = """Reply with ONE JSON object and NOTHING else: no prose
before or after it, no markdown code fences, no explanation.

{
  "schema_version": 1,
  "summary": "one line, at most 200 characters",
  "overview": "2-5 sentences of plain prose about this window; no headings, no bullets",
  "items": [{"db": "<database>", "status_line": "one line: what this database did"}],
  "open_incident_notes": [{"incident": 1, "note": "one line: what this window adds"}],
  "flags": ["anything a human should look at"]
}"""

# escalated windows (see build_escalated_report_prompt): the routine contract
# above, plus one extra field a routine window never asks for.
_ESCALATED_REPORT_CONTRACT = """Reply with ONE JSON object and NOTHING else: no prose
before or after it, no markdown code fences, no explanation.

{
  "schema_version": 1,
  "summary": "one line, at most 200 characters",
  "overview": "2-5 sentences of plain prose about this window; no headings, no bullets",
  "items": [{"db": "<database>", "status_line": "one line: what this database did"}],
  "open_incident_notes": [{"incident": 1, "note": "one line: what this window adds"}],
  "notable_analysis": [{"db": "<database>", "analysis": "3-10 sentences of markdown prose"}],
  "flags": ["anything a human should look at"]
}"""

_HEALTH_WARNING = ("Collection health (telemetry, not database state — never "
                   "report a collection gap as a database outage, and never "
                   "treat absent events as recovery)")


def build_report_prompt(day: str, window: tuple[str, str],
                        ingested: list[dict], wiki_root: Path, *,
                        health: list[str] | None = None) -> str:
    """The whole fleet-report task in one text prompt. The model gets no file
    access, so the window's ingest results, the collection-health lines and the
    open incidents are all handed to it here; the page, its filename, its table
    and its links are the writer's business."""
    root = Path(wiki_root)
    item_lines = "\n".join(
        f"- {i['db']}: {'NOTABLE' if i.get('notable') else 'routine'} — "
        f"{i.get('summary', '')}"
        + (f" (trigger: {i['trigger']})" if i.get("trigger") else "")
        for i in ingested) or "- (none ingested)"
    inc_lines = "\n".join(f"{n}. {i.title} — db {i.db or 'unknown'} "
                          f"(status: {i.status})"
                          for n, i in enumerate(all_open_incidents(root), 1))
    dbs = [i["db"] for i in ingested]
    return (
        f"You are writing the fleet report for the Oracle log wiki, covering "
        f"{window[0]} -> {window[1]} (day {day}).\n"
        f"Judge the window. Do NOT write files and do NOT name file paths — a "
        f"program renders your answer into the report page.\n\n"
        f"{_REPORT_CONTRACT}\n\n"
        f"Rules:\n"
        f'- "items": exactly one entry per database listed below '
        f"({', '.join(dbs) if dbs else 'none — leave the list empty'}). "
        f"Any other database is dropped.\n"
        f'- "open_incident_notes": refer to an open incident by its number '
        f"below, never by name or path; note only what this window says about "
        f"it. Leave the list empty when this window says nothing about any of "
        f"them. Never propose closing or resolving an incident — events "
        f"stopping is not recovery.\n"
        f'- "overview": no database is in trouble unless the items below say '
        f"so; describe the window, do not speculate about causes.\n"
        f'- "flags": short strings; use [] when there is nothing to flag.\n'
        f"- Never state a fact this prompt does not show, and never turn a "
        f"collection gap into a database outage.\n\n"
        f"Databases ingested in this window:\n{item_lines}\n\n"
        + (f"{_HEALTH_WARNING}:\n" + "\n".join(health) + "\n\n"
           if health else "")
        + f"Open incidents (any database):\n{inc_lines or '- (none)'}\n"
    )


# Material stands in for the wiki reading the agentic path would have done; the
# caps keep it inside a local model's context.

# one notable db's digest deltas + notable groups + trace evidence excerpt
MAX_MATERIAL_DIGEST = 10000
MAX_MATERIAL_PRIOR_REPORT = 4000  # latest same-day prior report page, if any
MAX_MATERIAL_INCIDENT = 3000      # each open incident page, full text
MAX_MATERIAL_INCIDENTS = 3        # at most this many incident pages
MAX_MATERIAL_ERROR_PAGE = 2000    # each error-class page cited by a notable group
MAX_MATERIAL_ERROR_PAGES = 5      # at most this many error-class pages

_CODE_IN_TEXT_RE = re.compile(r"\b([A-Z]{2,6}-\d{1,6})\b")


def _cap(text: str, limit: int) -> str:
    """`text` trimmed to `limit` characters, with a `[truncated]` marker
    appended when it was cut — so the model never mistakes a chopped-off
    section for the whole page."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def _digest_material(text: str) -> str:
    """The `## Deltas`, `### Notable` and `### Trace evidence` blocks of a
    rendered digest `.md` — the only part of a notable db's digest the
    Material carries. Per-source event counts and `### Routine (counters)`
    tables are noise for judging one window against history, so both are
    dropped: any `##`-level heading (2 or 3 `#`) ends the block being
    collected."""
    lines = text.splitlines()
    keep: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if (lines[i].startswith("## Deltas")
                or lines[i].startswith("### Notable")
                or lines[i].startswith("### Trace evidence")):
            keep.append(lines[i])
            i += 1
            while i < n and not lines[i].startswith("##"):
                keep.append(lines[i])
                i += 1
            keep.append("")
            continue
        i += 1
    return "\n".join(keep).strip()


def _material_section(head: str, body: str) -> str:
    return f"### {head}\n\n{body}\n"


_ESCALATED_RULES = (
    "\nEscalated-window rules (in addition to the rules above):\n"
    '- "notable_analysis": exactly one entry per NOTABLE database listed '
    "above, and no other database.\n"
    "- Compare this window against the Material only: prior occurrences of "
    "the same codes/groups, open incidents, error-class history. State only "
    "facts the Material shows.\n"
    "- You may cite a wiki page in \"analysis\" only as a [[...]] wikilink "
    "that appears verbatim in the Material below; never invent one, and "
    "never link a page the Material does not show.\n"
    "- Never declare recovery or a root cause the Material does not show — "
    "events stopping is not recovery.\n"
)


def build_escalated_report_prompt(day: str, window: tuple[str, str],
                                  ingested: list[dict], wiki_root: Path, *,
                                  health: list[str] | None = None) -> str:
    """`build_report_prompt` plus a deterministically assembled "Material"
    section: for each notable db, its digest's deltas/notable-groups excerpt;
    the latest prior report page from the same day, if any; every open
    incident page, in full; and the error-class page for every code that
    shows up in one of those digest excerpts. Everything is capped (see the
    MAX_MATERIAL_* constants) and every wikilink in it is one the writer will
    accept back unflattened (see apply_report/_prose's `keep_resolved_links`)
    — anything the model cites that is not verbatim here gets flattened."""
    root = Path(wiki_root)
    base = build_report_prompt(day, window, ingested, root, health=health)
    base = base.replace(_REPORT_CONTRACT, _ESCALATED_REPORT_CONTRACT, 1)

    sections: list[str] = []
    codes_seen: list[str] = []
    for i in ingested:
        if not i.get("notable"):
            continue
        db = i["db"]
        p = root / f"digests/{db}/{day}.md"
        if not p.exists():
            continue
        raw = _digest_material(p.read_text())
        if not raw:
            continue
        for c in _CODE_IN_TEXT_RE.findall(raw):
            if c not in codes_seen:
                codes_seen.append(c)
        sections.append(_material_section(
            f"Notable ingest — {db} (digests/{db}/{day}.md)",
            _cap(raw, MAX_MATERIAL_DIGEST)))

    prior_reports = sorted(root.glob(f"reports/{day}*.md"))
    if prior_reports:
        prior = prior_reports[-1]
        rel = prior.relative_to(root).as_posix()
        sections.append(_material_section(
            f"Prior report this day ([[{rel[:-3]}]])",
            _cap(prior.read_text(), MAX_MATERIAL_PRIOR_REPORT)))

    for inc in all_open_incidents(root)[:MAX_MATERIAL_INCIDENTS]:
        sections.append(_material_section(
            f"Open incident: [[{inc.path[:-3]}]] — {inc.title} "
            f"({inc.db or 'db unknown'}, status: {inc.status})",
            _cap((root / inc.path).read_text(), MAX_MATERIAL_INCIDENT)))

    err_added = 0
    for code in codes_seen:
        if err_added >= MAX_MATERIAL_ERROR_PAGES:
            break
        p = root / f"errors/{code}.md"
        if not p.exists():
            continue
        sections.append(_material_section(
            f"Error history: [[errors/{code}]]",
            _cap(p.read_text(), MAX_MATERIAL_ERROR_PAGE)))
        err_added += 1

    material = (
        "\nMaterial (assembled deterministically; you may cite a page only "
        "as a [[...]] wikilink that appears verbatim below):\n\n"
        + ("\n".join(sections) if sections
           else "_(nothing to show — no notable digest, prior report, open "
                "incident or error-class page was found)_\n"))
    return base + _ESCALATED_RULES + "\n" + material


def generate(prompt: str, cfg, *, escalate: bool = False,
             telemetry: dict | None = None) -> str:
    """One-shot text completion through the pi adapter (no tools, no file
    access): the whole answer is stdout. Model/provider/timeout come from the
    same `agents.pi` config the agentic path uses."""
    agents = getattr(cfg, "agents", {}) or {}
    pi = agents.get("pi", {}) or {}
    return run_text(prompt, pi.get("strong" if escalate else "cheap"),
                    agents.get("timeout_seconds", 900),
                    provider=pi.get("provider"),
                    cwd=getattr(cfg, "wiki_repo", None), telemetry=telemetry)


_RETRY = ("\n\nYour previous answer was rejected: {err}\n"
          "Answer again with ONE valid JSON object and nothing else.")


def _propose(prompt: str, cfg, parse, *, escalate: bool = False,
             telemetry: dict | None = None) -> dict:
    """Ask, validate, and retry exactly once with the validation error
    appended. A second bad answer is a HarnessError, so the orchestrator rolls
    back and records it like any other harness failure.

    The attempt count lands in `telemetry` (which `orchestrate.telemetry_fields`
    reads) so a stage that always needs its retry — twice the tokens, twice the
    latency — is visible in `dbwiki stats` instead of looking like one call."""
    if telemetry is not None:
        telemetry["attempts"] = 1
    try:
        return parse(generate(prompt, cfg, escalate=escalate,
                              telemetry=telemetry))
    except ProposalError as first:
        if telemetry is not None:
            telemetry["attempts"] = 2
        try:
            return parse(generate(prompt + _RETRY.format(err=first), cfg,
                                  escalate=escalate, telemetry=telemetry))
        except ProposalError as second:
            raise HarnessError(f"invalid structured proposal: {second} "
                               f"(first attempt: {first})") from second


def propose(prompt: str, cfg, *, escalate: bool = False,
            telemetry: dict | None = None) -> dict:
    """One validated ingest proposal, retried once. See _propose."""
    return _propose(prompt, cfg, parse_proposal, escalate=escalate,
                    telemetry=telemetry)


def propose_report(prompt: str, cfg, *, escalate: bool = False,
                   notable_dbs: set[str] | None = None,
                   telemetry: dict | None = None) -> dict:
    """One validated report proposal, retried once. See _propose.

    `notable_dbs` turns on the escalated contract (the extra
    `notable_analysis` field, validated against exactly this set of
    databases — an unknown db is rejected like any other malformed field, so
    it costs the retry, not a silent drop). Absent (the routine path), this
    is the unchanged routine contract."""
    parse = (parse_report_proposal if notable_dbs is None
            else lambda text: parse_escalated_report_proposal(text, notable_dbs))
    return _propose(prompt, cfg, parse, escalate=escalate, telemetry=telemetry)


def _first_json_object(text: str) -> str:
    """The first balanced `{...}` in the text — tolerates code fences and any
    prose the model wrapped around it."""
    s = text or ""
    start = s.find("{")
    if start < 0:
        raise ProposalError("response: contains no JSON object")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    raise ProposalError("response: JSON object is not closed")


def _str(obj: dict, key: str, label: str | None = None, *,
         max_len: int | None = None) -> str:
    label = label or key
    v = obj.get(key)
    if not isinstance(v, str):
        raise ProposalError(f"{label}: expected a string, got "
                            f"{type(v).__name__}")
    v = v.strip()
    if not v:
        raise ProposalError(f"{label}: must not be empty")
    if max_len and len(v) > max_len:
        raise ProposalError(f"{label}: longer than {max_len} characters")
    return v


def _summary(obj: dict, flags: list[str]) -> str:
    """A summary over MAX_SUMMARY is cut at the last word that fits and
    flagged, not rejected: the limit exists for the log line and the
    incident table, and a model that does not count characters (thinking
    off) would otherwise fail the run twice over a headline."""
    v = _str(obj, "summary")
    if len(v) <= MAX_SUMMARY:
        return v
    cut = v[:MAX_SUMMARY - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    flags.append(f"summary was {len(v)} characters, cut to {len(cut)}")
    return cut


def _error_updates(obj: dict) -> list[dict]:
    v = obj.get("error_updates") or []
    if not isinstance(v, list):
        raise ProposalError("error_updates: expected a list")
    out = []
    for i, item in enumerate(v):
        if not isinstance(item, dict):
            raise ProposalError(f"error_updates[{i}]: expected an object")
        code = _str(item, "code", f"error_updates[{i}].code")
        if not CODE_RE.match(code):
            raise ProposalError(f"error_updates[{i}].code: {code!r} is not an "
                                f"error code like ORA-00600")
        out.append({"code": code,
                    "note": _str(item, "note", f"error_updates[{i}].note")})
    return out


def _incident(obj: dict) -> dict:
    inc = obj.get("incident")
    if inc is None:
        inc = {"action": "none"}
    if not isinstance(inc, dict):
        raise ProposalError("incident: expected an object")
    action = inc.get("action")
    if action not in ACTIONS:
        raise ProposalError(f"incident.action: expected one of "
                            f"{'|'.join(ACTIONS)}, got {action!r}")
    for key in ("slug", "title", "body", "existing_page"):
        if inc.get(key) is not None and not isinstance(inc.get(key), str):
            raise ProposalError(f"incident.{key}: expected a string or null")
    out = {"action": action, "slug": None, "title": None, "body": None,
           "existing_page": None}
    if action == "open":
        slug = _str(inc, "slug", "incident.slug")
        if not SLUG_RE.match(slug):
            raise ProposalError("incident.slug: expected lowercase words "
                                "joined by '-'")
        out.update(slug=slug,
                   title=_str(inc, "title", "incident.title", max_len=MAX_TITLE),
                   body=_str(inc, "body", "incident.body"))
    elif action == "update":
        out.update(existing_page=_str(inc, "existing_page",
                                      "incident.existing_page"),
                   body=_str(inc, "body", "incident.body"))
    return out


def _flags(obj: dict) -> list[str]:
    v = obj.get("flags") or []
    if not isinstance(v, list):
        raise ProposalError("flags: expected a list")
    out = []
    for i, f in enumerate(v):
        if not isinstance(f, str):
            raise ProposalError(f"flags[{i}]: expected a string")
        if f.strip():
            out.append(f.strip())
    return out


def parse_proposal(text: str) -> dict:
    """Strictly validate the model's answer against the v1 contract, returning
    a normalized proposal (every optional field present, whitespace stripped)."""
    raw = _first_json_object(text)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProposalError(f"response: not valid JSON ({e})") from e
    if not isinstance(obj, dict):
        raise ProposalError("response: expected a JSON object")
    if obj.get("schema_version") != STRUCTURED_SCHEMA_VERSION:
        raise ProposalError(f"schema_version: expected "
                            f"{STRUCTURED_SCHEMA_VERSION}, got "
                            f"{obj.get('schema_version')!r}")
    if not isinstance(obj.get("notable"), bool):
        raise ProposalError("notable: expected true or false")
    flags = _flags(obj)
    return {
        "schema_version": STRUCTURED_SCHEMA_VERSION,
        "summary": _summary(obj, flags),
        "notable": obj["notable"],
        "journal_entry": _str(obj, "journal_entry"),
        "error_updates": _error_updates(obj),
        "incident": _incident(obj),
        "flags": flags,
    }


def _items(obj: dict) -> list[dict]:
    v = obj.get("items") or []
    if not isinstance(v, list):
        raise ProposalError("items: expected a list")
    out = []
    for i, item in enumerate(v):
        if not isinstance(item, dict):
            raise ProposalError(f"items[{i}]: expected an object")
        out.append({"db": _str(item, "db", f"items[{i}].db", max_len=64),
                    "status_line": _str(item, "status_line",
                                        f"items[{i}].status_line",
                                        max_len=MAX_LINE)})
    return out


def _incident_notes(obj: dict) -> list[dict]:
    v = obj.get("open_incident_notes") or []
    if not isinstance(v, list):
        raise ProposalError("open_incident_notes: expected a list")
    out = []
    for i, item in enumerate(v):
        if not isinstance(item, dict):
            raise ProposalError(f"open_incident_notes[{i}]: expected an object")
        n = item.get("incident")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ProposalError(f"open_incident_notes[{i}].incident: expected "
                                f"the number of a listed open incident, got "
                                f"{n!r}")
        out.append({"incident": n,
                    "note": _str(item, "note",
                                 f"open_incident_notes[{i}].note",
                                 max_len=MAX_LINE)})
    return out


def _report_obj(text: str) -> dict:
    """Decode + schema-check the model's answer, shared by the routine and
    escalated report parsers so both reject a bad envelope the same way."""
    raw = _first_json_object(text)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProposalError(f"response: not valid JSON ({e})") from e
    if not isinstance(obj, dict):
        raise ProposalError("response: expected a JSON object")
    if obj.get("schema_version") != STRUCTURED_SCHEMA_VERSION:
        raise ProposalError(f"schema_version: expected "
                            f"{STRUCTURED_SCHEMA_VERSION}, got "
                            f"{obj.get('schema_version')!r}")
    return obj


def _report_fields(obj: dict) -> dict:
    flags = _flags(obj)
    return {
        "schema_version": STRUCTURED_SCHEMA_VERSION,
        "summary": _summary(obj, flags),
        "overview": _str(obj, "overview"),
        "items": _items(obj),
        "open_incident_notes": _incident_notes(obj),
        "flags": flags,
    }


def _notable_analysis(obj: dict, notable_dbs: set[str]) -> list[dict]:
    """`notable_analysis`, present only on the escalated contract: one
    `{db, analysis}` per entry, `db` restricted to exactly the window's
    notable databases (an unknown or duplicate db is a parse-time rejection —
    like an unknown error code in ingest, but here caught before the writer
    ever sees it, since the writer trusts this list is already clean)."""
    v = obj.get("notable_analysis") or []
    if not isinstance(v, list):
        raise ProposalError("notable_analysis: expected a list")
    out, seen = [], set()
    for i, item in enumerate(v):
        if not isinstance(item, dict):
            raise ProposalError(f"notable_analysis[{i}]: expected an object")
        db = _str(item, "db", f"notable_analysis[{i}].db", max_len=64)
        if db not in notable_dbs:
            raise ProposalError(f"notable_analysis[{i}].db: {db!r} is not a "
                                f"notable database in this window")
        if db in seen:
            raise ProposalError(f"notable_analysis[{i}].db: duplicate entry "
                                f"for {db!r}")
        seen.add(db)
        out.append({"db": db,
                   "analysis": _str(item, "analysis",
                                    f"notable_analysis[{i}].analysis")})
    missing = notable_dbs - seen
    if missing:
        raise ProposalError(f"notable_analysis: missing entry for notable "
                            f"database(s) {', '.join(sorted(missing))}")
    return out


def parse_report_proposal(text: str) -> dict:
    """Strictly validate a report answer against the v1 contract. Same shape of
    rules as parse_proposal: known keys only, every string bounded, every
    reference resolved later by the writer rather than trusted here."""
    return _report_fields(_report_obj(text))


def parse_escalated_report_proposal(text: str, notable_dbs: set[str]) -> dict:
    """`parse_report_proposal` plus the escalated-only `notable_analysis`
    field. Routine report parsing (this function's caller never touches it)
    is unaffected by this function's existence."""
    obj = _report_obj(text)
    out = _report_fields(obj)
    out["notable_analysis"] = _notable_analysis(obj, notable_dbs)
    return out


def _prose(text: str, root: Path, flags: list[str], *,
          keep_resolved_links: bool = False) -> str:
    """Model prose made safe for a wiki page: wiki-link syntax flattened,
    heading markers stripped, and digest paths that do not exist replaced — a
    hallucinated link must never fail the lint that guards the commit.

    `keep_resolved_links` (only set by the escalated-report writer): a
    `[[target]]` whose target resolves to an existing wiki page is kept as a
    live wikilink instead of being flattened to its label — this is how a
    notable-item analysis is allowed to cite the Material's own pages. Every
    other caller leaves this False, so their output is unchanged."""
    def _link(m: re.Match) -> str:
        inner = m.group(1)
        label = inner.split("|")[-1].split("#")[0]
        if keep_resolved_links:
            target = inner.split("|")[0].split("#")[0].strip()
            if target and (root / f"{target}.md").exists():
                return f"[[{target}]]"
        return label
    out = _LINK_RE.sub(_link, text)
    out = re.sub(r"(?m)^\s*#{1,6}\s*", "", out)
    for ref in sorted(set(DIGEST_REF_RE.findall(out))):
        if not (root / ref).exists():
            out = out.replace(ref, "(unknown digest)")
            flags.append(f"dropped a reference to a digest that does not "
                         f"exist: {ref}")
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _journal_page(db: str, month: str) -> str:
    return f"---\ntype: journal\ndb: {db}\n---\n\n# {db} — journal {month}\n"


def _db_page(db: str, now_iso: str) -> str:
    return (f"---\ntype: database\ndb: {db}\nupdated: {now_iso}\n---\n\n"
            f"# {db}\n\nProfile stub created by structured ingest; facts are "
            f"filled in as digests are ingested.\n")


def _error_page(code: str, now_iso: str) -> str:
    return (f"---\ntype: error-class\nupdated: {now_iso}\n---\n\n# {code}\n\n"
            f"## Occurrences\n\n{_TABLE_HEAD}\n")


def _incident_page(db: str, title: str, body: str, now_iso: str, day: str,
                   rel_digest_md: str) -> str:
    return (f"---\ntype: incident\nstatus: open\ndb: {db}\nopened: {now_iso}\n"
            f"---\n\n# {title}\n\n{body}\n\n## Evidence\n\n"
            f"- {day}: {rel_digest_md}\n")


def _incident_rel(day: str, db: str, slug: str) -> str:
    """The page path an `open` proposal for `db` on `day` lands on.

    The day and the database are already in the path, and the model keeps
    putting them in the slug too, in either order (`cdb1-2026-08-11-x`,
    `2026-08-11-cdb1-x`). Cutting them keeps the three spellings of one
    incident on one path. Two passes because either prefix may come first,
    and a prefix is only cut when something is left to name the page."""
    prefixes = (f"{day}-", f"{db.replace('_', '-').lower()}-")
    for _ in prefixes:
        for prefix in prefixes:
            if slug.lower().startswith(prefix) and slug[len(prefix):]:
                slug = slug[len(prefix):]
    return f"{INCIDENT_DIR}/{day}-{db}-{slug}.md"


def _open_incident_on(root: Path, db: str, day: str) -> str | None:
    """The path of the incident already open for `db` on `day`, or None.

    Lexically first when several are open, so the wiki already carrying
    duplicates converges on one page instead of picking a different survivor
    each tick.

    Keyed on the path rather than `Incident.opened`, which holds the wall
    clock of the ingest that wrote the page, not the day the digest covers: a
    backfilled day is ingested days later, and two pages for one day can carry
    two different `opened` values. `incidents/<day>-` is what `_incident_rel`
    builds, so it is what identifies the page a second open would duplicate."""
    return min((i.path for i in all_open_incidents(root)
                if i.db == db and i.path.startswith(f"{INCIDENT_DIR}/{day}-")),
               default=None)


def apply_proposal(wiki_root, db: str, digest: dict, rel_digest_md: str,
                   proposal: dict, now_iso: str) -> dict:
    """Apply a validated proposal to the wiki and return the standard ingest
    result dict. Every path is derived here; the model named none of them.

    Idempotent by content: a page is only rewritten when the text actually
    changes, so re-applying the same proposal to the same tree touches
    nothing and `pages_touched` stays exact."""
    root = Path(wiki_root)
    day = digest["window"]["day"]
    month = day[:7]
    flags = list(proposal["flags"])
    touched: list[str] = []
    new_pages: list[tuple[str, str, str]] = []   # rel, type, label

    def read(rel: str) -> str | None:
        p = root / rel
        return p.read_text() if p.exists() else None

    def write(rel: str, text: str) -> bool:
        p = root / rel
        if p.exists() and p.read_text() == text:
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if rel not in touched:
            touched.append(rel)
        return True

    summary = oneline(_prose(proposal["summary"], root, flags))[:MAX_SUMMARY]
    entry = _prose(proposal["journal_entry"], root, flags)

    db_rel = f"databases/{db}.md"
    if read(db_rel) is None:
        write(db_rel, _db_page(db, now_iso))
        new_pages.append((db_rel, "database", db))

    j_rel = f"databases/{db}/journal/{month}.md"
    j_text = read(j_rel)
    if j_text is None:
        j_text = _journal_page(db, month)
        new_pages.append((j_rel, "journal", f"{db} journal {month}"))
    # same-day supersede: the digest is cumulative, so the latest ingest of a
    # day replaces that day's entry (and collapses any accumulated rewordings)
    block = f"\n## {day} — {summary}\n\n{entry}\n\nevidence: {rel_digest_md}\n"
    j_text = day_block_replace(
        j_text, re.compile(rf"\A## {re.escape(day)} — "), block)
    write(j_rel, j_text)

    codes = digest_codes(digest)
    for upd in proposal["error_updates"]:
        code = upd["code"]
        if code not in codes:
            flags.append(f"error code not present in the digest, dropped: {code}")
            continue
        rel = f"errors/{code}.md"
        text = read(rel)
        created = text is None
        text = text if text is not None else _error_page(code, now_iso)
        note = oneline(_prose(upd["note"], root, flags))
        row = f"| {day} | {db} | {note} | {rel_digest_md} |"
        after = section_line_replace(text, "Occurrences",
                                     f"| {day} | {db} | ", row,
                                     header=_TABLE_HEAD)
        if after != text or created:
            after = set_frontmatter(after, {"updated": now_iso})
        if write(rel, after) and created:
            new_pages.append((rel, "error-class", code))

    inc = proposal["incident"]
    opened: list[str] = []
    updated: list[str] = []
    def held(rel: str) -> bool:
        """Whether the wiki has a page at `rel`, for `link_codes`. The error
        pages this proposal writes are already on disk by here, so a code the
        digest raised for the first time is linked by the ingest that created
        its page rather than by the next one."""
        return read(rel) is not None

    if inc["action"] == "open":
        rel = _incident_rel(day, db, inc["slug"])
        title = oneline(_prose(inc["title"], root, flags))
        body = link_codes(_prose(inc["body"], root, flags), held)
        target = _open_incident_on(root, db, day) or rel
        if target != rel:
            flags.append(f"incident for {db} on {day} already open: {target} "
                         f"(evidence appended instead of opening a duplicate)")
        if read(target) is None:
            write(target, _incident_page(db, title, body, now_iso, day,
                                         rel_digest_md))
            opened.append(target)
            new_pages.append((target, "incident", title))
        else:
            if target == rel:
                flags.append(f"incident page already exists: {rel} (evidence "
                             f"appended instead of opening a duplicate)")
            text = section_line_replace(
                read(target), "Evidence", f"- {day}: {rel_digest_md}",
                f"- {day}: {rel_digest_md} — {oneline(body)}")
            if write(target, text):
                updated.append(target)
    elif inc["action"] == "update":
        rel = (inc["existing_page"] or "").strip().lstrip("./")
        current = (read(rel) if rel.startswith("incidents/") and ".." not in rel
                   else None)
        if current is None:
            flags.append(f"incident update target is not an existing incident "
                         f"page: {inc['existing_page']!r} (skipped)")
        elif not read_incident(current, rel).is_active:
            flags.append(f"incident update target is resolved: {rel} "
                         f"(skipped; open a new incident for a recurrence)")
        else:
            body = _prose(inc["body"], root, flags)
            block = link_codes(f"\n## Update {day}\n\n{body}\n\n"
                               f"evidence: {rel_digest_md}\n", held)
            text = day_block_replace(
                current, re.compile(rf"\A## Update {re.escape(day)}\s*\Z"),
                block)
            if write(rel, text):
                updated.append(rel)

    idx = read("index.md")
    if idx is None:
        idx = "---\ntype: index\n---\n\n# Logbook\n"
    for rel, kind, label in new_pages:
        target = rel[:-3] if rel.endswith(".md") else rel
        if f"[[{target}]]" in idx:
            continue
        idx = section_append(idx, _INDEX_SECTION[kind],
                             f"- [[{target}]] — {label}")
    write("index.md", idx)

    write("log.md", log_append(read("log.md"),
                               f"[{now_iso}] ingest — {db} {day}: {summary}"))

    return {"task": "ingest", "db": db, "notable": proposal["notable"],
            "summary": summary, "pages_touched": touched,
            "incidents_opened": opened, "incidents_updated": updated,
            "flags": flags, "mode": "structured"}


_REPORT_TABLE_HEAD = ("| db | state | headline | evidence |\n"
                      "|---|---|---|---|")


def _clock(iso: str) -> str:
    """`2026-07-27T20:19:00Z` -> `20:19Z`; anything else passes through."""
    m = re.search(r"T(\d{2}:\d{2})", iso or "")
    return f"{m.group(1)}Z" if m else (iso or "?")


def _evidence(root: Path, db: str, day: str) -> str:
    """Only pages that exist may be cited: the report is linted like any other
    page, and a link to a journal that was never written blocks the commit."""
    out = []
    journal = f"databases/{db}/journal/{day[:7]}"
    if (root / f"{journal}.md").exists():
        out.append(f"[[{journal}]]")
    digest = f"digests/{db}/{day}.md"
    if (root / digest).exists():
        out.append(digest)
    return ", ".join(out) or "(no page yet)"


def _notable_items_block(root: Path, ingested: list[dict], proposal: dict,
                         flags: list[str]) -> str | None:
    """The `## Notable items` section for an escalated report: one `###
    <db>` subsection per `notable_analysis` entry, in the same order as
    `ingested`. `None` (nothing rendered) when the proposal carries no such
    field — the routine contract never sets it, so a routine proposal leaves
    the page byte-identical to before this feature existed."""
    entries = {e["db"]: e["analysis"] for e in proposal.get("notable_analysis") or []}
    if not entries:
        return None
    parts = ["## Notable items\n"]
    for i in ingested:
        db = i["db"]
        if db not in entries:
            continue
        analysis = _prose(entries[db], root, flags, keep_resolved_links=True)
        parts.append(f"### {db}\n\n{analysis}\n")
    return "\n".join(parts)


def apply_report(wiki_root, day: str, window: tuple[str, str],
                 ingested: list[dict], proposal: dict, now_iso: str, *,
                 suffix: str = "", health: list[str] | None = None) -> dict:
    """Render a validated report proposal into `reports/<day><suffix>.md` and
    return the standard report result dict. The model supplied prose only:
    the filename, the summary table, every link and the log line are decided
    here, from the same inputs the prompt was built from."""
    root = Path(wiki_root)
    flags = list(proposal["flags"])
    touched: list[str] = []

    def read(rel: str) -> str | None:
        p = root / rel
        return p.read_text() if p.exists() else None

    def write(rel: str, text: str) -> bool:
        p = root / rel
        if p.exists() and p.read_text() == text:
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if rel not in touched:
            touched.append(rel)
        return True

    summary = oneline(_prose(proposal["summary"], root, flags))[:MAX_SUMMARY]
    overview = _prose(proposal["overview"], root, flags)

    known = [i["db"] for i in ingested]
    lines: dict[str, str] = {}
    for item in proposal["items"]:
        db = item["db"]
        if db not in known:
            flags.append(f"database not ingested in this window, dropped: {db}")
        elif db in lines:
            flags.append(f"duplicate report item for {db}, kept the first")
        else:
            lines[db] = oneline(_prose(item["status_line"], root, flags))
    rows = []
    for i in ingested:
        db = i["db"]
        head = lines.get(db)
        if head is None:
            head = oneline(str(i.get("summary") or "")) or "(no summary)"
            flags.append(f"no status line for {db}; used the ingest summary")
        state = "NOTABLE" if i.get("notable") else "routine"
        rows.append(f"| {db} | {state} | {head} | {_evidence(root, db, day)} |")
    if not rows:
        rows.append("| (none) | — | no database was ingested in this window | — |")

    incidents = all_open_incidents(root)
    notes: dict[int, str] = {}
    for note in proposal["open_incident_notes"]:
        n = note["incident"]
        if not 1 <= n <= len(incidents):
            flags.append(f"note refers to open incident {n}, which was not "
                         f"listed (dropped)")
            continue
        notes.setdefault(n, oneline(_prose(note["note"], root, flags)))
    open_lines = [
        f"- [[{i.path[:-3]}]] — {i.title} ({i.db or 'db unknown'}, "
        f"status: {i.status})"
        + (f" — {notes[n]}" if n in notes else "")
        for n, i in enumerate(incidents, 1)
    ] or ["- (none open)"]

    fm = (f"---\ntype: report\nwindow_start: {window[0]}\n"
          f"window_end: {window[1]}\ngenerated: {now_iso}\n---\n")
    heading = (f"# Fleet report — {day} {_clock(window[0])} to "
               f"{_clock(window[1])}\n")
    body = [fm, heading, f"{overview}\n"]
    notable_block = _notable_items_block(root, ingested, proposal, flags)
    if notable_block:
        body.append(notable_block)
    body += ["## Summary\n", _REPORT_TABLE_HEAD,
            "\n".join(rows) + "\n"]
    if health:
        body += ["## Collection health (telemetry, not database state)\n",
                 "\n".join(ln if ln.lstrip().startswith("-") else f"- {ln}"
                           for ln in health) + "\n"]
    body += ["## Open items\n", "\n".join(open_lines) + "\n"]

    rel_report = f"reports/{day}{suffix}.md"
    write(rel_report, "\n".join(body))

    idx = read("index.md") or "---\ntype: index\n---\n\n# Logbook\n"
    write("index.md", section_append(
        idx, _INDEX_SECTION["report"],
        f"- [[{rel_report[:-3]}]] — fleet report, {day} "
        f"{_clock(window[0])}–{_clock(window[1])}: {summary}"))

    write("log.md", log_append(
        read("log.md"), f"[{now_iso}] report — {rel_report}: {summary}"))

    return {"task": "report", "day": day,
            "notable": any(bool(i.get("notable")) for i in ingested),
            "summary": summary, "pages_touched": touched,
            "incidents_opened": [], "incidents_updated": [],
            "flags": flags, "mode": "structured"}
