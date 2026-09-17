"""Deterministic wiki lint — provenance schema v1. Mechanical checks over the
markdown structure only: no LLM, no network, no ES. The agent lint stays
responsible for the semantic rules listed as deferred in docs/provenance.md.

Reuses `pagetext` for the frontmatter regex and research.py for everything
about citations (URL extraction, the approved-domain allowlist) so the
linter and the research rails can never disagree about what may be cited."""

import datetime as dt
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

import yaml

from .evidence_ref import parse_refs
from .incidents import MonitoringWindow, Status, parse_actions, parse_status
from .pagetext import FM_RE
from .research import _as_date, approved_domains, unapproved_urls

PROVENANCE_SCHEMA_VERSION = 1

# TODO: error-class/source/incident are
# evidenced by this repo's code and tests; the rest is inferred from the wiki
# layout in DESIGN.md and needs the authoritative list from wiki/AGENTS.md.
KNOWN_PAGE_TYPES = frozenset({
    "error-class", "source", "incident", "database", "journal", "host",
    "service", "concept", "report", "index", "log",
})

# rule id -> (severity, remediation hint)
RULES: dict[str, tuple[str, str]] = {
    "frontmatter-malformed": (
        "error", "frontmatter must be a `---`-delimited YAML mapping with a "
        "known `type:` (see docs/provenance.md)"),
    "frontmatter-contradictory": (
        "error", "make the frontmatter self-consistent: no date before "
        "`added:`, and `status: approved` needs `domains:`"),
    "incident-status-invalid": (
        "error", "set `status:` to open, monitoring or resolved, and give a "
        "`monitoring:` window while the status is monitoring"),
    "action-malformed": (
        "error", "fix the `## Action` section: `## Action <ISO timestamp>` then "
        "one fenced `yaml` block (see wiki/AGENTS.md)"),
    "evidence-ref-malformed": (
        "error", "fix the `evidence_ref:` yaml block: schema_version 1, a "
        "kind, template_id, environment, data_view, entity, signature and a "
        "`from`/`to` window (see docs/notes/incident-remediation-workflow.md)"),
    "wikilink-broken": (
        "error", "create the target page or fix the link target"),
    "page-orphaned": (
        "warning", "link the page from index.md or from a related page"),
    "digest-missing": (
        "error", "cite a digest that exists under digests/<db>/"),
    "citation-malformed": (
        "error", "cite as `(source: sources/<name>; url: <url>; accessed: "
        "YYYY-MM-DD)` against an approved source page"),
    "encoding-invalid": (
        "error", "re-save the page as UTF-8; no other rule can read it "
        "until then"),
}

# pages exempt from the orphan requirement: entry points, machine output,
# and catalog pages that are reached by path rather than by link
_ORPHAN_EXEMPT_FILES = frozenset({"index.md", "log.md"})
_ORPHAN_EXEMPT_DIRS = ("digests/", "reports/", "sources/")

_INDEX = "index.md"
_EXCEPTIONS_FILE = ".lint-exceptions"

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
DIGEST_REF_RE = re.compile(r"digests/[\w.-]+/[\w.:-]+\.(?:json|md)")
CITATION_RE = re.compile(
    r"\(\s*source:\s*(?:sources/)?([\w.-]+)\s*;"
    r"\s*url:\s*(\S+?)\s*;"
    r"\s*accessed:\s*([^)\s]+)\s*\)")
REFERENCE_HEAD_RE = re.compile(r"^##\s+Reference\b", re.I)
HEADING_RE = re.compile(r"^##\s")
# fields that cannot predate the page itself
_DATE_FIELDS = ("updated", "researched", "last_reviewed")


@dataclass(frozen=True)
class Finding:
    file: str       # wiki-relative POSIX path
    rule: str       # stable kebab-case rule id
    severity: str   # error | warning
    message: str
    hint: str
    suppressed: bool = False

    def to_dict(self) -> dict:
        return {"file": self.file, "rule": self.rule, "severity": self.severity,
                "message": self.message, "hint": self.hint,
                "suppressed": self.suppressed}


def _f(file: str, rule: str, message: str) -> Finding:
    severity, hint = RULES[rule]
    return Finding(file, rule, severity, message, hint)


def _markdown_files(wiki: Path) -> dict[str, Path]:
    out = {}
    for p in sorted(wiki.rglob("*.md")):
        rel = p.relative_to(wiki).as_posix()
        if rel.startswith(".git/"):
            continue
        out[rel] = p
    return out


def resolve_link(target: str, pages: Collection[str]) -> str | None:
    """Wiki-root-relative path first, then a unique basename match (Obsidian
    behavior). An ambiguous stem deliberately does not resolve.

    Public because the read model and the portal's rendered wikilinks must
    resolve a `[[link]]` exactly the way lint judges it; one rule, or a page
    lint calls broken renders as a working link."""
    t = target.strip()
    if not t:
        return None
    for cand in (t, t if t.endswith(".md") else t + ".md"):
        if cand in pages:
            return cand
    stem = Path(t).stem
    hits = [rel for rel in pages if Path(rel).stem == stem]
    return hits[0] if len(hits) == 1 else None


