"""Daily DBA summary: one deterministic HTML page per day, over all databases.

The operator reads one page a day. It answers three questions in order —
*what needs attention*, *what is worth a look*, *what merely happened* — and
every entry links back into the wiki (journal, digest, incident, fleet
report) so the page is an index, never a replacement for the evidence.

Everything here is a pure function of files on disk plus (optionally) the
ingest ledger: same inputs -> identical bytes. No model call, no clock, no
network. The pages carry no timestamp for exactly that reason.

Wiki pages are markdown, so a `file://` link would show raw text; links are
therefore rendered against `report.link_base` (a GitHub blob URL by default),
and only for pages that actually exist on disk — a missing page degrades to
plain text rather than a dead link.
"""

import html
import json
import re
from pathlib import Path

from . import evidence_ref
from .changes import Change, of_digest
from .deeplink import LinkState, Resolver
from .incidents import Incident, Status, load_incidents
from .monitoring import read_facts

DEFAULT_LINK_BASE = "https://github.com/<you>/<wiki-repo>/blob/main"

#: canonical column order for per-source counts; anything else sorts after
SOURCE_ORDER = ("alert", "listener", "dataguard")

_DAY_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

SILENCE_NOTE = ("no events collected — telemetry, NOT database state")


def _e(value) -> str:
    """Escape anything interpolated into the page. Digest rules, ledger
    summaries and incident titles are model- or log-derived text and may carry
    angle brackets or quotes."""
    return html.escape(str(value), quote=True)


def _n(value) -> str:
    return f"{int(value):,}"


def _sorted_sources(names) -> list[str]:
    return sorted(names, key=lambda s: (SOURCE_ORDER.index(s)
                                        if s in SOURCE_ORDER else len(SOURCE_ORDER),
                                        s))


def day_digests(wiki_root: Path, day: str) -> dict[str, dict]:
    """`{db: digest}` for every database with a digest on `day`, db-sorted.

    The compactor writes a digest for every discovered database each run, not
    only for the ones an agent woke up for, so this is the fleet roster for
    the day. An unreadable digest is skipped rather than fatal: one corrupt
    file must not cost the operator the whole page."""
    root = Path(wiki_root)
    out: dict[str, dict] = {}
    for jp in sorted((root / "digests").glob(f"*/{day}.json")):
        try:
            digest = json.loads(jp.read_text())
        except (OSError, ValueError):
            continue
        out[jp.parent.name] = digest
    return dict(sorted(out.items()))


def ledger_summaries(state, day: str) -> dict[str, dict]:
    """`{db: {"summary": ..., "notable": ...}}` from the ingest ledger for
    `day`. `state=None` (or a ledger read that fails) simply yields nothing —
    the page then falls back to deterministic headlines."""
    if state is None:
        return {}
    try:
        ledger = state.get_ledger()
    except Exception:  # noqa: BLE001 — a summary is a nicety, never a blocker
        return {}
    out: dict[str, dict] = {}
    for key, entry in ledger.items():
        parts = Path(key).parts
        if len(parts) != 3 or parts[0] != "digests" or Path(key).stem != day:
            continue
        if entry.get("status") != "ingested" or not entry.get("summary"):
            continue
        out[parts[1]] = {"summary": entry.get("summary"),
                         "notable": entry.get("notable")}
    return out


def report_rel(wiki_root: Path, day: str) -> str | None:
    """The day's fleet report page: the consolidated `reports/<day>.md` if it
    exists, else the latest intra-day `reports/<day>-HHMM.md`. Its prose is
    never parsed — the page only links to it."""
    root = Path(wiki_root)
    consolidated = root / "reports" / f"{day}.md"
    if consolidated.exists():
        return f"reports/{day}.md"
    partials = sorted((root / "reports").glob(f"{day}-[0-9][0-9][0-9][0-9].md"))
    return f"reports/{partials[-1].name}" if partials else None


def resolved_day(incident: Incident) -> str:
    """The day (YYYY-MM-DD) a resolved incident was last resolved, or `""`
    when the page does not say.

    The `at` of the `resolve`-shaped record that last moved the page into
    `resolved` — later records on a resolved page (a note, a merge) move
    `updated` but not the day it was closed. A page resolved by hand or by
    an agent carries no such record, and its `updated` is the best date
    there is."""
    at = ""
    before = None
    for record in sorted(incident.actions.records, key=lambda r: r.at):
        after = record.status_after
        if after is Status.RESOLVED and before is not Status.RESOLVED:
            at = record.at
        before = after
    day = (at or incident.updated)[:10]
    return day if _DAY_RE.match(day) else ""


