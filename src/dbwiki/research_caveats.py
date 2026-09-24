"""Practitioner caveats: the one stage that talks to the web from this box.

Structured research (research_structured.py) writes Oracle's own cause and
action onto an error page, and it can use no other source, because it fetches
one predictable docs.oracle.com URL per code. The wiki approves sixteen
sources; fifteen of them can only be reached by searching. This stage asks a
claude call with WebSearch and WebFetch and no file access at all
(harness.run_web_text) for up to three short practitioner notes per page, and
writes them itself.

What leaves the box: the error code, the cause and action text already
published on the page, and the list of approved source domains — the cause
and action redacted and the whole prompt leak-checked first
(`outbound_caveats_prompt`), because an offload fold-in may have de-mapped
host and database names into that text. Never a database name, a hostname or
an IP, which is what ADR-0002 protects. The model reads no wiki file, so it
cannot leak one, and it writes no wiki file, so a bad answer costs a
rejected proposal rather than a damaged page.

Shaped like research_structured.py: pure functions for selection, prompt and
validation, one deterministic writer, and structured.py's proposal machinery
(`ProposalError`, `_first_json_object`, `_str`, `_RETRY`) reused rather than
duplicated. The retry contract is the same: ask, validate, retry once with
the validation error appended, and a second bad answer is a HarnessError.
"""

import datetime as dt
import json
from pathlib import Path

from . import prompts, readmodel
from .exchange import citation_url_problem, prose_link_problem
from .harness import HarnessError, run_web_text
from .pagetext import frontmatter, log_append, oneline, set_frontmatter
from .redact import Redactor, build_vocabulary
from .research import _as_date, _open_incident_codes, _source_pages, _updated_key
from .structured import ProposalError, _first_json_object, _str, _RETRY

CAVEATS_REVIEW_DAYS = 180
MAX_NOTES = 3
MAX_NOTE_CHARS = 600
NOTE_PREFIX = "**Practitioner note:**"
REFERENCE_HEAD = "## Reference"

#: the whole instruction, a `str.format` template over the code, the page's
#: cause and action, the approved domains and the caps above
_CAVEATS_RULES = prompts.load("research-caveats")
_CAVEATS_CONTRACT = prompts.load("research-caveats-contract")


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
    """The whole caveat task in one text prompt: the error code, the cause
    and action text the page publishes, and the approved domains. Pure
    formatting — callers that send it go through `outbound_caveats_prompt`,
    which redacts `cause`/`action` and leak-checks the result first.

    The domain list is a hard instruction and also a rail: the parser rejects
    any note whose URL sits outside the source it names, so an answer that
    ignores the list costs the retry rather than reaching the page."""
    listed = "\n".join(
        f"- {slug} (tier {tier}): {', '.join(domains)}"
        for slug, tier, domains in sources) or "- (none)"
    return _CAVEATS_RULES.format(
        code=code, cause=cause, action=action, sources=listed,
        max_notes=MAX_NOTES, max_note_chars=MAX_NOTE_CHARS,
        contract=_CAVEATS_CONTRACT) + "\n"


def outbound_caveats_prompt(cfg, code: str, cause: str, action: str,
                            sources: list[tuple[str, str, list[str]]], *,
                            vocab=None) -> tuple[str, Redactor]:
    """The caveats prompt as it may leave the box: the page's cause and
    action redacted with the run's vocabulary, the whole prompt then run
    through the independent leak check (issue 07).

    ADR-0005 called this text public Oracle prose, and structured research
    writes exactly that, but an offload fold-in de-maps the researcher's
    pseudonyms back into `## Reference` — after which the section names
    hosts and databases. So this stage crosses the same boundary an offload
    request does. Returns `(prompt, redactor)`; the redactor de-maps any
    pseudonym the model echoes. Raises `RedactionLeak` when anything
    identifying survives: the caller skips the page and records why, it
    never sends a partial prompt."""
    if vocab is None:
        vocab = build_vocabulary(cfg, Path(cfg.wiki_repo), cfg.state_dir)
    red = Redactor(vocab, key=f"caveats-{code}")
    prompt = build_caveats_prompt(code, red.redact(cause), red.redact(action), sources)
    red.check(prompt)
    return prompt, red


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
        if problem := citation_url_problem(url, domains[slug]):
            raise ProposalError(f"notes[{i}].url: {url!r} is not on "
                                f"sources/{slug}'s domains ({problem})")
        note = _str(item, "text", f"notes[{i}].text", max_len=MAX_NOTE_CHARS)
        note = oneline(note)
        if "## " in note:
            raise ProposalError(f"notes[{i}].text: must not contain a "
                                f"markdown heading")
        if "[[" in note:
            raise ProposalError(f"notes[{i}].text: must not contain a wiki link")
        if problem := prose_link_problem(note):
            raise ProposalError(f"notes[{i}].text: {problem}; the url belongs "
                                f"in \"url\"")
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
    agents = cfg.agents
    rcfg = cfg.research
    model = (rcfg.get("caveats") or {}).get("model") \
        or (agents.get("claude", {}) or {}).get("cheap")
    timeout = agents.get("timeout_seconds", 900)
    approved = approved_fetchable_sources(Path(cfg.wiki_repo))

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
