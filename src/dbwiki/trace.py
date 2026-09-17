"""Trace-file evidence: the alert log names a `.trc` file, this fetches that
file's ingested chunks and cuts a bounded excerpt for the digest.

Deterministic, no LLM. The `awr.py` stance applies: an excerpt is context for
a group that is already notable, never a reason to wake anyone — nothing here
sets `notable`, and a lookup that fails degrades to "no trace evidence"
instead of failing the compaction."""

import datetime as dt
import sys

import requests

from .es import ES
from .patterns import ORA_CODE_RE, extract_codes

HEAD_LINES = 3      # opening lines kept from the file header chunk
CONTEXT_LINES = 2   # lines kept either side of an ORA-code line
ELLIPSIS = "…"
TRUNCATED = "\n[truncated]"


def _shift(ts: str, hours: float) -> str:
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is not None:
        t = t.astimezone(dt.timezone.utc)
    return (t + dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_trace_docs(es: ES, tl: dict, path: str, t0: str, t1: str) -> list[dict]:
    """The trace chunks for one file, oldest first. `[]` when the data stream
    is missing, holds nothing for this path, or Elasticsearch is unhappy: a
    transient lookup failure must never cost a whole digest."""
    slack = tl.get("slack_hours", 1)
    ts_field = tl.get("timestamp_field", "@timestamp")
    body = {
        "size": tl.get("max_docs_per_path", 5),
        "query": {"bool": {"filter": [
            {"range": {ts_field: {"gte": _shift(t0, -slack), "lt": _shift(t1, slack)}}},
            {"term": {tl["path_field"]: path}},
        ]}},
        # offset first: file order is offset order, and the pipeline stamps
        # some chunks' @timestamp from parsed trace content rather than
        # arrival, so a timestamp-led sort scrambles the file. unmapped_type:
        # an index generation without the offset field must not turn the
        # whole lookup into a 400.
        "sort": [{"log.offset": {"order": "asc", "unmapped_type": "long"}},
                 {ts_field: "asc"}],
    }
    index = ",".join(tl["index_patterns"])
    try:
        hits = es.search(index, body)["hits"]["hits"]
    except requests.RequestException as e:
        print(f"trace lookup for {path} failed, continuing without it: {e}",
              file=sys.stderr)
        return []
    return [{"index": h["_index"], "id": h["_id"],
             "ts": h["_source"].get(ts_field, ""),
             "message": h["_source"].get("message") or ""}
            for h in hits]


def _carries_code(message: str, codes: list[str]) -> bool:
    return bool(codes) and bool(set(extract_codes(message)) & set(codes))


def _excerpt_lines(lines: list[str], keep: set[int]) -> list[str]:
    """`lines` reduced to the kept ones, with one `…` standing in for each run
    of dropped ones (including a dropped head or tail)."""
    out: list[str] = []
    gap = False
    for n, line in enumerate(lines):
        if n not in keep:
            gap = True
            continue
        if gap:
            out.append(ELLIPSIS)
            gap = False
        out.append(line.rstrip())
    if gap and out:
        out.append(ELLIPSIS)
    return out


def select_excerpt(docs: list[dict], codes: list[str],
                   max_chars: int) -> tuple[str, bool]:
    """The excerpt for one trace file, and whether it was cut short.

    Chunks whose text carries one of the referring groups' ORA codes come
    first, the rest keep fetch order. From each chunk we keep every ORA-code
    line with its context, plus the opening lines of the leading chunk and of
    the `Trace file …` header wherever it landed."""
    ordered = sorted(docs, key=lambda d: not _carries_code(d["message"], codes))
    out: list[str] = []
    for i, doc in enumerate(ordered):
        lines = doc["message"].splitlines()
        head = i == 0 or (lines and lines[0].startswith("Trace file "))
        keep = set(range(min(HEAD_LINES, len(lines)))) if head else set()
        for n, line in enumerate(lines):
            if ORA_CODE_RE.search(line):
                keep |= set(range(max(0, n - CONTEXT_LINES),
                                  min(len(lines), n + CONTEXT_LINES + 1)))
        out.extend(_excerpt_lines(lines, keep))
    text = "\n".join(out).strip()
    if len(text) <= max_chars:
        return text, False
    keep = max_chars - len(TRUNCATED)
    if keep <= 0:
        return "", True
    return text[:keep].rstrip() + TRUNCATED, True


def trace_evidence(es: ES, tl: dict, paths_meta: dict[str, dict],
                   t0: str, t1: str) -> list[dict]:
    """One entry per trace path that Elasticsearch actually has documents for,
    in first-mention order, under the per-digest character budget. Paths with
    no documents (an incident-dir dump filebeat never ships, or a trace that
    has not arrived yet) contribute nothing at all, so a fleet without the
    trace stream keeps writing exactly the digests it wrote before.

    `max_total_chars` shrinks excerpts rather than dropping paths, so which
    paths appear depends only on what Elasticsearch holds. Deciding admission
    on excerpt length instead would put the excerpt heuristic back inside the
    content hash by the back door, which is exactly what `content_hash`
    promises it is not. A path that runs out of room keeps its entry, its
    document count and its `es_samples`, so `dbwiki es trace` can still reach
    the file."""
    total_budget = tl.get("max_total_chars", 6000)
    per_path = tl.get("max_excerpt_chars", 1500)
    out: list[dict] = []
    used = 0
    for path, meta in paths_meta.items():
        docs = fetch_trace_docs(es, tl, path, t0, t1)
        if not docs:
            continue
        room = min(per_path, max(0, total_budget - used))
        excerpt, truncated = select_excerpt(docs, meta["codes"], room)
        used += len(excerpt)
        out.append({
            "path": path,
            "mentions": meta["mentions"],
            "rules": list(meta["rules"]),
            "docs_found": len(docs),
            "es_samples": [{"index": d["index"], "id": d["id"], "ts": d["ts"]}
                           for d in docs],
            "excerpt": excerpt,
            "truncated": truncated,
        })
    return out