def open_incidents(wiki_root: Path, day: str) -> list[Incident]:
    """Incidents that were open on `day`, in stable path order.

    Unresolved incidents are standing attention items: they appear on every
    day's page until somebody resolves them. An incident opened *after* the
    rendered day is left off, so backfilling an old page cannot show an
    incident that did not exist yet; an incident with no readable `opened`
    date is shown (fail loud, not silent).

    A resolved incident still belongs to the days before `resolved_day`: the
    page for a past day, and that day's count on the index, must not change
    because the incident was closed later. One with no readable resolution
    date stays off, as every resolved incident did before."""
    return [i for i in load_incidents(Path(wiki_root))
            if i.existed_on(day)
            and (i.is_active or resolved_day(i) > day)]


def available_days(wiki_root: Path, extra: str | None = None) -> list[str]:
    """Days that have (or are about to have) a rendered page, oldest first."""
    days = {p.stem for p in (Path(wiki_root) / "html").glob("*.html")
            if _DAY_RE.match(p.stem)}
    if extra:
        days.add(extra)
    return sorted(days)


def notable_groups(digest: dict) -> list[tuple[str, str, int]]:
    """`(source, rule, count)` for every notable group, biggest first."""
    groups = [(name, g.get("rule", "?"), int(g.get("count", 0)))
              for name, s in (digest.get("sources") or {}).items()
              for g in (s.get("notable") or [])]
    return sorted(groups, key=lambda t: (-t[2], t[0], t[1]))


def digest_headline(digest: dict) -> str:
    """A deterministic one-liner for a digest, used when the ledger has no
    model-written summary (a db whose digest was never ingested, or a page
    rendered without state)."""
    groups = notable_groups(digest)
    if groups:
        top = groups[:3]
        sources = {g[0] for g in top}
        if len(sources) == 1:
            body = ", ".join(f"{_n(c)}× {r}" for _, r, c in top)
            return f"{body} ({top[0][0]})"
        return ", ".join(f"{_n(c)}× {r} ({s})" for s, r, c in top)
    silent = [d.get("source", "?") for d in digest.get("deltas") or []
              if d.get("type") == "silence"]
    if silent:
        return f"{SILENCE_NOTE} ({', '.join(_sorted_sources(silent))})"
    deltas = digest.get("deltas") or []
    if deltas:
        kinds = sorted({d.get("type", "?") for d in deltas})
        return (f"no notable events; {len(deltas)} change"
                f"{'' if len(deltas) == 1 else 's'} of interest: "
                f"{', '.join(k.replace('_', ' ') for k in kinds)}")
    events = int((digest.get("totals") or {}).get("events", 0))
    return f"{_n(events)} events, nothing notable"


def delta_lines(digest: dict) -> list[str]:
    """One human line per delta, grouped by type so a day with 12 first-ever
    codes reads as one line rather than twelve."""
    by_type: dict[str, list[dict]] = {}
    for d in digest.get("deltas") or []:
        by_type.setdefault(str(d.get("type", "?")), []).append(d)
    lines: list[str] = []
    for kind in sorted(by_type):
        items = by_type[kind]
        if kind == "silence":
            srcs = _sorted_sources(str(i.get("source", "?")) for i in items)
            lines.append(f"{SILENCE_NOTE}: no {', '.join(srcs)} events in the window")
        elif kind in ("first_ever_code", "new_service", "new_client_program"):
            label = {"first_ever_code": "first-ever error code",
                     "new_service": "new listener service",
                     "new_client_program": "new client program"}[kind]
            values = sorted({str(i.get("value", "?")) for i in items})
            shown = ", ".join(values[:6])
            more = f" (+{len(values) - 6} more)" if len(values) > 6 else ""
            plural = "" if len(values) == 1 else "s"
            lines.append(f"{label}{plural}: {shown}{more}")
        elif kind == "after_change":
            for i in items:
                codes = ", ".join(i.get("codes") or []) or str(i.get("rule", "?"))
                gap = int(i.get("gap_s", 0))
                span = (f"{gap // 60} min" if gap < 7200
                        else f"{gap / 3600:.1f} h")
                lines.append(f"after a change: {codes} {span} after "
                             f"{i.get('change_rule', '?')}")
        elif kind == "rate_anomaly":
            for i in sorted(items, key=lambda x: (str(x.get("source")),
                                                  str(x.get("counter")))):
                lines.append(
                    f"rate anomaly: {i.get('counter')} ({i.get('source')}) "
                    f"{_n(i.get('count', 0))} in {i.get('window_hours')}h — "
                    f"{i.get('rate_per_hour')}/h vs baseline median "
                    f"{i.get('baseline_median_per_hour')}/h")
        else:
            lines.append(f"{kind.replace('_', ' ')}: {len(items)}")
    return lines


#: How many change rows the page's changes strip draws before it points at
#: the digests. One instance restart alone fires eight to eleven.
MAX_CHANGE_ROWS = 25


