"""Research offload (ADR-0002), on-prem half: turn one `errors/<CODE>.md`
page plus what the box knows about the code into the anonymized *research
request* that goes to the exchange, and turn a *source-review* into its
request. Everything identifying is pseudonymized by `redact.Redactor` and
re-checked before the request is handed back; a leak refuses the request
(`RedactionLeak`) rather than shipping a partial payload.

This module builds requests only. Enqueue/fold-in (queue plumbing, result
validation, `apply_research`) is the next rollout step and lives elsewhere so
`dbwiki redact --show` can print exactly this output with nothing else."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter
from pathlib import Path

from . import prompts
from .incidents import Incident, by_error_code, load_incidents
from .pagetext import day_block_replace, frontmatter, log_append, oneline, set_frontmatter
from .redact import (_PSEUDONYM_RE, Redactor, RedactionLeak, Vocabulary,
                     build_vocabulary, strip_wiki_refs)
from .research import _source_pages
from .validate import confined, safe_id

SCHEMA_VERSION = 1

MAX_MESSAGES = 5          # distinct message samples per request
MAX_MESSAGE_CHARS = 600
MAX_CONTEXT = 12          # Occurrence note cells
MAX_CONTEXT_CHARS = 300
MAX_REFERENCE_CHARS = 4000
MAX_CO_CODES = 10

_CODE_RE = re.compile(r"\b(?:ORA|TNS|RMAN|DGM|DGMGRL|PLS|LRM|KUP|SP2)-\d{1,6}\b")
_ROW_RE = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([^|]*?)\s*\|\s*(.*?)\s*\|\s*([^|]*?)\s*\|\s*$")
_SECTION_RE = re.compile(r"^## (.+?)\s*$", re.M)

_RESEARCH_INSTRUCTIONS = prompts.load("research-offload")


def sections(text: str) -> dict[str, str]:
    """`## Heading` -> body text (frontmatter and the H1 excluded)."""
    out: dict[str, str] = {}
    heads = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out[m.group(1).strip()] = text[m.end():end].strip()
    return out


def occurrence_rows(text: str) -> list[dict]:
    """Rows of the `## Occurrences` table: day, db, note, evidence."""
    rows = []
    for line in sections(text).get("Occurrences", "").splitlines():
        m = _ROW_RE.match(line.strip())
        if not m or m.group(1) == "day":
            continue
        rows.append({"day": m.group(1), "db": m.group(2), "note": m.group(3),
                     "evidence": m.group(4)})
    return rows


def _digest_json(wiki: Path, evidence: str) -> dict | None:
    rel = evidence.strip()
    if not rel.startswith("digests/"):
        return None
    p = (wiki / rel).with_suffix(".json")
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _digest_groups(digest: dict):
    for src in (digest.get("sources") or {}).values():
        if isinstance(src, dict):
            for g in src.get("notable") or []:
                if isinstance(g, dict):
                    yield g


def _registry_span(state_dir: Path | None, code: str) -> tuple[str, str]:
    """Earliest first_seen / latest last_seen for the code over every
    registry (all dbs, all sources); "" when unknown."""
    first, last = "", ""
    if not state_dir:
        return first, last
    for p in sorted((Path(state_dir) / "registry").glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        for by_src in (d.get("codes") or {}).values():
            rec = (by_src or {}).get(code) if isinstance(by_src, dict) else None
            if not rec:
                continue
            fs, ls = str(rec.get("first_seen") or ""), str(rec.get("last_seen") or "")
            if fs and (not first or fs < first):
                first = fs
            if ls and ls > last:
                last = ls
    return first, last


def _incident_state(index: dict[str, list[Incident]], code: str) -> dict:
    linked = index.get(code, ())
    return {"open_incidents": sum(1 for inc in linked if inc.is_active),
            "resolved_incidents": sum(1 for inc in linked if not inc.is_active)}


def _sources_registry(wiki: Path) -> list[dict]:
    out = []
    for slug, fm in _source_pages(wiki).items():
        out.append({"slug": slug,
                    "domains": [str(d).lower() for d in (fm.get("domains") or [])],
                    "status": str(fm.get("status") or ""),
                    "fetchable": bool(fm.get("fetchable", False))})
    return out


def _estate(cfg) -> dict:
    rcfg = cfg.research
    est = dict(rcfg.get("estate") or {})
    est.setdefault("product", "Oracle Database")
    return est


def _truncate(text: str, limit: int) -> str:
    """Cut already-redacted text to `limit` characters, never through a
    pseudonym: `HOST_AB` cut to `HOST_A` would name a different host."""
    if len(text) <= limit:
        return text
    for m in _PSEUDONYM_RE.finditer(text, max(0, limit - 8), limit + 8):
        if m.start() < limit < m.end():
            return text[:m.start()]
    return text[:limit]


def build_research_request(cfg, wiki: Path, page: Path, run_id: str, *,
                           today: dt.date | None = None,
                           vocab: Vocabulary | None = None,
                           incident_index: dict[str, list[Incident]] | None = None,
                           state_dir: Path | None = None) -> tuple[dict, Redactor]:
    """The research request for one error page, fully redacted and
    leak-checked. Returns `(request, redactor)`; the caller persists
    `redactor.save(state_dir)` when it enqueues (and only then) — one mapping
    file per request, `.state/redaction/<run_id>-<CODE>.json`. Raises
    `RedactionLeak` if identifying content survived redaction.

    `vocab` and `incident_index` are built here when absent; a caller
    requesting more than one page builds each once per offload instead."""
    wiki = Path(wiki)
    page = Path(page)
    today = today or dt.date.today()
    state_dir = state_dir if state_dir is not None else cfg.state_dir
    vocab = vocab or build_vocabulary(cfg, wiki, state_dir)
    if incident_index is None:
        incident_index = by_error_code(load_incidents(wiki))
    code = page.stem
    red = Redactor(vocab, run_id, key=f"{run_id}-{code}")
    text = page.read_text()
    rows = occurrence_rows(text)
    secs = sections(text)

    # message samples + co-occurring codes from the digests the page cites
    messages: dict[str, str] = {}          # template -> raw sample
    co: Counter = Counter()
    count = 0
    for r in rows:
        d = _digest_json(wiki, r["evidence"])
        if not d:
            continue
        for g in _digest_groups(d):
            codes = [str(c) for c in (g.get("codes") or [])]
            if code not in codes:
                continue
            count += int(g.get("count") or 0)
            tmpl = str(g.get("template") or g.get("message") or "")
            if tmpl and tmpl not in messages:
                messages[tmpl] = str(g.get("message") or tmpl)
            co.update(c for c in codes if c != code)
    if not co:
        # weak fallback: other codes seen in the same digests / notes
        for r in rows:
            co.update(c for c in _CODE_RE.findall(r["note"]) if c != code)
    first, last = _registry_span(state_dir, code)
    days = sorted({r["day"] for r in rows})
    if not first and days:
        first = days[0]
    if not last and days:
        last = days[-1]

    inc = _incident_state(incident_index, code)
    synopsis = {
        "message_templates": list(messages.values())[:MAX_MESSAGES],
        "occurrences": {"count": count or len(rows), "first_seen": first,
                        "last_seen": last, "spread_days": len(days)},
        "co_occurring_codes": [c for c, _ in co.most_common(MAX_CO_CODES)],
        "context": [strip_wiki_refs(r["note"]) for r in rows[-MAX_CONTEXT:] if r["note"]],
        "current_reference": strip_wiki_refs(secs.get("Reference", "")),
        **inc,
        "resolution_state": (f"resolved in {inc['resolved_incidents']} incident(s)"
                             if inc["resolved_incidents"] and not inc["open_incidents"]
                             else "no confirmed fix"),
    }

    # redact first, truncate after: a name or address cut in half before
    # redaction no longer matches its term or pattern and ships (issue 07)
    synopsis = red.redact_obj(synopsis)
    synopsis["message_templates"] = [_truncate(m, MAX_MESSAGE_CHARS)
                                     for m in synopsis["message_templates"]]
    synopsis["context"] = [_truncate(c, MAX_CONTEXT_CHARS) for c in synopsis["context"]]
    synopsis["current_reference"] = _truncate(synopsis["current_reference"],
                                              MAX_REFERENCE_CHARS)

    request = {
        "schema_version": SCHEMA_VERSION,
        "kind": "research",
        "run_id": run_id,
        "code": code,
        "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attempts": 0,
        "estate": _estate(cfg),
        "synopsis": synopsis,
        "sources": _sources_registry(wiki),
        "instructions": _RESEARCH_INSTRUCTIONS,
    }
    # fail closed: the whole request is re-scanned, not just the synopsis
    red.check(request)
    return request, red


def build_source_review_request(wiki: Path, source_page: Path, run_id: str, *,
                                vocab: Vocabulary | None = None) -> dict:
    """Source-review requests carry no estate data by design, but the source
    page's body travels as `previous_notes` and a human may have mentioned
    a host or a database there. The notes are redacted (vocabulary built
    from the wiki when not given), the request leak-checked; notes that
    still leak are dropped rather than sent, and a request that leaks even
    without them raises RedactionLeak (fail closed)."""
    wiki = Path(wiki)
    fm = frontmatter(source_page.read_text())
    text = source_page.read_text()
    body = strip_wiki_refs(text.split("---", 2)[-1] if text.startswith("---") else text)
    if vocab is None:
        vocab = build_vocabulary(None, wiki)
    red = Redactor(vocab, run_id)
    request = {
        "schema_version": SCHEMA_VERSION,
        "kind": "source-review",
        "run_id": run_id,
        "slug": source_page.stem,
        "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attempts": 0,
        "url": str(fm.get("url") or ""),
        "domains": [str(d).lower() for d in (fm.get("domains") or [])],
        "status": str(fm.get("status") or ""),
        "last_reviewed": str(fm.get("last_reviewed") or ""),
        "review_after_days": fm.get("review_after_days"),
        "previous_notes": _truncate(red.redact(body), 2000),
    }
    if red.leak_check(request):
        request["previous_notes"] = ""
        red.check(request)
    return request


def request_filename(request: dict) -> str:
    """`research-<CODE>-<run_id>.json` / `source-review-<slug>-<run_id>.json`
    (raises ValueError for an unsafe id, like exchange.request_filename)."""
    key = request.get("code") or request.get("slug") or "unknown"
    return f"{request['kind']}-{safe_id(key, 'key')}-{safe_id(request['run_id'], 'run_id')}.json"


__all__ = ["build_research_request", "build_source_review_request",
           "request_filename", "occurrence_rows", "sections", "RedactionLeak"]


RESULT_MAX_FIELD_CHARS = 4000
RESULT_MAX_REFERENCES = 8
LEAK_LOG = "redaction_leaks.jsonl"
PROPOSED_STATUSES = ("approved", "deprecated")
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}")


def _page_problem(wiki: Path, sub: str, name, what: str) -> str | None:
    """`<wiki>/<sub>/<name>.md` must be a safe id, stay under `<sub>/`
    (symlinks resolved) and exist. The id arrives from the exchange, i.e.
    from the other side of the trust boundary."""
    try:
        safe_id(name, what)
        path = confined(Path(wiki) / sub, f"{name}.md")
    except ValueError as exc:
        return str(exc)
    if not path.is_file():
        return f"no {'error' if sub == 'errors' else 'source'} page for {what} {name!r}"
    return None


def _date_problem(value, what: str) -> str | None:
    """A `YYYY-MM-DD` date; only the first ten characters are ever written,
    so anything after them must not be there either."""
    s = str(value or "")
    try:
        dt.date.fromisoformat(s)
    except ValueError:
        return f"{what}: {value!r} is not a date (YYYY-MM-DD)"
    return None if _DATE_RE.match(s) and len(s) == 10 else f"{what}: {value!r} is not a date"


def validate_research_result(res: dict, wiki: Path) -> list[str]:
    """Schema and policy checks on a researcher's answer, before anything is
    de-mapped or written: right kind, a page to write to, prose fields of
    bounded size, references only to approved source pages on their own
    domains. Problems, empty when clean."""
    from .exchange import citation_url_problem, prose_link_problem
    from .research import _source_pages
    wiki = Path(wiki)
    problems: list[str] = []
    if res.get("kind") != "research":
        problems.append(f"kind={res.get('kind')!r}, expected 'research'")
    if res.get("schema_version") not in (None, SCHEMA_VERSION):
        problems.append(f"schema_version {res.get('schema_version')!r} unsupported")
    if p := _page_problem(wiki, "errors", res.get("code"), "code"):
        problems.append(p)
    try:
        safe_id(res.get("run_id"), "run_id")
    except ValueError as exc:
        problems.append(str(exc))
    for key in ("cause", "action"):
        v = res.get(key)
        if not isinstance(v, str) or not v.strip():
            problems.append(f"{key}: missing or empty")
        elif len(v) > RESULT_MAX_FIELD_CHARS:
            problems.append(f"{key}: {len(v)} chars > {RESULT_MAX_FIELD_CHARS}")
        elif p := prose_link_problem(v):
            problems.append(f"{key}: {p} (links belong in references)")
    refs = res.get("references")
    if not isinstance(refs, list) or not refs:
        problems.append("references: missing or empty (every claim needs a citation)")
        refs = []
    if len(refs) > RESULT_MAX_REFERENCES:
        problems.append(f"references: {len(refs)} > {RESULT_MAX_REFERENCES}")
    sources = _source_pages(wiki)
    for i, ref in enumerate(refs):
        if not isinstance(ref, dict):
            problems.append(f"references[{i}]: not an object")
            continue
        slug = str(ref.get("source") or "").removeprefix("sources/")
        fm = sources.get(slug)
        if fm is None or fm.get("status") != "approved":
            problems.append(f"references[{i}]: source {slug!r} is not an approved source page")
            continue
        url = ref.get("url")
        domains = {str(d).strip().lower() for d in (fm.get("domains") or [])}
        if p := citation_url_problem(url, domains):
            problems.append(f"references[{i}]: url {str(url)[:120]!r} is not on "
                            f"sources/{slug} domains ({p})")
        if p := _date_problem(ref.get("accessed"), f"references[{i}].accessed"):
            problems.append(p)
    for key in ("related_codes", "flags"):
        v = res.get(key)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            problems.append(f"{key}: must be a list of strings")
    return problems


def validate_source_review_result(res: dict, wiki: Path) -> list[str]:
    problems: list[str] = []
    if res.get("kind") != "source-review":
        problems.append(f"kind={res.get('kind')!r}, expected 'source-review'")
    if p := _page_problem(wiki, "sources", res.get("slug"), "slug"):
        problems.append(p)
    if not isinstance(res.get("still_valid"), bool):
        problems.append("still_valid: must be a boolean")
    if p := _date_problem(res.get("checked"), "checked"):
        problems.append(p)
    notes = res.get("notes")
    if notes is not None and (not isinstance(notes, str) or len(notes) > 2000):
        problems.append("notes: must be a string of at most 2000 chars")
    ps = res.get("proposed_status")
    if ps is not None and ps not in PROPOSED_STATUSES:
        # free text here would reach log.md and the run flags verbatim
        problems.append(f"proposed_status: must be one of {', '.join(PROPOSED_STATUSES)}")
    return problems


def _citations(refs: list[dict]) -> str:
    return " ".join(
        f"(source: sources/{str(r.get('source')).removeprefix('sources/')}; "
        f"url: <{r.get('url')}>; accessed: {str(r.get('accessed'))[:10]})"
        for r in refs)


def apply_research_result(wiki_root, rel_page: str, res: dict, today: dt.date) -> None:
    """Deterministic writer for an (already validated, already de-mapped)
    research result: same page mechanics as research_structured.apply_research
    — `researched:` set, `## Reference` replaced-or-appended, prose flattened,
    one log.md line — but citing every reference the researcher actually
    read, and appending `related_codes` as a plain-text hint when present."""
    from .research_structured import _REFERENCE_HEAD_RE
    from .structured import _prose
    root = Path(wiki_root)
    page = confined(root, rel_page)          # defence in depth: validated upstream
    text = set_frontmatter(page.read_text(), {"researched": today.isoformat()})
    cites = _citations(res["references"])
    cause = _prose(res["cause"], root, [])
    action = _prose(res["action"], root, [])
    block = (f"\n## Reference\n\n**Cause:** {cause}\n{cites}.\n\n"
             f"**Action:** {action}\n{cites}.\n")
    related = [c for c in (res.get("related_codes") or []) if re.fullmatch(r"[A-Z0-9]+-\d+", c)]
    if related:
        block += f"\nRelated codes (per the sources above): {', '.join(related)}.\n"
    text = day_block_replace(text, _REFERENCE_HEAD_RE, block)
    page.write_text(text)
    log_path = root / "log.md"
    urls = ", ".join(str(r.get("url")) for r in res["references"][:3])
    log_line = f"[{today.isoformat()}] research (offload) — {rel_page}: {urls}"
    log_path.write_text(log_append(
        log_path.read_text() if log_path.exists() else None, log_line))


def apply_source_review_result(wiki_root, rel_page: str, res: dict, today: dt.date) -> None:
    """Source review: on-prem sets `last_reviewed` to the researcher's
    `checked` date. `status` is never changed here — a `still_valid: false`
    or a `proposed_status` is surfaced in the log line and the run flags for
    a human to act on (source pages enter and change status by human
    commit only)."""
    root = Path(wiki_root)
    page = confined(root, rel_page)          # defence in depth: validated upstream
    checked = str(res.get("checked"))[:10]
    page.write_text(set_frontmatter(page.read_text(), {"last_reviewed": checked}))
    verdict = "still valid" if res.get("still_valid") else "NOT valid — needs a human"
    if res.get("proposed_status"):
        verdict += f"; proposed status: {res['proposed_status']}"
    notes = oneline(res.get("notes") or "")[:160]
    log_path = root / "log.md"
    log_line = f"[{today.isoformat()}] source review (offload) — {rel_page}: {verdict}" \
               + (f" — {notes}" if notes else "")
    log_path.write_text(log_append(
        log_path.read_text() if log_path.exists() else None, log_line))


_TELE_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}\Z")


def _num(v, hi: float, *, integer: bool = False) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return (not integer or isinstance(v, int)) and 0 <= v <= hi


def clean_telemetry(tele) -> dict | None:
    """The researcher's self-reported telemetry, schema-checked before it
    joins `.state/agent_runs.jsonl` (issue 10): only the fields a harness
    run records, each with its type and a sane range; anything else —
    unknown keys, step previews, out-of-range cost — is dropped, and the
    record is marked `reported_by: researcher` so `dbwiki stats` can tell
    self-reported numbers from measured ones. None when not a dict."""
    from .validate import is_safe_id
    if not isinstance(tele, dict):
        return None
    out: dict = {}
    for key in ("adapter", "model", "researcher"):
        v = tele.get(key)
        if isinstance(v, str) and _TELE_NAME_RE.match(v):
            out[key] = v
    if tele.get("task") == "research":
        out["task"] = "research"
    if _num(tele.get("duration_s"), 86400):
        out["duration_s"] = tele["duration_s"]
    for key, hi in (("exit_code", 255), ("stdout_bytes", 10**9), ("prompt_bytes", 10**9),
                    ("attempts", 10)):
        if _num(tele.get(key), hi, integer=True):
            out[key] = tele[key]
    if isinstance(tele.get("timed_out"), bool):
        out["timed_out"] = tele["timed_out"]
    usage = tele.get("usage")
    if usage == "unknown":
        out["usage"] = "unknown"
    elif isinstance(usage, dict):
        out["usage"] = {k: usage[k] for k, hi in (("input_tokens", 10**8),
                                                  ("output_tokens", 10**8))
                        if _num(usage.get(k), hi, integer=True)}
        if _num(usage.get("cost_usd"), 100):
            out["usage"]["cost_usd"] = usage["cost_usd"]
        elif usage.get("cost_usd") == "unknown":
            out["usage"]["cost_usd"] = "unknown"
    if isinstance(tele.get("event_id"), str) and is_safe_id(tele["event_id"]):
        out["event_id"] = tele["event_id"]
    out["reported_by"] = "researcher"
    return out


class ResultRejected(ValueError):
    """An exchange result the on-prem side will not apply; the message is
    the reason recorded with it in the exchange's failed/."""