def _load_exceptions(wiki: Path) -> dict[str, set[str]]:
    """path -> {rule ids} suppressed for it; `{"*"}` means every rule."""
    out: dict[str, set[str]] = {}
    f = wiki / _EXCEPTIONS_FILE
    if not f.exists():
        return out
    for line in f.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"\t+|\s{2,}", line) if p.strip()]
        path = parts[0]
        out.setdefault(path, set()).update(parts[1:] or ["*"])
    return out


def _suppressed(finding: Finding, exceptions: dict[str, set[str]]) -> bool:
    rules = exceptions.get(finding.file)
    return bool(rules) and ("*" in rules or finding.rule in rules)


def _frontmatter_findings(rel: str, text: str) -> list[Finding]:
    if not text.startswith("---"):
        return []  # v1 does not require frontmatter, only well-formed frontmatter
    m = FM_RE.match(text)
    if not m:
        return [_f(rel, "frontmatter-malformed",
                   "frontmatter block is not closed by a `---` line")]
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        return [_f(rel, "frontmatter-malformed",
                   f"frontmatter is not valid YAML: {str(e).splitlines()[0]}")]
    if not isinstance(data, dict):
        return [_f(rel, "frontmatter-malformed",
                   "frontmatter is not a YAML mapping")]
    out = []
    ptype = data.get("type")
    if ptype is not None and str(ptype) not in KNOWN_PAGE_TYPES:
        out.append(_f(rel, "frontmatter-malformed",
                      f"unknown page type {str(ptype)!r}"))
    if str(ptype) == "incident":
        out += _incident_findings(rel, text, data)
    added = _as_date(data.get("added"))
    if added:
        for key in _DATE_FIELDS:
            d = _as_date(data.get(key))
            if d and d < added:
                out.append(_f(rel, "frontmatter-contradictory",
                              f"{key}: {d} is earlier than added: {added}"))
    if data.get("status") == "approved" and not data.get("domains"):
        out.append(_f(rel, "frontmatter-contradictory",
                      "status: approved without any `domains:` — nothing "
                      "could be cited from it"))
    return out


def _incident_findings(rel: str, text: str, data: dict) -> list[Finding]:
    """What an incident page may say, per `incidents`: a status in the
    vocabulary, a readable window whenever it claims to be monitoring, and
    `## Action` sections that parse. One rule id per cause."""
    out = []
    status = parse_status(data.get("status"))
    if status is None:
        msg = "no `status:` key; expected one of open, monitoring, resolved"
        if "status" in data:
            msg = (f"status: {data['status']!r} is not one of open, "
                   f"monitoring, resolved")
        out.append(_f(rel, "incident-status-invalid", msg))
    elif status is Status.MONITORING and MonitoringWindow.from_frontmatter(
            data.get("monitoring")) is None:
        msg = ("status: monitoring with no `monitoring:` key; the page never "
               "says what it is waiting for")
        if "monitoring" in data:
            msg = (f"status: monitoring with an unreadable `monitoring:` "
                   f"window: {data['monitoring']!r}")
        out.append(_f(rel, "incident-status-invalid", msg))
    out += [_f(rel, "action-malformed",
               f"{problem.heading}: {problem.message}")
            for problem in parse_actions(text).problems]
    return out


def _wikilink_findings(rel: str, targets: list[str],
                       pages: Collection[str]) -> list[Finding]:
    out = []
    for t in dict.fromkeys(targets):
        if t.strip().startswith("digests/"):
            continue  # reported as digest-missing, so one rule id per cause
        if resolve_link(t, pages) is None:
            out.append(_f(rel, "wikilink-broken",
                          f"[[{t}]] matches no page"))
    return out


def _digest_findings(rel: str, text: str, targets: list[str],
                     wiki: Path) -> list[Finding]:
    refs = set(DIGEST_REF_RE.findall(text))
    for t in targets:
        t = t.strip()
        if t.startswith("digests/"):
            refs.add(t if t.endswith((".json", ".md")) else t + ".md")
    return [_f(rel, "digest-missing", f"referenced digest does not exist: {r}")
            for r in sorted(refs) if not (wiki / r).exists()]


def _evidence_ref_findings(rel: str, text: str) -> list[Finding]:
    """Every `evidence_ref:` block that claims to be one and is not.

    Checked on every curated page rather than on incidents alone, because the
    document places these blocks on incident, error, action and resolution
    pages: a reference is evidence, and evidence is cited wherever a
    conclusion is drawn."""
    return [_f(rel, "evidence-ref-malformed",
               f"{problem.where}: {problem.message}")
            for problem in parse_refs(text)[1]]


def _reference_items(text: str) -> list[str]:
    """Logical items under a `## Reference` heading (up to the next `##`
    heading): a bullet plus its wrapped continuation lines is ONE item, the
    way markdown renders it — agents wrap prose at ~80 columns, so a citation
    split across physical lines must still be judged whole. Blank lines and
    new bullets start a new item."""
    items: list[str] = []
    inside = open_item = False
    for line in text.splitlines():
        if HEADING_RE.match(line):
            inside = bool(REFERENCE_HEAD_RE.match(line))
            open_item = False
            continue
        if not inside:
            continue
        stripped = line.strip()
        if not stripped:
            open_item = False
        elif open_item and not stripped.startswith(("- ", "* ")):
            items[-1] += " " + stripped
        else:
            items.append(stripped)
            open_item = True
    return items


