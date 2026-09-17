"""Practitioner caveats: the one stage that talks to the web from this box.

Structured research (research_structured.py) writes Oracle's own cause and
action onto an error page, and it can use no other source, because it fetches
one predictable docs.oracle.com URL per code. The wiki approves sixteen
sources; fifteen of them can only be reached by searching. This stage asks a
claude call with WebSearch and WebFetch and no file access at all
(harness.run_web_text) for up to three short practitioner notes per page, and
writes them itself.

What leaves the box: the error code, the Oracle cause and action text already
published on the page, and the list of approved source domains. Never a
database name, a hostname or an IP, which is what ADR-0002 protects. The
model reads no wiki file, so it cannot leak one, and it writes no wiki file,
so a bad answer costs a rejected proposal rather than a damaged page.

Shaped like research_structured.py: pure functions for selection, prompt and
validation, one deterministic writer, and structured.py's proposal machinery
(`ProposalError`, `_first_json_object`, `_str`, `_RETRY`) reused rather than
duplicated. The retry contract is the same: ask, validate, retry once with
the validation error appended, and a second bad answer is a HarnessError.
"""

import datetime as dt
import json
from pathlib import Path

from . import readmodel, research
from .harness import HarnessError, run_web_text
from .pagetext import frontmatter, log_append, oneline, set_frontmatter
from .research import _as_date, _open_incident_codes, _source_pages, _updated_key
from .structured import ProposalError, _first_json_object, _str, _RETRY

CAVEATS_REVIEW_DAYS = 180
MAX_NOTES = 3
MAX_NOTE_CHARS = 600
NOTE_PREFIX = "**Practitioner note:**"
REFERENCE_HEAD = "## Reference"


def select_caveat_candidates(wiki: Path, limit: int,
                             today: dt.date) -> list[Path]:
    """Error pages due a caveat pass: they already carry research (a caveat
    annotates Oracle's cause and action, it does not replace them), at least
    one active incident links them (a page nobody is currently hitting is not
    worth a paid web call), and their `caveats_reviewed:` frontmatter is
    absent or older than CAVEATS_REVIEW_DAYS.

    Ordered the way `research.select_error_candidates` orders its workload:
    the pages the most active incidents link come first, ties broken by the
    most recently updated. The first `limit` are returned, so the weekly cron
    line spends a bounded number of calls on the pages the operator is most
    likely to open next."""
    wiki = Path(wiki)
    counts = _open_incident_codes(wiki)
    cands = []
    for p in sorted((wiki / "errors").glob("*.md")):
        text = p.read_text()
        rel = f"errors/{p.name}"
        if readmodel.parse_research(p.stem, rel, text) is None:
            continue
        if counts.get(p.stem, 0) <= 0:
            continue
        fm = frontmatter(text)
        reviewed = _as_date(fm.get("caveats_reviewed"))
        if reviewed is not None and \
                reviewed + dt.timedelta(days=CAVEATS_REVIEW_DAYS) > today:
            continue
        cands.append((counts.get(p.stem, 0), _updated_key(fm.get("updated")), p))
    cands.sort(key=lambda c: c[1], reverse=True)
    cands.sort(key=lambda c: c[0], reverse=True)
    return [p for _, _, p in cands[:limit]]


def approved_fetchable_sources(wiki: Path) -> list[tuple[str, str, list[str]]]:
    """`(slug, tier, domains)` for every source page a caveat may be read
    through: `status: approved` and `fetchable: true`, path-sorted so the
    prompt's domain list is byte-stable from run to run. The tier travels
    with the domains because the prompt tells the model which sources carry
    more weight, and the domains are lowercased here so the parser's
    `research.domain_approved` check and the prompt agree on them."""
    out = []
    for slug, fm in _source_pages(Path(wiki)).items():
        if fm.get("status") != "approved" or not fm.get("fetchable"):
            continue
        domains = [str(d).strip().lower() for d in (fm.get("domains") or [])]
        out.append((slug, str(fm.get("tier", "")), domains))
    return out