def change_rows(digests: dict[str, dict]) -> list[tuple[str, Change]]:
    """`(db, change)` for every change on every database, as one fleet
    timeline. `of_digest` is the same reader the markdown `## Changes` section
    uses, so a digest written before the `changes` key still shows its
    lifecycle groups here."""
    rows = [(db, c) for db, d in digests.items() for c in of_digest(d)]
    return sorted(rows, key=lambda r: (r[1].ts, r[0], r[1].rule, r[1].message))


def _clock(ts: str) -> str:
    """`HH:MM:SS` of an ISO stamp. The page is one day, so the date would only
    pad the row — and a page carries no full timestamp of its own."""
    clock = ts.split("T", 1)[-1][:8]
    return clock if re.fullmatch(r"\d{2}:\d{2}:\d{2}", clock) else ts


def source_counts(digest: dict) -> list[tuple[str, int]]:
    return [(name, int((digest.get("sources") or {}).get(name, {})
                       .get("total_events", 0)))
            for name in _sorted_sources((digest.get("sources") or {}))]


def top_counter(digest: dict) -> str:
    """The biggest routine counter across sources — what this db spent the
    day doing when nothing happened."""
    best: tuple[int, str, str] | None = None
    for name, s in (digest.get("sources") or {}).items():
        for counter, count in (s.get("routine_counters") or {}).items():
            cand = (int(count), name, str(counter))
            if best is None or (-cand[0], cand[1], cand[2]) < (-best[0], best[1], best[2]):
                best = cand
    return f"{best[2]} ×{_n(best[0])}" if best else "—"


def tier_of(digest: dict) -> str:
    """`attention` (the digest flagged the day notable), `watch` (not notable
    but something changed) or `routine`. Open incidents are added to the
    attention tier separately — they are not a property of one day's digest."""
    if digest.get("notable"):
        return "attention"
    return "watch" if digest.get("deltas") else "routine"