def _citation_findings(rel: str, text: str, wiki: Path,
                       domains: set[str]) -> list[Finding]:
    out = []
    for line in _reference_items(text):
        cites = CITATION_RE.findall(line)
        external = bool(re.search(r"https?://", line)) or "source:" in line
        if external and not cites:
            out.append(_f(rel, "citation-malformed",
                          f"external claim without a well-formed citation: "
                          f"{line.strip()[:120]}"))
        for slug, _url, accessed in cites:
            if not (wiki / "sources" / f"{slug}.md").exists():
                out.append(_f(rel, "citation-malformed",
                              f"cites sources/{slug}, which is not a source page"))
            try:
                dt.date.fromisoformat(accessed)
            except ValueError:
                out.append(_f(rel, "citation-malformed",
                              f"invalid accessed date {accessed!r} "
                              f"(expected YYYY-MM-DD)"))
        for url in sorted(unapproved_urls(line, domains)):
            out.append(_f(rel, "citation-malformed",
                          f"cites {url}, whose host is not on an approved "
                          f"source's domains"))
    return out


def _orphan_findings(links: dict[str, set[str]],
                     pages: dict[str, Path]) -> list[Finding]:
    inbound = {t for src, ts in links.items() for t in ts if t != src}
    reachable, queue = set(), [_INDEX] if _INDEX in pages else []
    while queue:
        cur = queue.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        queue.extend(links.get(cur, ()))
    out = []
    for rel in links:
        if rel in _ORPHAN_EXEMPT_FILES or rel.startswith(_ORPHAN_EXEMPT_DIRS):
            continue
        if rel not in inbound and rel not in reachable:
            out.append(_f(rel, "page-orphaned",
                          "no inbound wikilink and not reachable from index.md"))
    return out


def lint_wiki(wiki_root: Path | str,
              only_paths: list[str] | None = None) -> list[Finding]:
    """Deterministic v1 findings for the wiki at `wiki_root`.

    `only_paths` restricts *reporting* to those wiki-relative paths (what an
    agent stage touched); the page inventory and link graph are always built
    over the whole wiki so link/orphan resolution stays correct.

    A page whose bytes are not UTF-8 costs itself one `encoding-invalid`
    finding and nothing more: it stays in the inventory, so `[[links]]` to it
    still resolve, and only its own text goes unread, so its own links are
    not checked and it is not orphan-reported."""
    wiki = Path(wiki_root)
    if not wiki.is_dir():
        raise FileNotFoundError(f"wiki root does not exist: {wiki}")
    pages = _markdown_files(wiki)
    # digests are compactor output, not curated pages: linkable, never linted
    texts, undecodable = {}, {}
    for rel, path in pages.items():
        if rel.startswith("digests/"):
            continue
        try:
            texts[rel] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            undecodable[rel] = f"{exc.reason} at byte {exc.start}"
    raw_links = {rel: WIKILINK_RE.findall(t) for rel, t in texts.items()}
    links = {rel: {r for r in (resolve_link(t, pages) for t in ts) if r}
             for rel, ts in raw_links.items()}
    domains = approved_domains(wiki)
    only = None if only_paths is None else {str(p) for p in only_paths}

    findings = [_f(rel, "encoding-invalid", f"not valid UTF-8: {why}")
                for rel, why in sorted(undecodable.items())
                if only is None or rel in only]
    for rel, text in sorted(texts.items()):
        if only is not None and rel not in only:
            continue
        findings += _frontmatter_findings(rel, text)
        findings += _wikilink_findings(rel, raw_links[rel], pages)
        findings += _digest_findings(rel, text, raw_links[rel], wiki)
        findings += _evidence_ref_findings(rel, text)
        findings += _citation_findings(rel, text, wiki, domains)
    findings += [f for f in _orphan_findings(links, pages)
                 if only is None or f.file in only]

    exceptions = _load_exceptions(wiki)
    return [f if not _suppressed(f, exceptions)
            else Finding(f.file, f.rule, f.severity, f.message, f.hint, True)
            for f in findings]


def blocking(findings: list[Finding]) -> list[Finding]:
    """Findings that must fail a run: unsuppressed errors."""
    return [f for f in findings if f.severity == "error" and not f.suppressed]


def format_findings(findings: list[Finding]) -> str:
    lines = []
    for f in findings:
        mark = " [suppressed]" if f.suppressed else ""
        lines.append(f"{f.file}: {f.severity}: {f.rule}: {f.message}{mark}")
        if not f.suppressed:
            lines.append(f"    hint: {f.hint}")
    errors = len(blocking(findings))
    warns = len([f for f in findings
                 if f.severity == "warning" and not f.suppressed])
    supp = len([f for f in findings if f.suppressed])
    lines.append(f"{errors} error(s), {warns} warning(s), {supp} suppressed")
    return "\n".join(lines)
