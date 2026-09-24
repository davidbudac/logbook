"""Structured research: the DESIGN.md 2a pattern (structured.py) applied to
the researcher agent instead of ingest/report. No web-capable adapter is ever
invoked in this mode — deterministic code picks the one approved+fetchable
source, builds the URL, fetches the page, and writes every byte of the wiki
page; the model's only job is turning the already-fetched text into a
`{"cause", "action"}` JSON. research.py stays pure selection, as its
docstring promises, so all of the fetch/prompt/proposal/writer mechanics for
this mode live here instead.

Reuses structured.py's proposal machinery (`_propose`, `_first_json_object`,
`_str`, `_prose`) and pagetext.py's frontmatter/section-splice writer
primitives (`set_frontmatter`, `day_block_replace`, `log_append`) rather than
duplicating them — the v1 contract's retry-once-then-harness-error shape and
its page-mechanics are identical, only the JSON schema and the target section
differ."""

import datetime as dt
import html.parser
import json
import re
from pathlib import Path

import requests

from . import prompts
from .exchange import prose_link_problem
from .pagetext import day_block_replace, log_append, set_frontmatter
from .research import _source_pages
from .structured import (ProposalError, _first_json_object, _prose, _propose,
                         _str)

MAX_PAGE_CHARS = 15000     # fetched page text handed to the model
MAX_FIELD_CHARS = 4000     # each of cause/action
DEFAULT_TIMEOUT = 20
_USER_AGENT = "logbook-research/1 (+https://github.com/davidbudac/logbook)"
_REFERENCE_HEAD_RE = re.compile(r"\A## Reference\s*\Z")


def docs_url(code: str) -> str:
    """The Oracle error-help page for an error-page stem (e.g. `ORA-16607`,
    `TNS-12543`): every code's reference page lives at one predictable path,
    so structured mode needs no search step at all."""
    return f"https://docs.oracle.com/en/error-help/db/{code.lower()}/"


def fetchable_source(wiki) -> tuple[str, list[str]] | None:
    """The one source page structured research is allowed to fetch from: the
    first (path-sorted) `sources/<slug>.md` with `status: approved`,
    `fetchable: true`, and `docs.oracle.com` among its `domains` — in
    practice `sources/oracle-docs.md`. Returns `(slug, domains)`; `None` when
    no such source exists, so the caller refuses to fetch from anywhere
    nobody approved rather than guessing."""
    for slug, fm in _source_pages(Path(wiki)).items():
        if fm.get("status") != "approved" or not fm.get("fetchable"):
            continue
        domains = [str(d).strip().lower() for d in (fm.get("domains") or [])]
        if "docs.oracle.com" in domains:
            return slug, domains
    return None


class _TextExtractor(html.parser.HTMLParser):
    """Readable page text: `script`/`style`/`nav`/`header`/`footer` subtrees
    dropped, everything else's text concatenated and whitespace-collapsed."""

    _SKIP = {"script", "style", "nav", "header", "footer"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self._chunks).split())


def fetch_page(url: str, timeout: int = DEFAULT_TIMEOUT) -> str | None:
    """GET `url` with a plain UA and extract its readable text, capped at
    `MAX_PAGE_CHARS`. `None` on a non-200 response or any request/parse
    exception — a fetch failure is a flag for the caller, never a crash."""
    try:
        resp = requests.get(url, timeout=timeout,
                            headers={"User-Agent": _USER_AGENT})
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    parser = _TextExtractor()
    try:
        parser.feed(resp.text)
    except Exception:  # noqa: BLE001 — malformed HTML must not crash research
        return None
    return parser.text()[:MAX_PAGE_CHARS]


_RESEARCH_CONTRACT = prompts.load("research-contract")
#: framing and rules, a `str.format` template over the code and contract
_RESEARCH_RULES = prompts.load("research")


def build_research_prompt(code: str, page_text: str) -> str:
    """The whole structured-research task in one text prompt. The model gets
    no web access and no file access at all — the page text already fetched
    by deterministic code is everything it may draw on."""
    return (_RESEARCH_RULES.format(code=code, contract=_RESEARCH_CONTRACT)
            + f"\n\nPage text for {code}:\n---\n{page_text}\n---\n")


def parse_research_proposal(text: str) -> dict:
    """Strictly validate the model's answer against the `{cause, action}`
    contract — same shape of rules as structured.parse_proposal."""
    raw = _first_json_object(text)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProposalError(f"response: not valid JSON ({e})") from e
    if not isinstance(obj, dict):
        raise ProposalError("response: expected a JSON object")
    out = {"cause": _str(obj, "cause", max_len=MAX_FIELD_CHARS),
           "action": _str(obj, "action", max_len=MAX_FIELD_CHARS)}
    for key, value in out.items():
        # the citation is built by code; a link in the prose would reach the
        # page past the approved-source rail (issue 10)
        if problem := prose_link_problem(value):
            raise ProposalError(f"{key}: {problem}; no links in prose")
    return out


def propose_research(prompt: str, cfg, *, telemetry: dict | None = None) -> dict:
    """One validated `{cause, action}` proposal, retried once with the
    validation error appended — reuses structured._propose verbatim. Always
    the cheap tier: turning already-fetched text into two prose fields never
    needs escalation."""
    return _propose(prompt, cfg, parse_research_proposal, escalate=False,
                    telemetry=telemetry)


def apply_research(wiki_root, rel_page: str, proposal: dict, url: str,
                   source_slug: str, today: dt.date) -> None:
    """Apply one validated research proposal to `rel_page`. Sets `researched:`
    in the frontmatter (added if missing, replaced in place otherwise — every
    other key and its order preserved, see pagetext.set_frontmatter) and
    replace-or-appends the `## Reference` section with the model's
    cause/action prose, cited against the one source this mode fetched from.
    Idempotent: re-running (e.g. after a later re-fetch) replaces the prior
    `## Reference` block instead of piling one up. Never touches `##
    Occurrences` or anything else already on the page. Also appends one
    log.md line, the same contract every other structured writer keeps."""
    root = Path(wiki_root)
    page = root / rel_page
    text = set_frontmatter(page.read_text(), {"researched": today.isoformat()})
    citation = (f"(source: sources/{source_slug}; url: {url}; "
               f"accessed: {today.isoformat()})")
    # model prose never gets to decide page mechanics: headings/wiki-links it
    # slipped in are flattened exactly like ingest/report prose, so a stray
    # "##" line can never be mistaken for a new section by the lint's
    # Reference-block reader
    cause = _prose(proposal["cause"], root, [])
    action = _prose(proposal["action"], root, [])
    block = (f"\n## Reference\n\n**Cause:** {cause}\n{citation}.\n\n"
            f"**Action:** {action}\n{citation}.\n")
    text = day_block_replace(text, _REFERENCE_HEAD_RE, block)
    page.write_text(text)
    log_path = root / "log.md"
    log_line = f"[{today.isoformat()}] research (structured) — {rel_page}: {url}"
    log_path.write_text(log_append(
        log_path.read_text() if log_path.exists() else None, log_line))