def tiers(digests: dict[str, dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"attention": [], "watch": [], "routine": []}
    for db, digest in digests.items():
        out[tier_of(digest)].append(db)
    return out


CSS = """
:root {
  color-scheme: light dark;
  --bg: #f6f6f4;
  --surface: #ffffff;
  --surface-2: #fbfbf9;
  --text: #1b1b19;
  --muted: #6a6a63;
  --line: #e2e2db;
  --accent: #2a5db0;
  --red: #a72118;      --red-bg: #fdecea;  --red-line: #d9564b;
  --amber: #7d5300;    --amber-bg: #fdf3e2; --amber-line: #d69a2a;
  --green: #1d6340;    --green-bg: #eaf4ee; --green-line: #4f9a72;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
          "Liberation Mono", monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16171a;
    --surface: #1e2024;
    --surface-2: #23262b;
    --text: #e6e6e2;
    --muted: #9a9a93;
    --line: #33363c;
    --accent: #8ab4f8;
    --red: #ff9c92;      --red-bg: #3a1f1c;  --red-line: #d9564b;
    --amber: #f0c377;    --amber-bg: #352915; --amber-line: #d69a2a;
    --green: #8fd5ac;    --green-bg: #1a2c22; --green-line: #4f9a72;
  }
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  padding: 2rem 1.15rem 4rem;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, sans-serif;
  font-size: 16px;
  line-height: 1.55;
}
main { max-width: 900px; margin: 0 auto; }
a { color: var(--accent); text-decoration-thickness: 1px;
    text-underline-offset: 2px; }
a:hover { text-decoration-style: solid; }
code, .num, .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }

/* ---- header ---- */
header.page { border-bottom: 1px solid var(--line); padding-bottom: 1.1rem;
              margin-bottom: 1.75rem; }
header.page .eyebrow { font-size: .78rem; letter-spacing: .1em;
  text-transform: uppercase; color: var(--muted); margin: 0 0 .2rem; }
header.page h1 { margin: 0; font-size: 1.9rem; line-height: 1.15;
  letter-spacing: -.015em; font-family: var(--mono); font-weight: 650; }
.chips { display: flex; flex-wrap: wrap; gap: .45rem; margin: .9rem 0 0;
         padding: 0; list-style: none; }
.chip { display: inline-flex; align-items: baseline; gap: .4rem;
  border: 1px solid var(--line); background: var(--surface);
  border-radius: 999px; padding: .18rem .7rem; font-size: .82rem;
  color: var(--muted); }
.chip b { font-family: var(--mono); font-variant-numeric: tabular-nums;
          font-size: .95rem; color: var(--text); font-weight: 650; }
.chip.alarm { border-color: var(--red-line); color: var(--red); }
.chip.alarm b { color: var(--red); }
nav.days { display: flex; flex-wrap: wrap; gap: .9rem; align-items: baseline;
  margin-top: .9rem; font-size: .87rem; }
nav.days .spacer { flex: 1 1 auto; }
nav.days .disabled { color: var(--muted); }

/* ---- tier sections ---- */
section.tier { margin: 0 0 2.25rem; }
section.tier > h2 { display: flex; align-items: baseline; gap: .6rem;
  font-size: 1.08rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
section.tier > h2 .count { font-family: var(--mono); font-size: .85rem;
  color: var(--muted); font-weight: 500; }
section.tier > p.blurb { margin: 0 0 .85rem; color: var(--muted);
  font-size: .85rem; }
.rule { height: 3px; border-radius: 2px; margin: 0 0 .95rem; }
.t-attention .rule { background: var(--red-line); }
.t-watch .rule { background: var(--amber-line); }
.t-routine .rule { background: var(--green-line); }
.t-changes .rule { background: var(--accent); }

/* ---- changes strip ---- */
ul.changes { list-style: none; margin: 0; padding: 0; font-size: .87rem; }
ul.changes li { display: flex; flex-wrap: wrap; align-items: baseline;
  gap: .15rem .6rem; padding: .3rem .2rem; border-bottom: 1px solid var(--line); }
ul.changes li:last-child { border-bottom: 0; }
ul.changes .num { color: var(--muted); }
ul.changes .db { font-family: var(--mono); font-weight: 650; }
ul.changes .what { font-family: var(--mono); overflow-wrap: anywhere; }
ul.changes li.more { color: var(--muted); }

/* ---- cards ---- */
.card { background: var(--surface); border: 1px solid var(--line);
  border-left: 5px solid var(--line); border-radius: 6px;
  padding: .85rem 1rem; margin: 0 0 .7rem; }
.card.sev-attention { border-left-color: var(--red-line); }
.card.sev-watch { border-left-color: var(--amber-line); }
.card-head { display: flex; flex-wrap: wrap; align-items: baseline;
  gap: .55rem; }
.card-head h3 { margin: 0; font-family: var(--mono); font-size: 1.02rem;
  font-weight: 650; letter-spacing: -.01em; }
.badge { font-size: .68rem; letter-spacing: .07em; text-transform: uppercase;
  font-weight: 700; border-radius: 3px; padding: .1rem .42rem;
  border: 1px solid currentColor; }
.badge.b-attention { color: var(--red); background: var(--red-bg); }
.badge.b-watch { color: var(--amber); background: var(--amber-bg); }
.badge.b-routine { color: var(--green); background: var(--green-bg); }
.headline { margin: .45rem 0 0; }
.meta { margin: .35rem 0 0; color: var(--muted); font-size: .85rem; }
.meta .num { color: var(--text); }
ul.deltas { margin: .5rem 0 0; padding-left: 1.1rem; }
ul.deltas li { margin: .12rem 0; }
ul.deltas li.silence { color: var(--amber); }
.links { margin: .55rem 0 0; font-size: .85rem; display: flex;
  flex-wrap: wrap; gap: .1rem .55rem; }
.links .sep { color: var(--line); }
.nolink { color: var(--muted); text-decoration: line-through
          var(--line) 1px; }
.empty { margin: 0; padding: .7rem 1rem; border: 1px dashed var(--line);
  border-radius: 6px; color: var(--muted); background: var(--surface-2); }

/* ---- routine table ---- */
details.routine { border: 1px solid var(--line); border-left: 5px solid
  var(--green-line); border-radius: 6px; background: var(--surface); }
details.routine > summary { cursor: pointer; padding: .7rem 1rem;
  font-size: .92rem; }
details.routine > summary::marker { color: var(--muted); }
details.routine[open] > summary { border-bottom: 1px solid var(--line); }
.tablewrap { overflow-x: auto; }
table.fleet { border-collapse: collapse; width: 100%; font-size: .87rem; }
table.fleet th, table.fleet td { text-align: left; padding: .38rem .75rem;
  border-bottom: 1px solid var(--line); white-space: nowrap; }
table.fleet thead th { font-size: .72rem; letter-spacing: .06em;
  text-transform: uppercase; color: var(--muted); font-weight: 600; }
table.fleet tbody tr:last-child td { border-bottom: 0; }
table.fleet td.db { font-family: var(--mono); font-weight: 650; }
table.fleet td.num, table.fleet th.num { text-align: right;
  font-family: var(--mono); font-variant-numeric: tabular-nums; }
table.fleet td.counter { font-family: var(--mono); color: var(--muted);
  white-space: normal; }

/* ---- index ---- */
ul.days { list-style: none; margin: 0; padding: 0; }
ul.days li { display: flex; flex-wrap: wrap; align-items: baseline;
  gap: .5rem .9rem; padding: .6rem .2rem; border-bottom: 1px solid var(--line); }
ul.days li a { font-family: var(--mono); font-size: 1.02rem; font-weight: 650; }
ul.days li .stat { font-size: .84rem; color: var(--muted); }
ul.days li .stat b { font-family: var(--mono); color: var(--text);
  font-weight: 650; }
ul.days li .stat.alarm b, ul.days li .stat.alarm { color: var(--red); }

footer.page { margin-top: 2.5rem; padding-top: 1rem;
  border-top: 1px solid var(--line); color: var(--muted); font-size: .8rem; }

@media print {
  body { background: #fff; padding: 0; }
  .card, details.routine { break-inside: avoid; }
  details.routine { border-left-width: 5px; }
  details.routine[open] > summary { display: none; }
}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)}</title>\n"
        f"<style>{CSS}</style>\n"
        "</head>\n<body>\n<main>\n"
        f"{body}"
        "</main>\n</body>\n</html>\n"
    )


class _Links:
    """Renders wiki links against `link_base`, but only for pages that exist
    on disk; everything else degrades to plain (struck-through) text."""

    def __init__(self, wiki_root: Path, link_base: str):
        self.root = Path(wiki_root)
        self.base = (link_base or DEFAULT_LINK_BASE).rstrip("/")

    def md(self, rel_md: str | None, text: str) -> str:
        if rel_md and (self.root / rel_md).exists():
            return f'<a href="{_e(f"{self.base}/{rel_md}")}">{_e(text)}</a>'
        return f'<span class="nolink">{_e(text)}</span>'

    def row(self, parts: list[str]) -> str:
        if not parts:
            return ""
        joined = '<span class="sep">·</span>'.join(parts)
        return f'<p class="links">{joined}</p>'


def _db_links(wikilinks: _Links, db: str, day: str, report: str | None,
              incidents: list[str] = (), deep: list[str] = ()) -> str:
    parts = [wikilinks.md(f"databases/{db}/journal/{day[:7]}.md",
                          f"journal {day[:7]}"),
             wikilinks.md(f"digests/{db}/{day}.md", "digest")]
    for rel in incidents:
        parts.append(wikilinks.md(rel, Path(rel).stem))
    if report:
        parts.append(wikilinks.md(report, "full narrative report"))
    return wikilinks.row(parts + list(deep))


#: How many of a card's notable groups get a deep link.
MAX_DEEP_LINKS = 5


def _deep_links(resolver: Resolver | None, db: str, day: str,
                digest: dict) -> list[str]:
    """An anchor for each of the day's biggest notable groups whose bounded
    filter this deployment can actually open.

    Available links only. The daily page is generated once and read later, so
    a badge saying "expired" would be a claim about the moment of rendering
    rather than the moment of reading; the portal, which answers per request,
    is where the other states belong.

    Each anchor names its signature, because a card with eighteen notable
    groups would otherwise carry eighteen identically labelled links and the
    operator could not tell which one opens what. `MAX_DEEP_LINKS` is why the
    sort is by count: a live storm digest carries dozens of groups, and a link
    row longer than the card it sits under is not a drilldown, it is a wall.
    The digest itself is one link away and holds every group."""
    if resolver is None:
        return []
    groups = sorted(((g, int(g.get("count") or 0))
                     for s in (digest.get("sources") or {}).values()
                     for g in (s.get("notable") or [])),
                    key=lambda pair: -pair[1])
    out = []
    for group, _count in groups[:MAX_DEEP_LINKS]:
        ref = evidence_ref.from_digest_group(db, day, group)
        link = resolver.logs(ref)
        if link.state is LinkState.AVAILABLE:
            text = f"{link.label} ({ref.signature.value})"
            out.append(f'<a href="{_e(link.url)}" '
                       f'title="{_e(link.description)}">{_e(text)}</a>')
    return out


def _plural(n: int, noun: str) -> str:
    return noun if n == 1 else noun + "s"


def _chip(label: str, value, alarm: bool = False) -> str:
    cls = "chip alarm" if alarm else "chip"
    return f'<li class="{cls}"><b>{_e(value)}</b> {_e(label)}</li>'


def _section(kind: str, heading: str, blurb: str, count: int, body: str) -> str:
    return (
        f'<section class="tier t-{kind}">\n'
        f'<h2>{_e(heading)} <span class="count">{count}</span></h2>\n'
        f'<p class="blurb">{_e(blurb)}</p>\n'
        f'<div class="rule"></div>\n'
        f"{body}"
        "</section>\n"
    )


def _empty(text: str) -> str:
    return f'<p class="empty">{_e(text)}</p>\n'


def _db_card(kind: str, badge: str, db: str, headline: str, digest: dict,
             links_html: str, deltas: list[str] = ()) -> str:
    counts = ", ".join(f'{_e(name)} <span class="num">{_n(c)}</span>'
                       for name, c in source_counts(digest))
    totals = digest.get("totals") or {}
    meta = (f'{counts} — <span class="num">{_n(totals.get("events", 0))}</span> '
            f'events, <span class="num">{_n(totals.get("notable_groups", 0))}</span> '
            f"notable groups")
    delta_html = ""
    if deltas:
        items = "".join(
            f'<li class="{"silence" if line.startswith(SILENCE_NOTE) else ""}">'
            f"{_e(line)}</li>" for line in deltas)
        delta_html = f'<ul class="deltas">{items}</ul>\n'
    return (
        f'<article class="card sev-{kind}">\n'
        f'<div class="card-head"><h3>{_e(db)}</h3>'
        f'<span class="badge b-{kind}">{_e(badge)}</span></div>\n'
        f'<p class="headline">{_e(headline)}</p>\n'
        f'<p class="meta">{meta}</p>\n'
        f"{delta_html}{links_html}"
        "</article>\n"
    )


def _changes_strip(wikilinks: _Links, digests: dict[str, dict],
                   day: str) -> str:
    """What the operators did today, across the fleet, above the tiers.

    Empty when nothing changed, so a quiet day's page is what it always was.
    Past `MAX_CHANGE_ROWS` the strip says how many it left out rather than
    growing: every row is also in its database's digest, one link away."""
    rows = change_rows(digests)
    if not rows:
        return ""
    items = []
    for db, c in rows[:MAX_CHANGE_ROWS]:
        count = f" ×{_n(c.count)}" if c.count > 1 else ""
        items.append(
            f'<li><span class="num">{_e(_clock(c.ts))}</span>'
            f'<span class="db">{wikilinks.md(f"digests/{db}/{day}.md", db)}</span>'
            f"<span>{_e(c.rule)}{count}</span>"
            f'<span class="what">{_e(c.message)}</span></li>')
    left = len(rows) - MAX_CHANGE_ROWS
    if left > 0:
        items.append(f'<li class="more">+{left} more — each database\'s '
                     "digest lists them all</li>")
    return _section(
        "changes", "Changes",
        "Administrative acts the logs recorded: parameter changes, restarts, "
        "datafile and redo DDL. Read them before the errors below. Times UTC.",
        len(rows), f'<ul class="changes">\n{chr(10).join(items)}\n</ul>\n')


def _monitoring_meta(incident: Incident, state_dir: Path | None) -> str:
    """One line saying what the tick's last deterministic evaluation made of
    this incident's recovery signal, plus the first reason it is not met.

    Empty for anything but a monitoring incident, for a page rendered without
    a state dir, and for a window no tick has evaluated yet: the facts live in
    `.state/`, which is machine output the wiki never carries."""
    if incident.status is not Status.MONITORING or state_dir is None:
        return ""
    facts = read_facts(state_dir, incident.slug)
    if not facts:
        return ""
    signal = facts.get("signal") or {}
    payload = next((str(v) for k, v in signal.items() if k != "kind"), "")
    until = (facts.get("window") or {}).get("until", "?")
    line = (f"monitoring: {signal.get('kind', '?')} {payload} until {until} "
            f"— {facts.get('verdict', '?')}")
    first = next(iter(facts.get("contradictions") or []), "")
    if first:
        line += f" ({first})"
    return f'<p class="meta">{_e(line)}</p>\n'


def _incident_card(wikilinks: _Links, incident: Incident, day: str,
                   report: str | None, state_dir: Path | None) -> str:
    who = incident.db or "unknown db"
    parts = [wikilinks.md(incident.path, "incident page")]
    if incident.db:
        parts.append(
            wikilinks.md(f"databases/{incident.db}/journal/{day[:7]}.md",
                         f"journal {day[:7]}"))
    if report:
        parts.append(wikilinks.md(report, "full narrative report"))
    return (
        '<article class="card sev-attention">\n'
        f'<div class="card-head"><h3>{_e(who)}</h3>'
        f'<span class="badge b-attention">{_e(incident.status.label)}</span>'
        f'<span class="meta">status: {_e(incident.status)}</span></div>\n'
        f'<p class="headline">{_e(incident.title)}</p>\n'
        f"{_monitoring_meta(incident, state_dir)}"
        f"{wikilinks.row(parts)}"
        "</article>\n"
    )


def _routine_table(wikilinks: _Links, dbs: list[str],
                   digests: dict[str, dict], day: str) -> str:
    cols = _sorted_sources({name for db in dbs
                            for name in (digests[db].get("sources") or {})})
    head = "".join(f'<th class="num">{_e(c)}</th>' for c in cols)
    rows = []
    for db in dbs:
        d = digests[db]
        by = dict(source_counts(d))
        cells = "".join(f'<td class="num">{_n(by.get(c, 0))}</td>' for c in cols)
        rows.append(
            f'<tr><td class="db">{_e(db)}</td>{cells}'
            f'<td class="num">{_n((d.get("totals") or {}).get("events", 0))}</td>'
            f'<td class="counter">{_e(top_counter(d))}</td>'
            f'<td>{wikilinks.md(f"digests/{db}/{day}.md", "digest")}</td>'
            "</tr>")
    return (
        '<details class="routine">\n'
        f"<summary>{len(dbs)} database{'' if len(dbs) == 1 else 's'} with "
        "nothing new — counts only</summary>\n"
        '<div class="tablewrap">\n<table class="fleet">\n'
        f'<thead><tr><th>db</th>{head}<th class="num">total</th>'
        "<th>top routine counter</th><th>links</th></tr></thead>\n"
        f"<tbody>\n{chr(10).join(rows)}\n</tbody>\n</table>\n</div>\n</details>\n"
    )


#: The one `<nav>` `_nav` draws, newline included, so a neighbour refresh can
#: swap it without rendering the rest of the page again.
_NAV_RE = re.compile(r'<nav class="days">.*?</nav>\n')


def _nav(day: str, days: list[str]) -> str:
    i = days.index(day) if day in days else -1
    prev_day = days[i - 1] if i > 0 else None
    next_day = days[i + 1] if 0 <= i < len(days) - 1 else None
    left = (f'<a href="{_e(prev_day)}.html">&#8249; {_e(prev_day)}</a>'
            if prev_day else '<span class="disabled">&#8249; earlier</span>')
    right = (f'<a href="{_e(next_day)}.html">{_e(next_day)} &#8250;</a>'
             if next_day else '<span class="disabled">later &#8250;</span>')
    return (f'<nav class="days">{left}'
            f'<a href="index.html">all days</a>'
            f'<span class="spacer"></span>{right}</nav>\n')


def render_daily(wiki_root: Path, day: str, *, state=None,
                 link_base: str = DEFAULT_LINK_BASE,
                 links: Resolver | None = None) -> str:
    """The day's summary page as a self-contained HTML string.

    `state` is an optional `StateStore`: its ingest-ledger entries supply the
    model-written one-liner for databases that were ingested, and its
    directory supplies the monitoring verdicts. Without it (or for a db that
    was never ingested) the headline is derived from the digest itself, so the
    page renders from the wiki alone.

    `links` is an optional `deeplink.Resolver`. Without one, or for a
    reference this deployment cannot map, the page renders exactly what it
    rendered before: the summary and the wiki links, and no URL that would be
    unsafe or broken. The page stays a pure function of disk plus config, so
    only *available* links are drawn and no state badge is invented here."""
    root = Path(wiki_root)
    digests = day_digests(root, day)
    summaries = ledger_summaries(state, day)
    state_dir = getattr(state, "dir", None)
    incidents = open_incidents(root, day)
    report = report_rel(root, day)
    wikilinks = _Links(root, link_base)
    by_tier = tiers(digests)
    inc_by_db: dict[str, list[str]] = {}
    for i in incidents:
        inc_by_db.setdefault(i.db, []).append(i.path)

    total_events = sum(int((d.get("totals") or {}).get("events", 0))
                       for d in digests.values())
    chips = "".join([
        _chip(_plural(len(digests), "database"), len(digests)),
        _chip(_plural(total_events, "event"), _n(total_events)),
        _chip("notable", len(by_tier["attention"]),
              alarm=bool(by_tier["attention"])),
        _chip(_plural(len(incidents), "open incident"), len(incidents),
              alarm=bool(incidents)),
    ])
    report_link = (f'<p class="meta">'
                   f'{wikilinks.md(report, "full narrative report")}'
                   "</p>\n" if report else
                   '<p class="meta"><span class="nolink">no fleet report for '
                   "this day</span></p>\n")
    header = (
        '<header class="page">\n'
        '<p class="eyebrow">Oracle fleet — daily summary</p>\n'
        f"<h1>{_e(day)}</h1>\n"
        f'<ul class="chips">{chips}</ul>\n'
        f"{report_link}"
        f"{_nav(day, available_days(root, day))}"
        "</header>\n"
    )

    # tier 1 — needs attention: notable digests, then every open incident
    body = []
    for db in by_tier["attention"]:
        d = digests[db]
        headline = (summaries.get(db) or {}).get("summary") or digest_headline(d)
        body.append(_db_card("attention", "Needs attention", db, headline, d,
                             _db_links(wikilinks, db, day, report,
                                       inc_by_db.get(db, []),
                                       _deep_links(links, db, day, d)),
                             delta_lines(d)))
    for i in incidents:
        body.append(_incident_card(wikilinks, i, day, report, state_dir))
    attention = _section(
        "attention", "Needs attention",
        "Databases whose day was flagged notable, and every incident that is "
        "still open.",
        len(by_tier["attention"]) + len(incidents),
        "".join(body) or _empty("Nothing needs attention today."))

    # tier 2 — worth a look: not notable, but something changed
    body = []
    for db in by_tier["watch"]:
        d = digests[db]
        headline = (summaries.get(db) or {}).get("summary") or digest_headline(d)
        body.append(_db_card("watch", "Worth a look", db, headline, d,
                             _db_links(wikilinks, db, day, report,
                                       inc_by_db.get(db, [])),
                             delta_lines(d)))
    watch = _section(
        "watch", "Worth a look",
        "Nothing notable, but something was seen for the first time or moved "
        "out of its usual range.",
        len(by_tier["watch"]),
        "".join(body) or _empty("Nothing new to look at today."))

    routine = _section(
        "routine", "Routine",
        "Normal activity. Counts only — open if you want the numbers.",
        len(by_tier["routine"]),
        _routine_table(wikilinks, by_tier["routine"], digests, day)
        if by_tier["routine"] else _empty("No databases reported a routine day."))

    footer = ('<footer class="page">Generated deterministically from the wiki '
              "digests, the ingest ledger and the open incidents. Absence of "
              "events is a telemetry fact, never evidence of recovery.</footer>\n")
    changed = _changes_strip(wikilinks, digests, day)
    return _page(f"Fleet daily summary — {day}",
                 header + changed + attention + watch + routine + footer)


def day_stats(wiki_root: Path, day: str) -> dict:
    """Mini-stats for one day, as the index shows them."""
    digests = day_digests(wiki_root, day)
    return {
        "day": day,
        "dbs": len(digests),
        "notable": sum(1 for d in digests.values() if d.get("notable")),
        "incidents": len(open_incidents(wiki_root, day)),
    }


def render_index(wiki_root: Path, *, days: list[str] | None = None) -> str:
    """The newest-first catalog of rendered days. Regenerated on every render
    so a new day never needs a separate step."""
    root = Path(wiki_root)
    days = sorted(set(days if days is not None else available_days(root)),
                  reverse=True)
    rows = []
    for day in days:
        s = day_stats(root, day)
        stats = (f'<span class="stat"><b>{_n(s["dbs"])}</b> '
                 f'{_plural(s["dbs"], "database")}</span>'
                 f'<span class="stat{" alarm" if s["notable"] else ""}">'
                 f'<b>{_n(s["notable"])}</b> notable</span>'
                 f'<span class="stat{" alarm" if s["incidents"] else ""}">'
                 f'<b>{_n(s["incidents"])}</b> '
                 f'{_plural(s["incidents"], "open incident")}</span>')
        rows.append(f'<li><a href="{_e(day)}.html">{_e(day)}</a>{stats}</li>')
    body = (f'<ul class="days">\n{chr(10).join(rows)}\n</ul>\n' if rows
            else _empty("No daily summaries have been rendered yet."))
    header = ('<header class="page">\n'
              '<p class="eyebrow">Oracle fleet</p>\n'
              "<h1>Daily summaries</h1>\n"
              f'<ul class="chips">{_chip("days", len(days))}</ul>\n'
              "</header>\n")
    footer = ('<footer class="page">One page per day, newest first. Each page '
              "tiers the fleet by what needs attention.</footer>\n")
    return _page("Fleet daily summaries", header + body + footer)


def write_daily(wiki_root: Path, day: str, *, state=None,
                link_base: str = DEFAULT_LINK_BASE,
                links: Resolver | None = None) -> list[Path]:
    """Render `day`, refresh its neighbours' navigation, and regenerate the
    index. Returns every path written, `html/<day>.html` first.

    The neighbour refresh is what keeps prev/next honest: yesterday's page was
    rendered when today did not exist yet, so its "later" link would stay dead
    forever unless the new day rewrites it. It rewrites the `<nav>` and
    nothing else, so a past page keeps the day it describes; only a page
    whose nav cannot be found is rendered again whole."""
    root = Path(wiki_root)
    out_dir = root / "html"
    out_dir.mkdir(parents=True, exist_ok=True)

    def _write(d: str) -> Path:
        p = out_dir / f"{d}.html"
        p.write_text(render_daily(root, d, state=state, link_base=link_base,
                                  links=links))
        return p

    def _renav(d: str, days: list[str]) -> Path:
        p = out_dir / f"{d}.html"
        text = p.read_text()
        navs = _NAV_RE.findall(text)
        if len(navs) != 1:
            return _write(d)
        p.write_text(_NAV_RE.sub(lambda _: _nav(d, days), text))
        return p

    written = [_write(day)]
    days = available_days(root, day)
    i = days.index(day)
    for j in (i - 1, i + 1):
        if 0 <= j < len(days) and (out_dir / f"{days[j]}.html").exists():
            written.append(_renav(days[j], days))
    index = out_dir / "index.html"
    index.write_text(render_index(root))
    return [*written, index]