def build_caveats_prompt(code: str, cause: str, action: str,
                         sources: list[tuple[str, str, list[str]]]) -> str:
    """The whole caveat task in one text prompt. Everything it carries is
    already public: the error code, the Oracle cause and action text the page
    publishes, and the approved domains. Nothing identifies the estate, so
    the prompt is safe to hand to a hosted model, which is the whole reason
    this stage may exist at all.

    The domain list is a hard instruction and also a rail: the parser rejects
    any note whose URL sits outside the source it names, so an answer that
    ignores the list costs the retry rather than reaching the page."""
    listed = "\n".join(
        f"- {slug} (tier {tier}): {', '.join(domains)}"
        for slug, tier, domains in sources) or "- (none)"
    return (
        f"You are researching Oracle error {code} for an operations wiki.\n\n"
        f"Oracle's own documentation already says this, and it is already "
        f"published on the page:\n"
        f"Cause: {cause}\n"
        f"Action: {action}\n\n"
        f"Search ONLY these approved sources and fetch pages ONLY from these "
        f"domains:\n{listed}\n\n"
        f"Find up to {MAX_NOTES} practitioner notes that add something "
        f"Oracle's text above does not say: a common real cause, a gotcha, a "
        f"diagnostic step, or a version-specific fix. Each note must come "
        f"from ONE page you actually fetched, and its url must be that "
        f"page's url on that source's domains.\n\n"
        f"Reply with ONE JSON object and NOTHING else: no prose before or "
        f"after it, no markdown code fences, no explanation.\n\n"
        f'{{\n  "notes": [\n    {{"text": "the practitioner note in plain '
        f'prose", "source": "<slug>", "url": "the page you fetched"}}\n  ]\n}}\n\n'
        f"Rules:\n"
        f"- An empty list is the right answer when nothing is worth adding. "
        f"Never pad the list.\n"
        f'- "source" must be one of the slugs listed above.\n'
        f'- Each "text" is at most {MAX_NOTE_CHARS} characters, one '
        f"paragraph, plain factual prose.\n"
        f'- No database names, no hostnames, no IP addresses in "text".\n'
        f'- No markdown headings, no wiki links and no URLs inside "text"; '
        f'the url belongs in "url".\n'
    )


def parse_caveats_proposal(text: str,
                           sources: list[tuple[str, str, list[str]]]) -> dict:
    """Strictly validate the model's answer against the notes contract, the
    same shape of rules as research_structured.parse_research_proposal.

    Every rule here is one a bad answer must not be able to write past: an
    unknown slug or a URL off that slug's domains would cite a source nobody
    approved, a heading or a wiki link in the prose would fool the lint's
    Reference-block reader, and a note carrying a newline would split the
    paragraph the writer builds. A missing `notes` key reads as an empty
    list, which is a legitimate answer. Anything else raises ProposalError
    naming the offending field, so the retry tells the model what to fix."""
    domains = {slug: set(doms) for slug, _tier, doms in sources}
    raw = _first_json_object(text)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProposalError(f"response: not valid JSON ({e})") from e
    if not isinstance(obj, dict):
        raise ProposalError("response: expected a JSON object")
    items = obj.get("notes")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ProposalError("notes: expected a list")
    if len(items) > MAX_NOTES:
        raise ProposalError(f"notes: at most {MAX_NOTES} notes, got {len(items)}")
    notes = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ProposalError(f"notes[{i}]: expected an object")
        slug = _str(item, "source", f"notes[{i}].source")
        if slug not in domains:
            raise ProposalError(f"notes[{i}].source: {slug!r} is not an "
                                f"approved fetchable source")
        url = _str(item, "url", f"notes[{i}].url")
        host = research.url_domain(url)
        if not research.domain_approved(host, domains[slug]):
            raise ProposalError(f"notes[{i}].url: {url!r} is not on "
                                f"sources/{slug}'s domains")
        note = _str(item, "text", f"notes[{i}].text", max_len=MAX_NOTE_CHARS)
        note = oneline(note)
        if "## " in note:
            raise ProposalError(f"notes[{i}].text: must not contain a "
                                f"markdown heading")
        if "[[" in note:
            raise ProposalError(f"notes[{i}].text: must not contain a wiki link")
        notes.append({"text": note, "source": slug, "url": url})
    return {"notes": notes}


