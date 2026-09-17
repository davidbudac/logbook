"""Research task support: deterministic selection of the workload (which error
pages need external research, which source pages are due for review) and the
citation allowlist the orchestrator enforces. The LLM only judges."""

import datetime as dt
import re
from pathlib import Path
from urllib.parse import urlparse

from .incidents import load_incidents
from .pagetext import frontmatter

URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`;,]+")
SOURCE_RE = re.compile(r"source:\s*(?:sources/)?([A-Za-z0-9._-]+)")
ERROR_LINK_RE = re.compile(r"\[\[errors/([^\]]+)\]\]")

DEFAULT_REVIEW_DAYS = 180


def _as_date(v) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        try:
            return dt.date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _updated_key(v) -> str:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:19]
    return str(v or "")[:19]


def _source_pages(wiki: Path) -> dict[str, dict]:
    """slug -> frontmatter, for every `sources/<slug>.md`."""
    out = {}
    for p in sorted((wiki / "sources").glob("*.md")):
        out[p.stem] = frontmatter(p.read_text())
    return out


def approved_domains(wiki: Path) -> set[str]:
    """Domains citable in Reference sections: the `domains` of every source
    page with `status: approved`. A hostname matches if it equals a listed
    domain or is a subdomain of one (see `domain_approved`)."""
    domains = set()
    for fm in _source_pages(wiki).values():
        if fm.get("status") != "approved":
            continue
        for d in fm.get("domains") or []:
            domains.add(str(d).strip().lower())
    return domains


def sources_due_review(wiki: Path, today: dt.date) -> list[Path]:
    """Approved source pages whose `last_reviewed` is older than
    `review_after_days` (a missing review date is always due)."""
    due = []
    for p in sorted((wiki / "sources").glob("*.md")):
        fm = frontmatter(p.read_text())
        if fm.get("status") != "approved":
            continue
        last = _as_date(fm.get("last_reviewed"))
        try:
            days = int(fm.get("review_after_days", DEFAULT_REVIEW_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_REVIEW_DAYS
        if last is None or last + dt.timedelta(days=days) <= today:
            due.append(p)
    return due


def cited_urls(md_text: str) -> set[str]:
    return set(URL_RE.findall(md_text))


def cited_sources(md_text: str) -> set[str]:
    """Source slugs referenced by `(source: sources/<slug>; url: ...)`."""
    return set(SOURCE_RE.findall(md_text))


def url_domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def domain_approved(host: str, domains: set[str]) -> bool:
    """True when the hostname is a listed domain or a subdomain of one."""
    return any(host == d or host.endswith("." + d) for d in domains)


def unapproved_urls(md_text: str, domains: set[str]) -> set[str]:
    return {u for u in cited_urls(md_text)
            if not domain_approved(url_domain(u), domains)}


def _open_incident_codes(wiki: Path) -> dict[str, int]:
    """Error-page slugs wikilinked from active incidents, mapped to how many
    active incidents link each one. A slug absent from an active incident's
    links has no entry (equivalent to a count of zero)."""
    counts: dict[str, int] = {}
    for inc in load_incidents(wiki):
        if not inc.is_active:
            continue
        for code in inc.error_codes:
            counts[code] = counts.get(code, 0) + 1
    return counts


def _is_stale(text: str, researched: dt.date, sources: dict[str, dict]) -> bool:
    """Research goes stale when a cited source page was reviewed after the
    research was done, was deprecated, or disappeared."""
    for slug in cited_sources(text):
        fm = sources.get(slug)
        if fm is None or fm.get("status") != "approved":
            return True
        last = _as_date(fm.get("last_reviewed"))
        if last and last > researched:
            return True
    return False


def select_error_candidates(wiki: Path, limit: int) -> list[Path]:
    """Error pages needing research: no `researched:` date, or stale relative
    to the sources they cite. Pages linked from more active incidents come
    first, ties broken by the most recently updated."""
    sources = _source_pages(wiki)
    hot_counts = _open_incident_codes(wiki)
    cands = []
    for p in sorted((wiki / "errors").glob("*.md")):
        text = p.read_text()
        fm = frontmatter(text)
        researched = _as_date(fm.get("researched"))
        if researched is not None and not _is_stale(text, researched, sources):
            continue
        cands.append((hot_counts.get(p.stem, 0), _updated_key(fm.get("updated")), p))
    cands.sort(key=lambda c: c[1], reverse=True)
    cands.sort(key=lambda c: c[0], reverse=True)
    return [p for _, _, p in cands[:limit]]