def fold_result(wiki: Path, state_dir: Path, res, today: dt.date
                ) -> tuple[str, str, list[str]]:
    """Validate, de-map and write ONE researcher result through the
    deterministic writer: `(rel_page, commit_subject, flags)`.

    Everything in `res` came from the other side of the trust boundary, so
    every way it can go wrong is this one result's problem: a validation
    failure, a missing redaction mapping, or the writer itself failing
    (an id that validated but no longer resolves, a full disk) all raise
    `ResultRejected`, and the caller restores the tree and moves the result
    to failed/ — the results behind it still fold."""
    wiki = Path(wiki)
    if not isinstance(res, dict):
        raise ResultRejected("result is not a JSON object")
    kind = res.get("kind")
    flags: list[str] = []
    if kind == "research":
        problems = validate_research_result(res, wiki)
        key = f"{res.get('run_id')}-{res.get('code')}"
        if not problems and not Redactor.mapping_path(state_dir, key).is_file():
            problems.append(f"unknown run_id {res.get('run_id')!r} (no redaction "
                            f"mapping on-prem for {key})")
        if problems:
            raise ResultRejected("; ".join(problems))
        rel = f"errors/{res['code']}.md"
        try:
            clean = Redactor.load(state_dir, key).demap_obj(res)
            apply_research_result(wiki, rel, clean, today)
        except Exception as exc:  # noqa: BLE001 — any failure rejects this result only
            raise ResultRejected(f"could not apply: {type(exc).__name__}: {exc}") from exc
        return rel, f"research (offload) — {rel}", flags
    if kind == "source-review":
        problems = validate_source_review_result(res, wiki)
        if problems:
            raise ResultRejected("; ".join(problems))
        rel = f"sources/{res['slug']}.md"
        try:
            apply_source_review_result(wiki, rel, res, today)
        except Exception as exc:  # noqa: BLE001 — any failure rejects this result only
            raise ResultRejected(f"could not apply: {type(exc).__name__}: {exc}") from exc
        if not res.get("still_valid") or res.get("proposed_status"):
            flags.append(f"source review: {rel} "
                         f"{'still valid' if res.get('still_valid') else 'NOT valid'}"
                         + (f", proposed status {res['proposed_status']}"
                            if res.get("proposed_status") else ""))
        return rel, f"source review (offload) — {rel}", flags
    raise ResultRejected(f"unknown result kind {kind!r}")


def record_leak(state_dir: Path, run_id: str, code: str, hits: list[str]) -> None:
    """A refused request is a health fact: append it to
    `.state/redaction_leaks.jsonl` (the hits stay on-prem — this file is the
    one place the offending tokens are written down)."""
    p = Path(state_dir) / LEAK_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps({"at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "run_id": run_id, "code": code,
                             "hits": sorted(set(hits))[:20]}) + "\n")


def leak_records(state_dir: Path) -> list[dict]:
    p = Path(state_dir) / LEAK_LOG
    if not p.exists():
        return []
    out = []
    for ln in p.read_text().splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out