def propose_caveats(prompt: str, cfg, *, telemetry: dict | None = None) -> dict:
    """One validated notes proposal, retried once with the validation error
    appended. Asks through `harness.run_web_text` rather than
    structured.generate: this is the one stage whose model needs the web, and
    structured.generate is a pi text call with no tools at all.

    The model comes from `research.caveats.model`, falling back to the claude
    adapter's cheap tier. The approved sources the answer is validated
    against are read from the wiki the config names, so the rail and the
    prompt cannot disagree about which slugs and domains exist. The attempt
    count lands in `telemetry` the way structured._propose records it, so a
    stage that always needs its retry — twice the paid tokens — is visible in
    `dbwiki stats`."""
    agents = getattr(cfg, "agents", {}) or {}
    rcfg = getattr(cfg, "research", {}) or {}
    model = (rcfg.get("caveats") or {}).get("model") \
        or (agents.get("claude", {}) or {}).get("cheap")
    timeout = agents.get("timeout_seconds", 900)
    approved = approved_fetchable_sources(Path(getattr(cfg, "wiki_repo", ".")))

    def ask(text: str) -> dict:
        return parse_caveats_proposal(
            run_web_text(text, model, timeout, telemetry=telemetry),
            approved)

    if telemetry is not None:
        telemetry["attempts"] = 1
    try:
        return ask(prompt)
    except ProposalError as first:
        if telemetry is not None:
            telemetry["attempts"] = 2
        try:
            return ask(prompt + _RETRY.format(err=first))
        except ProposalError as second:
            raise HarnessError(f"invalid structured proposal: {second} "
                               f"(first attempt: {first})") from second


def _note_paragraph(note: dict, today: dt.date) -> str:
    """One practitioner-note paragraph: the prose, then the citation on its
    own physical line, the way research_structured.apply_research writes its
    cause and action citation. `lint._reference_items` joins a paragraph's
    wrapped lines back into one item, so the citation is judged whole."""
    return (f"{NOTE_PREFIX} {note['text']}\n"
            f"(source: sources/{note['source']}; url: {note['url']}; "
            f"accessed: {today.isoformat()}).")


def _splice_notes(text: str, paragraphs: list[str]) -> str:
    """Rewrite the `## Reference` section so its practitioner notes are
    exactly `paragraphs`, leaving every other paragraph byte-identical.

    Not `pagetext.day_block_replace`, which rewrites the whole section: the
    `**Cause:**` and `**Action:**` paragraphs and any hand-written
    `Practitioner caveat:` a human added are the section's existing content
    and must survive this writer untouched, down to their line wrapping. So
    the section body is split on blank lines, every paragraph whose first
    line opens with the note prefix is dropped, and the new notes are
    appended after the last surviving paragraph. Sections after `## Reference`
    do not move.

    Twice: identical bytes, because the second pass drops exactly the notes
    the first pass wrote and appends the same ones again."""
    lines = text.rstrip("\n").splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip() == REFERENCE_HEAD), None)
    if start is None:
        if not paragraphs:
            return text.rstrip("\n") + "\n"
        return (text.rstrip("\n") + f"\n\n{REFERENCE_HEAD}\n\n"
                + "\n\n".join(paragraphs) + "\n")
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    kept: list[list[str]] = []
    current: list[str] = []
    for line in lines[start + 1:end]:
        if line.strip():
            current.append(line)
            continue
        if current:
            kept.append(current)
            current = []
    if current:
        kept.append(current)
    body = ["\n".join(para) for para in kept
            if not para[0].startswith(NOTE_PREFIX)] + paragraphs
    body_lines: list[str] = []
    for i, para in enumerate(body):
        if i:
            body_lines.append("")
        body_lines += para.split("\n")
    block = [lines[start]] + ([""] + body_lines if body_lines else [])
    out = lines[:start] + block + [""] + lines[end:]
    return "\n".join(out).rstrip("\n") + "\n"


def apply_caveats(wiki_root, rel_page: str, proposal: dict,
                  today: dt.date) -> None:
    """Apply one validated notes proposal to `rel_page`.

    Sets `caveats_reviewed:` in the frontmatter and rewrites the practitioner
    notes inside `## Reference` (see `_splice_notes`). An empty proposal is a
    real answer, not a failure: the page still gets its `caveats_reviewed:`
    date, so the next weekly run spends its calls on a page nobody has looked
    at yet rather than asking about this one again, and the log line records
    `0 note(s)` so the operator can see the call happened.

    Appends one log.md line, the contract every deterministic writer here
    keeps."""
    root = Path(wiki_root)
    page = root / rel_page
    notes = proposal.get("notes") or []
    text = set_frontmatter(page.read_text(),
                           {"caveats_reviewed": today.isoformat()})
    page.write_text(_splice_notes(
        text, [_note_paragraph(n, today) for n in notes]))
    log_path = root / "log.md"
    log_line = (f"[{today.isoformat()}] research (caveats) — {rel_page}: "
                f"{len(notes)} note(s)")
    log_path.write_text(log_append(
        log_path.read_text() if log_path.exists() else None, log_line))
