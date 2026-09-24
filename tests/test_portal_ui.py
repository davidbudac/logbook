"""The one workbench page, proved twice: statically, and in a real browser.

The static half is `ui.page()` and `ui.check_page`. It is pure Python, it
runs anywhere, and it is what CI leans on: the page is deterministic, it
carries both stylesheets, it loads nothing external, it holds no inline
handler, its tags balance, and every endpoint its script names is a route
`server.ROUTES` serves. `check_page` is exercised against three separately
damaged copies of the page, because a checker that only ever returns `[]`
proves nothing.

The browser half is not in CI's critical path. It launches
`/usr/bin/google-chrome` headless over the DevTools protocol and is skipped
where that binary is absent. Chrome cannot reach a loopback `http.server`
in this environment, though a python client on the same host reads that same
server fine, so this is the browser's sandbox and not the server; the page is
written into `tmp_path` and opened as a `file://` URL with `window.fetch`
replaced by a shim over the JSON shapes `portal/wire.py` documents. What the
walk proves is the page's own behaviour: the state table, the form's
refusals, the diff it renders, the advisory panel's laziness and poll ladder,
and where each server refusal leaves the operator. What it cannot prove is
that the server ever sends those shapes, or that a browser can reach it.

Two of the advisory walks wait out a real timer rather than faking the clock:
what is being proved is that the ladder stops, and a stopped timer is only
visible as a request that never arrives.
"""

import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

import pytest

from dbwiki import (advisory, daily_html, delivery, events, heat,
                    incident_action, incidents, readmodel, review)
from dbwiki.portal import api, ui

CHROME = (shutil.which("google-chrome") or shutil.which("chromium")
          or shutil.which("chromium-browser")
          or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
MARKER = "/*@CSS@*/"

needs_chrome = pytest.mark.skipif(
    not os.path.exists(CHROME), reason=f"the browser walk needs {CHROME}")


def test_page_is_deterministic_and_carries_both_stylesheets():
    html = ui.page()
    assert html == ui.page(), "page() renders identical bytes twice"
    assert MARKER not in html, f"page() substitutes the one {MARKER} marker"
    accent = next(line.strip() for line in daily_html.CSS.splitlines()
                  if line.strip().startswith("--accent:"))
    assert accent in html, "the page carries daily_html.CSS's --accent token"
    assert "[hidden] { display: none !important; }" in html, \
        "the page carries WORKBENCH_CSS's screen-visibility rule"


def test_the_page_carries_the_servers_quiet_threshold():
    html = ui.page()
    assert "/*@QUIET_DAYS@*/" not in html, \
        "page() substitutes the one /*@QUIET_DAYS@*/ marker"
    assert f"const QUIET_DAYS = {readmodel.QUIET_DAYS};" in html, \
        "the page's threshold is readmodel.QUIET_DAYS and never a second 14"


def test_the_page_carries_the_daily_pages_change_cap():
    html = ui.page()
    assert "/*@MAX_CHANGE_ROWS@*/" not in html
    assert f"const MAX_CHANGE_ROWS = {daily_html.MAX_CHANGE_ROWS};" in html, \
        "the strip stops where the daily page's strip stops"


def test_check_page_is_silent_about_the_page_as_served():
    assert ui.check_page(ui.page()) == []


#: One damaged copy per mistake the check exists to catch, and the substring
#: its problem has to name. Each edit is anchored on the document skeleton
#: rather than on a screen's markup, so rewriting a screen does not silently
#: turn one of these into a no-op.
DAMAGE = {
    "inline handler": (
        "<body>", '<body onclick="publish()">', "is an inline handler"),
    "external script": (
        "</head>", '<script src="https://cdn.example/x.js"></script></head>',
        "loads something external"),
    "dropped close tag": (
        "</main>", "", "closes <main>"),
}


@pytest.mark.parametrize("fault", sorted(DAMAGE))
def test_check_page_names_each_kind_of_damage(fault):
    old, new, names = DAMAGE[fault]
    html = ui.page()
    assert html.count(old) == 1, f"the {fault} edit has one site to damage"
    problems = ui.check_page(html.replace(old, new, 1))
    assert any(names in problem for problem in problems), \
        f"a {fault} is reported and names it; got {problems}"


def test_script_src_hashes_the_one_block_and_refuses_a_page_with_two(
        monkeypatch):
    """`ui.script_src` is what `server.HEADERS` puts on the wire, so the claim
    is made here without a socket: it is the digest of `page()`'s one inline
    block, and a page carrying a second block raises rather than serving a
    policy that names one of them."""
    ui.script_src.cache_clear()
    try:
        html = ui.page()
        block = re.search(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                          html, re.S)
        assert block, "the page carries an inline script block"
        digest = base64.b64encode(
            hashlib.sha256(block.group(1).encode()).digest()).decode()
        assert ui.script_src() == f"'sha256-{digest}'"

        ui.script_src.cache_clear()
        monkeypatch.setattr(
            ui, "page", lambda: html.replace(
                "</body>", "<script>void 0;</script></body>", 1))
        with pytest.raises(RuntimeError, match="exactly one inline"):
            ui.script_src()
    finally:
        ui.script_src.cache_clear()


def _js_literal(name):
    html = ui.page()
    hit = re.search(r"const %s = (\{[^;]*\});" % name, html)
    assert hit, f"workbench.html no longer declares {name}"
    quoted = re.sub(r"([{,]\s*)([A-Za-z_-]+)(\s*:)", r'\1"\2"\3', hit.group(1))
    return json.loads(quoted)


def test_the_confirm_table_mirrors_the_server_and_the_tool_verbs_hold():
    mirrored = {incident_action.VERB_OF[cmd]: field
                for cmd, field in api.CONFIRMATION.items()}
    assert _js_literal("CONFIRM_FIELD") == mirrored, \
        "CONFIRM_FIELD is a mirror of api.CONFIRMATION; change them together"
    assert set(_js_literal("TOOL_FIELD")) == {"record-action", "resolve"}, \
        "drafting stays off monitor: its summary would describe remediation" \
        " the packed material cannot yet show"
    landed = set(_js_literal("TOOL_FIELD").values())
    assert landed <= {spec.target_field for spec in advisory.TOOLS.values()}, \
        f"{landed} names a field no advisory row is written for, so the form " \
        f"would offer a button the manifest can never fill"


def test_every_status_an_advisory_run_can_report_has_its_own_glyph():
    """`advisory.RunStatus` is the vocabulary; `ADVISORY_STATUS` is the page's
    rule for drawing it with the colour off."""
    drawn = _js_literal("ADVISORY_STATUS")
    words = {str(status) for status in advisory.RunStatus}
    unruled = sorted(words - set(drawn))
    assert not unruled, \
        f"advisory.py reports {unruled}, which ADVISORY_STATUS gives no glyph"
    stale = sorted(set(drawn) - words)
    assert not stale, f"ADVISORY_STATUS draws {stale}, which no run reaches"
    glyphs = [row["mk"] for row in drawn.values()]
    assert all(glyph.strip() for glyph in glyphs), f"{glyphs} holds a blank"
    assert len(set(glyphs)) == len(glyphs), \
        f"two advisory statuses share a glyph and cannot be told apart: {glyphs}"
    assert all(row["badge"] for row in drawn.values()), \
        "every row names a badge class as well as a glyph"


def test_every_status_the_events_contract_can_emit_has_its_own_glyph():
    """`events.STATUSES` is the vocabulary; `STATUS_GLYPH` is the page's rule
    for drawing it with the colour off. A status on one side and not the other
    is a badge with no glyph, or a glyph for a word nothing sends."""
    glyphs = _js_literal("STATUS_GLYPH")
    unruled = sorted(set(events.STATUSES) - set(glyphs))
    assert not unruled, \
        f"events.py emits {unruled}, which STATUS_GLYPH gives no glyph: " \
        f"each would reach the page as a badge told apart by colour alone"
    stale = sorted(set(glyphs) - set(events.STATUSES))
    assert not stale, f"STATUS_GLYPH draws {stale}, which events.py cannot emit"
    blank = sorted(name for name, glyph in glyphs.items() if not glyph.strip())
    assert not blank, f"{blank} render as an empty glyph"
    shared = sorted(name for name, glyph in glyphs.items()
                    if list(glyphs.values()).count(glyph) > 1)
    assert not shared, f"{shared} share one glyph and cannot be told apart"


def test_every_incident_status_the_lifecycle_holds_has_its_own_glyph():
    """`incidents.Status` is the vocabulary; `INCIDENT_GLYPH` is the queue
    board's rule for drawing it with the colour off. The board leans on that
    colour three times over — a row's ribbon, a bar's fill, a marker on the
    timeline — so a status with no glyph is a status three drawings tell
    apart by hue alone."""
    glyphs = _js_literal("INCIDENT_GLYPH")
    words = {str(status) for status in incidents.Status}
    assert set(glyphs) == words, \
        f"INCIDENT_GLYPH draws {sorted(glyphs)} and incidents.py holds " \
        f"{sorted(words)}"
    marks = list(glyphs.values())
    assert all(glyph.strip() for glyph in marks), f"{marks} holds a blank"
    assert len(set(marks)) == len(marks), \
        f"two statuses share a glyph and cannot be told apart: {marks}"


def test_every_action_kind_and_outcome_a_page_can_record_has_its_own_glyph():
    """`incidents.ACTION_KINDS` and `incidents.Outcome`, drawn by the action
    timeline. A kind on one side and not the other is a disc with no mark, or
    a mark for an act no page can hold."""
    kinds = _js_literal("ACTION_GLYPH")
    assert set(kinds) == set(incidents.ACTION_KINDS), \
        f"ACTION_GLYPH draws {sorted(kinds)} and incidents.py records " \
        f"{sorted(incidents.ACTION_KINDS)}"
    outcomes = _js_literal("ACTION_OUTCOME")
    assert set(outcomes) == {str(o) for o in incidents.Outcome}, \
        f"ACTION_OUTCOME draws {sorted(outcomes)} and incidents.Outcome " \
        f"holds {sorted(str(o) for o in incidents.Outcome)}"
    for name, drawn in (("kind", list(kinds.values())),
                        ("outcome", [row["mk"] for row in outcomes.values()])):
        assert all(glyph.strip() for glyph in drawn), f"{drawn} holds a blank"
        assert len(set(drawn)) == len(drawn), \
            f"two {name}s share a glyph and cannot be told apart: {drawn}"
    assert all(row["badge"] for row in outcomes.values()), \
        "every outcome names a badge class as well as a glyph"


def test_the_facet_registry_offers_every_dimension_the_queue_is_read_by():
    """The lens is a registry rather than five branches, and this is what
    holds the page's copy of it to the one the walks drive."""
    html = ui.page()
    for name in FACET_NAMES:
        assert re.search(r"\n  %s:\s+\{label:" % name, html), \
            f"FACETS no longer declares the {name} facet"
    assert 'const DEFAULT_FACET = "db";' in html, \
        "the database is the dimension the queue opens on"


def test_every_message_class_the_compactor_counts_has_its_own_map():
    """`heat.CLASSES` is the vocabulary and the order the wire sends it in,
    so the page's table is checked against the module rather than against a
    list written twice. Two maps sharing a label would be two panels the
    operator cannot tell apart."""
    maps = _js_literal("HEAT_CLASSES")
    assert list(maps) == list(heat.CLASSES), \
        f"HEAT_CLASSES draws {list(maps)} and heat.py counts " \
        f"{list(heat.CLASSES)}"
    assert all(row["label"].strip() and row["hue"].strip()
               for row in maps.values()), f"{maps} holds a blank"
    labels = [row["label"] for row in maps.values()]
    assert len(set(labels)) == len(labels), \
        f"two classes share a label and cannot be told apart: {labels}"
    assert "const DEFAULT_HEAT_DAYS = 30;" in ui.page(), \
        "a month is the span the maps open on"


def test_every_severity_band_a_review_selects_has_its_own_glyph():
    """`review.BANDS` is the vocabulary; `SEVERITY_GLYPH` is the page's rule
    for drawing it with the colour off. A band on one side and not the other
    is a badge with no glyph, or a glyph for a band nothing selects."""
    glyphs = _js_literal("SEVERITY_GLYPH")
    unruled = sorted(set(review.BANDS) - set(glyphs))
    assert not unruled, \
        f"review.py bands findings {unruled}, which SEVERITY_GLYPH gives no " \
        f"glyph: each would reach the page as a badge told apart by colour " \
        f"alone"
    stale = sorted(set(glyphs) - set(review.BANDS))
    assert not stale, f"SEVERITY_GLYPH draws {stale}, which review.py cannot band"
    blank = sorted(name for name, glyph in glyphs.items() if not glyph.strip())
    assert not blank, f"{blank} render as an empty glyph"
    shared = sorted(name for name, glyph in glyphs.items()
                    if list(glyphs.values()).count(glyph) > 1)
    assert not shared, f"{shared} share one glyph and cannot be told apart"


def test_every_movement_a_review_can_report_has_its_own_glyph():
    """`review.MOVEMENTS` is the vocabulary, exported as a constant so the
    page's table is checked against the module rather than against a list
    written twice."""
    glyphs = _js_literal("MOVEMENT_GLYPH")
    assert set(glyphs) == set(review.MOVEMENTS), \
        f"MOVEMENT_GLYPH draws {sorted(glyphs)} and review.py moves findings " \
        f"{sorted(review.MOVEMENTS)}"
    marks = list(glyphs.values())
    assert all(glyph.strip() for glyph in marks), f"{marks} holds a blank"
    assert len(set(marks)) == len(marks), \
        f"two movements share a glyph and cannot be told apart: {marks}"


def test_every_delivery_status_a_week_can_record_has_its_own_glyph():
    """`delivery.STATUSES` is the vocabulary, exported as a constant so the
    page's table is checked against the module rather than against a list
    written twice."""
    glyphs = _js_literal("DELIVERY_GLYPH")
    assert set(glyphs) == set(delivery.STATUSES), \
        f"DELIVERY_GLYPH draws {sorted(glyphs)} and delivery.py records " \
        f"{sorted(delivery.STATUSES)}"
    marks = list(glyphs.values())
    assert all(glyph.strip() for glyph in marks), f"{marks} holds a blank"
    assert len(set(marks)) == len(marks), \
        f"two statuses share a glyph and cannot be told apart: {marks}"


def test_the_dead_route_rule_has_only_its_two_permanent_exemptions():
    """`page` is the document itself and `health` is `dbwiki health`'s probe.
    Anything else parked here is a route the page has stopped calling and the
    dead-route rule has stopped catching."""
    assert ui.NOT_CALLED == frozenset({"page", "health"})


def test_every_endpoint_the_page_calls_is_a_route_the_server_serves():
    try:
        from dbwiki.portal.server import ROUTES
    except ImportError as exc:
        pytest.skip(f"portal.server does not import yet: {exc}")
    called = ui.endpoints_called(ui.page())
    assert called, "the page calls at least one endpoint"
    unmatched = [f"{method} {path}" for method, path in sorted(called)
                 if not any(route.method == method
                            and route.pattern.match(path.replace("{slug}", SLUG))
                            for route in ROUTES)]
    assert not unmatched, f"the page calls endpoints no route serves: {unmatched}"


SLUG = "2026-08-11-cdb1-oracle-internal-errors"
PATH = f"incidents/{SLUG}.md"
REV = "3f2a1b7c9d4e5f60718293a4b5c6d7e8f9012345"
MOVED = "aa41d0e5c6b7889900112233445566778899aabb"
AT = "2026-08-30T12:04:11Z"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f90123456ab"
CODES = ["ORA-00600", "ORA-00607", "ORA-00604", "ORA-06512"]
LINK_BASE = "https://example.invalid/wiki/blob/main"
CITED_DIGESTS = [f"digests/cdb1/2026-08-{day}.md" for day in ("28", "29", "30")]

#: The one cited digest the wiki does not hold, so the walk sees both arms of
#: the page's `reference()`: a link, and struck-through text.
MISSING_DIGEST = CITED_DIGESTS[-1]

PRINCIPAL = {"email": "dba@example.com", "name": "D. B. Admin",
             "source": "git", "roles": ["viewer", "operator", "closer"]}

#: `wire.TRANSPORT_OF`: the shape the decoder takes back, which the page
#: reads as `EMPTY_OF[field.type]` for an untouched field.
TRANSPORT = {"flag": "boolean", "list": "list", "signal": "object"}


def field(name, widget, *, required=False, default=None, help="", choices=()):
    """`wire.field_json`'s eight keys, exactly."""
    return {"name": name, "type": TRANSPORT.get(widget, "string"),
            "widget": widget, "required": required, "default": default,
            "help": help, "choices": list(choices),
            "cli_flag": "--no-error-pages" if name == "update_error_pages"
                        else "--" + name.replace("_", "-")}


F = {
    "intent": field("intent", "line", required=True,
                    help="what the operator set out to achieve"),
    "summary": field("summary", "line", required=True,
                     help="what was done, one line"),
    "ticket": field("ticket", "line", default="",
                    help="ticket the work was raised under"),
    "outcome": field("outcome", "choice", default="pending",
                     choices=["pending", "succeeded", "failed", "rejected"],
                     help="what came of it (default pending)"),
    "rollback": field("rollback", "line", default="", help="how to undo it"),
    "evidence": field("evidence", "list", default=[],
                      help="a digest backing the action; repeatable"),
    "notes": field("notes", "text", default="",
                   help="free text kept verbatim under the record"),
    "signal": field("signal", "signal", required=True,
                    help="what recovery looks like"),
    "until": field("until", "instant", required=True,
                   help="when the window closes"),
    "reason": field("reason", "line", required=True,
                    help="why the incident is coming back open"),
    "residual_risk": field("residual_risk", "text", default="",
                           help="what is still not safe"),
    "update_error_pages": field("update_error_pages", "flag", default=True,
                                help="rewrite the linked error pages"),
}

ALLOWED = [
    {"verb": "record-action", "permitted": True, "role": "operator",
     "fields": [F["intent"], F["summary"], F["ticket"], F["outcome"],
                F["rollback"], F["evidence"], F["notes"]]},
    {"verb": "monitor", "permitted": True, "role": "operator",
     "fields": [F["signal"], F["until"], F["intent"], F["summary"],
                F["ticket"], F["evidence"], F["notes"]]},
    {"verb": "extend", "permitted": True, "role": "operator",
     "fields": [F["until"], F["intent"], F["notes"]]},
    {"verb": "resolve", "permitted": True, "role": "closer",
     "fields": [F["summary"], F["residual_risk"], F["ticket"], F["evidence"],
                F["notes"], F["update_error_pages"]]},
    {"verb": "reopen", "permitted": False, "role": "operator",
     "fields": [F["reason"], F["evidence"], F["notes"]]},
]

def day_words(iso):
    """What the page's `dayWords` says about this instant: `4 Sep`, and the
    year beside it only when it is not this one. Written here rather than
    spelled out at each assertion, because the year is a fact about when the
    suite runs and not about the fixture."""
    at = dt.date.fromisoformat(str(iso)[:10])
    year = "" if at.year == dt.date.today().year else f" {at.year}"
    return f"{at.day} {at:%b}{year}"


def stamp_words(iso):
    """`dayWords` with the clock after it, which is what `stampWords` says."""
    clock = str(iso)[11:16]
    return f"{day_words(iso)} {clock}" if clock else day_words(iso)


def day_back(days):
    """A day the browser will read as `days` whole days behind its own today.

    The board's quiet chip counts the span itself rather than being told it,
    so a fixture pinned to a written-down date would age out of whatever it
    was asserting the moment the calendar moved past it."""
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


#: `api.Workbench.queue`'s three keys, which spell the stray list `strays`.
#: Every row carries `error_codes`, which is what the queue's code facet
#: pivots on; a row with none lands in the board's "no code" group.
QUEUE = {
    "revision": REV,
    "strays": [MISSING_DIGEST],
    "incidents": [
        {"slug": SLUG, "path": PATH, "db": "cdb1",
         "title": "Oracle internal errors on cdb1",
         "status": "monitoring", "label": "Monitoring", "unknown_status": None,
         "opened": "2026-08-11T06:14:22Z", "updated": "2026-08-28T15:00:00Z",
         "verdict": "not_met", "dirty": False, "error_codes": CODES,
         "last_seen": day_back(2),
         "allowed": ["record-action", "monitor", "extend", "resolve"]},
        {"slug": "2026-08-10-cdb1_stby-tns-12564", "path": "incidents/x.md",
         "db": "cdb1_stby", "title": "TNS and ORA errors on cdb1_stby",
         "status": "open", "label": "Open", "unknown_status": None,
         "opened": "2026-08-10T21:02:00Z", "updated": "2026-08-10T21:02:00Z",
         "verdict": None, "dirty": True, "error_codes": ["TNS-12564"],
         "last_seen": day_back(2),
         "allowed": ["record-action", "monitor"]},
    ],
}

#: What `?all=1` answers with: the same two rows and one resolved incident
#: the default queue leaves out. The lens toggle is the only thing that asks
#: for it, so a walk that never presses it sees exactly what it saw before.
RESOLVED_ROW = {
    "slug": "2026-07-01-cdb1-old-news", "path": "incidents/old.md",
    "db": "cdb1", "title": "old news on cdb1", "status": "resolved",
    "label": "Resolved", "unknown_status": None,
    "opened": "2026-07-01T09:00:00Z", "updated": "2026-07-04T09:00:00Z",
    "verdict": None, "dirty": False, "error_codes": ["ORA-00600"],
    "last_seen": None,
    "allowed": [],
}

QUEUE_ALL = dict(QUEUE, incidents=QUEUE["incidents"] + [RESOLVED_ROW])


def quiet_row(slug, status, last_seen):
    return {"slug": slug, "path": f"incidents/{slug}.md", "db": "cdb1",
            "title": slug, "status": status,
            "label": status.capitalize(), "unknown_status": None,
            "opened": "2026-07-02T09:00:00Z", "updated": "2026-07-02T09:00:00Z",
            "verdict": None, "dirty": False, "error_codes": ["ORA-00600"],
            "last_seen": last_seen, "allowed": []}


#: A board the quiet chip has something to say about. One open row whose
#: codes went quiet forty days ago, one open row still being hit, one open
#: row the wire could not date at all, and one monitoring row as quiet as the
#: first: the chip is for cases nobody has closed, so the last of those is
#: the row that proves status is part of the rule and not decoration.
#: Built when a test runs, not at import: the page counts from its own today,
#: so a board dated at collection time reads a day older once a run crosses
#: midnight.
def quiet_queue():
    return dict(QUEUE, incidents=[
        quiet_row("2026-07-02-cdb1-long-quiet", "open", day_back(40)),
        quiet_row("2026-07-02-cdb1-still-hit", "open", day_back(2)),
        quiet_row("2026-07-02-cdb1-never-dated", "open", None),
        quiet_row("2026-07-02-cdb1-watched", "monitoring", day_back(40)),
    ])


QUIET_SLUG = "2026-07-02-cdb1-long-quiet"


#: A queue the shape of the live one: thirty-one incidents in four clusters
#: of near days, which is what an operator's board looks like after a bad
#: fortnight and what the fixtures above never showed. The chart cannot name
#: all of them, and this is the fixture that proves it does not try.
CROWD_TITLES = [
    "Dataguard transport failures due to host unreachability",
    "First-time TNS-12543 and ORA-16665 during switchover drill",
    "Oracle dgnonc restore failed with a corrupt controlfile",
    "Oracle EMCDB log allocation issues under the nightly load",
    "Oracle errors ORA-65100 and ORA-01109 while opening the pluggable",
    "First-ever Oracle error burst on the standby apply process",
    "TNS and ORA errors on cdb1_stby after the listener restart",
    "Heavy swapping detected alongside a checkpoint that never completed",
    "TNS-12514 service errors from every client for eleven minutes",
    "Fatal TNS-12564 and a listener that refused the handshake",
    "ORA-600 internal error in the redo apply slave on cdb1",
    "Unmatched parse errors nothing in the wiki has a name for",
    "Oracle EMCDB internal error during the repository upgrade",
    "First-time ORA-04031 shared pool exhaustion on the reporting node",
]

#: Four clusters, and how many incidents each one holds: one old case still
#: open, then three days that each took several.
CROWD_DAYS = [(80, 1), (60, 3), (45, 9), (12, 18)]


def crowd_rows():
    rows = []
    for back, many in CROWD_DAYS:
        for step in range(many):
            index = len(rows)
            day = day_back(back - step % 3)
            status = "monitoring" if index % 5 == 4 else "open"
            rows.append({
                "slug": f"2026-01-0{index % 9 + 1}-cdb1-crowd-{index:02d}",
                "path": f"incidents/crowd-{index:02d}.md", "db": "cdb1",
                "title": CROWD_TITLES[index % len(CROWD_TITLES)],
                "status": status, "label": status.capitalize(),
                "unknown_status": None, "opened": f"{day}T09:00:00Z",
                "updated": f"{day}T09:00:00Z", "verdict": None,
                "dirty": False, "error_codes": ["ORA-00600"],
                "last_seen": day_back(1), "allowed": []})
    return rows


CROWD_QUEUE = dict(QUEUE, incidents=crowd_rows())


#: The facets the lens bar offers, in the order the registry declares them.
FACET_NAMES = ["db", "status", "code", "age", "verdict"]

#: `wire._references`, one row per path the page may link. Existence rides on
#: the row rather than pruning it, so a missing digest degrades to
#: struck-through text instead of a link that 404s.
REFERENCES = [
    {"path": PATH, "kind": "page", "label": "Oracle internal errors on cdb1",
     "exists": True, "url": f"{LINK_BASE}/{PATH}"},
] + [
    {"path": f"errors/{code}.md", "kind": "error", "label": code,
     "exists": True, "url": f"{LINK_BASE}/errors/{code}.md"}
    for code in CODES
] + [
    {"path": path, "kind": "digest", "label": path,
     "exists": path != MISSING_DIGEST, "url": f"{LINK_BASE}/{path}"}
    for path in CITED_DIGESTS
]

#: `wire.research_json`, one row per code the page names. The first is
#: researched and the rest are not, which is the pair of card states the
#: region has to draw: a wiki that knows what the code means, and the gap.
CAUSE = "a low-level unexpected condition an Oracle process hit"
ACTION = "raise it with support with the trace files attached"
CITATION = {"source": "sources/oracle-docs",
            "url": "https://docs.oracle.com/en/error-help/db/ora-600/",
            "accessed": "2026-08-17"}

#: Two practitioner notes on the researched code, the second citing a source
#: the wiki holds no URL for, which is the pair the list has to draw: a slug
#: that opens the page it was read from, and a slug that only names it.
NOTES = [
    {"source": "sources/jonathan-lewis",
     "url": "https://jonathanlewis.example.invalid/ora-600/",
     "accessed": "2026-08-18",
     "text": "the trace file names the failing kernel function, and the "
             "SR is answered faster with it quoted"},
    {"source": "sources/oracle-base", "url": "", "accessed": "2026-08-16",
     "text": "a memory-corruption ORA-00600 recurs on the same instance "
             "until it is bounced"},
]

#: The provenance line each note draws under itself, in the page's own
#: vocabulary for the day it was read.
NOTE_SOURCES = [
    f"Source: jonathan-lewis (accessed {day_words('2026-08-18')})",
    f"Source: oracle-base (accessed {day_words('2026-08-16')})",
]

RESOLUTIONS = [
    {"day": "2026-08-05", "db": "cdb1",
     "incident": "2026-08-05-cdb1-tns-12564",
     "path": "incidents/2026-08-05-cdb1-tns-12564.md",
     "remediation": "raised the listener queue and bounced it",
     "evidence": "digests/cdb1/2026-08-05.md"},
    {"day": "2026-07-02", "db": "cdb2",
     "incident": "2026-07-02-cdb2-tns-12564",
     "path": "incidents/2026-07-02-cdb2-tns-12564.md",
     "remediation": "reset the dead connection detection interval",
     "evidence": "digests/cdb2/2026-07-02.md"},
]

#: Two recorded fixes on the code that was closed before: one that held and
#: one that failed, the pair `## Past fixes` adds beyond the resolution
#: history.
PAST_FIXES = [
    {"day": "2026-08-05", "db": "cdb1",
     "incident": "2026-08-05-cdb1-tns-12564",
     "path": "incidents/2026-08-05-cdb1-tns-12564.md",
     "action": "raised the listener queue and bounced it (ticket: CHG-7)",
     "outcome": "succeeded", "held": "held (14d)"},
    {"day": "2026-08-04", "db": "cdb1",
     "incident": "2026-08-05-cdb1-tns-12564",
     "path": "incidents/2026-08-05-cdb1-tns-12564.md",
     "action": "restarted the listener", "outcome": "failed",
     "held": "n/a"},
]

#: Which codes the region draws a card for and which it names on the line:
#: the researched one and the one with cases already closed on it get cards,
#: and the two the wiki can say nothing at all about get the line.
CARDED = [CODES[0], CODES[1]]

UNRESEARCHED = CODES[2:]

RESEARCH = [
    {"code": CODES[0], "path": f"errors/{CODES[0]}.md", "exists": True,
     "researched": "2026-08-17", "cause": CAUSE, "action": ACTION,
     "citations": [CITATION], "notes": NOTES, "resolutions": [],
     "past_fixes": []},
    {"code": CODES[1], "path": f"errors/{CODES[1]}.md", "exists": True,
     "researched": "", "cause": "", "action": "", "citations": [],
     "notes": [], "resolutions": RESOLUTIONS, "past_fixes": PAST_FIXES},
] + [
    {"code": code, "path": f"errors/{code}.md", "exists": True,
     "researched": "", "cause": "", "action": "", "citations": [],
     "notes": [], "resolutions": [], "past_fixes": []}
    for code in CODES[2:]
]

LIVE_LINK = {"state": "available",
             "url": "https://kibana.example.invalid/app/discover#/?_a=(x)",
             "label": "Open exact logs",
             "description": "ORA-00600 on cdb1, 2026-08-30T10:00:00Z to "
                            "2026-08-30T10:20:00Z, production oracle-logs",
             "note": ""}
DEAD_LINK = {"state": "missing", "url": None,
             "label": "Open the exact document",
             "description": "The representative document none recorded, "
                            "production oracle-logs",
             "note": "AWR-derived group; no log documents exist"}

RUN_ID = "9f2c1a0b7de4"
RUN_STARTED = "2026-08-31T08:15:00Z"
RUN_FINISHED = "2026-08-31T08:15:41Z"

NEMOTRON = "unsloth/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF"


def agent_stage(event_id, task, at, *, db="", model=NEMOTRON, tier="strong",
                adapter="pi", mode="structured", seconds=180.0, ok=True,
                rolled_back=False, usage=True):
    """One `wire.agent_stage_json` row. `usage=False` is the line that
    measured nothing, which the wire carries as the literal None."""
    return {"event_id": event_id, "task": task, "db": db, "model": model,
            "model_tier": tier, "mode": mode, "adapter": adapter,
            "started": at, "duration_s": seconds, "ok": ok,
            "rolled_back": rolled_back,
            "usage": {"input_tokens": 2496, "output_tokens": 14004,
                      "cost_usd": 0.0, "known": True, "cost_known": False}
            if usage else None,
            "trace": {"state": "available",
                      "url": "https://langfuse.example.invalid/project/p/traces/"
                             + event_id,
                      "label": "Open this stage's trace",
                      "description": "The Langfuse trace of stage " + event_id,
                      "note": ""}}


def agent_totals(stages, rolled_back=0):
    """`wire.totals_json` over those rows, sums counted the way the server
    counts them so the page cannot be shown a total no sample supports."""
    timed = [s for s in stages if s["duration_s"] is not None]
    counted = [s for s in stages if s["usage"] and s["usage"]["known"]]
    return {"stages": len(stages), "rolled_back": rolled_back,
            "tokens": sum(s["usage"]["input_tokens"]
                          + s["usage"]["output_tokens"] for s in counted),
            "token_stages": len(counted),
            "seconds": sum(s["duration_s"] for s in timed),
            "timed_stages": len(timed), "cost_usd": 0.0, "priced_stages": 0,
            "unpriced_stages": len(counted)}


#: One tick of the loop: two ingests and the report after them. The second
#: ingest failed validation and the report was rolled back, so the lanes have
#: both outlined arms to draw and the model table has a `failed` to count.
TICK_STAGES = [
    agent_stage("ev-1", "ingest", "2026-08-31T08:15:00Z", db="cdb1",
                tier="cheap", seconds=363.1),
    agent_stage("ev-2", "ingest", "2026-08-31T08:21:10Z", db="cdb2",
                seconds=254.0, ok=False),
    agent_stage("ev-3", "report", "2026-08-31T08:26:00Z",
                model="gpt-5.6-luna", adapter="codex", mode="agentic",
                seconds=99.5, rolled_back=True, usage=False),
]

#: The ticks that wrote the incident page, as `/api/incidents/{slug}` carries
#: them. The older one holds no stages: the ledger's cap has dropped them and
#: the commit is still proof the tick wrote the page.
TOUCHED_BY = [
    {"run_id": RUN_ID, "at": "2026-08-31T08:30:00Z", "commits": ["a" * 40],
     "stages": TICK_STAGES},
    {"run_id": "0011223344ff", "at": "2026-08-24T02:10:00Z",
     "commits": ["d" * 40], "stages": []},
]


INCIDENT = {
    "revision": REV, "head": REV,
    "slug": SLUG, "path": PATH, "db": "cdb1",
    "title": "Oracle internal errors on cdb1",
    "status": "monitoring", "label": "Monitoring", "unknown_status": None,
    "opened": "2026-08-11T06:14:22Z", "updated": "2026-08-28T15:00:00Z",
    "last_seen": day_back(2),
    "error_codes": CODES,
    "research": RESEARCH,
    "window": {"kind": "error_absent", "code": "ORA-00600",
               "start": "2026-08-28T15:00:00Z",
               "until": "2026-09-04T15:00:00Z"},
    "problems": [],
    "links": [LIVE_LINK, DEAD_LINK],
    "touched_by": TOUCHED_BY,
    "allowed": ALLOWED,
    "actions": [
        {"at": "2026-08-28T15:00:00Z", "kind": "start-monitoring",
         "actor": "dba@example.com", "intent": "confirm patch 36150540 holds",
         "summary": "applied patch 36150540 on cdb1",
         "status_after": "monitoring", "ticket": "CHG-8812",
         "outcome": "pending", "rollback": "opatch rollback -id 36150540",
         "window": {"kind": "error_absent", "code": "ORA-00600",
                    "start": "2026-08-28T15:00:00Z",
                    "until": "2026-09-04T15:00:00Z"},
         "evidence": ["digests/cdb1/2026-08-28.md"], "notes": ""},
    ],
    "closure": {
        "verdict": "not_met", "evaluated_at": "2026-08-30T10:15:00Z",
        "source_revision": "2b1c8f34d5e6a7b8c9d0e1f2a3b4c5d6e7f80912",
        "stale": True,
        "for": ["2026-08-29: ORA-00600 absent (digests/cdb1/2026-08-29.md)",
                "2026-08-30: ORA-00600 absent (digests/cdb1/2026-08-30.md)"],
        "against": ["2026-08-31 to 2026-09-04: 5 window days not yet observed"],
        "digests": CITED_DIGESTS,
        "facts": {"schema": 1},
    },
    "history": [
        {"sha": REV, "short": "3f2a1b7", "at": "2026-08-28T15:00:04Z",
         "author": "dba@example.com", "actor": "dba@example.com",
         "subject": f"incident: monitor {SLUG}"},
    ],
    "references": REFERENCES,
    "strays": [MISSING_DIGEST],
    "dirty": False,
    "body": "---\ntype: incident\nstatus: monitoring\n---\n",
}


BUILT_AT = "2026-08-31T10:02:11Z"
DB_NAME = "cdb1"

OWNER_COMMIT = {"sha": REV, "short": REV[:7], "at": "2026-08-30T06:15:02Z",
                "author": "dba@example.com", "actor": "",
                "subject": "ingest: cdb1 standby lag cleared"}

FLEET = {
    "revision": REV, "built_at": BUILT_AT, "head": MOVED,
    "dbs": [
        {"db": DB_NAME, "open": 1, "monitoring": 1,
         "journal_day": "2026-08-29",
         "journal_headline": "standby lag cleared", "errors_30d": 4,
         "page": f"databases/{DB_NAME}.md"},
        {"db": "cdb1_stby", "open": 1, "monitoring": 0, "journal_day": "",
         "journal_headline": "", "errors_30d": 0,
         "page": "databases/cdb1_stby.md"},
    ],
}

#: The live fleet, eighteen databases deep, headlines and all. A journal
#: headline is a sentence a model wrote and runs past a hundred characters,
#: which is the shape the two-row fixture above never had and the reason the
#: table used to scroll sideways.
BIG_FLEET = {
    "revision": REV, "built_at": BUILT_AT, "head": MOVED,
    "dbs": [{"db": db, "open": open_, "monitoring": watched,
             "errors_30d": errors, "journal_day": "2026-09-12",
             "journal_headline": headline, "page": f"databases/{db}.md"}
            for db, open_, watched, errors, headline in [
    ('awrwh', 0, 0, 0,
     'No events recorded for awrwh on 2026-09-12.'),
    ('cdb1', 17, 0, 53,
     'CDB1 log allocation failure with checkpoint incomplete '
     'on 2026-09-13; 11 events across redo log groups 890-891'),
    ('cdb1_stby', 5, 0, 2,
     'cdb1_stby 2026-09-13: 104 events, 12 notable; parse '
     'errors ORA-1219 count 10807, deleted archivelogs'),
    ('dbtest', 0, 0, 0,
     'No events recorded for dbtest on 2026-09-12; quiet '
     'operational day across all log groups'),
    ('dbtestn', 0, 0, 0,
     'No events, errors, or notable activity recorded for '
     'dbtestn on 2026-09-12 within the 21.5-hour observation '
     'window.'),
    ('dgcdb', 0, 0, 0,
     'No events reported for dgcdb on 2026-09-12; all '
     'sections (alert, listener, dataguard) show 0 events.'),
    ('dgcdb_s', 0, 0, 0,
     'No events detected for dgcdb_s on 2026-09-12'),
    ('dgnonc', 2, 0, 13,
     '2,439 TNS-12514 listener establish failures on dgnonc '
     'from 00:00:30 to 10:25:31 UTC on 2026-09-13'),
    ('dgnonc_s', 2, 0, 28,
     'dgnonc_s: 495 TNS-12564 connection refused and 495 '
     'ORA-12514 Data Guard failures, '
     '2026-09-13T00:00:30Z–08:14:31Z'),
    ('dgtest', 1, 0, 10,
     'No events recorded for dgtest on 2026-09-12'),
    ('dgtest_s', 0, 0, 0,
     'No events or errors detected for dgtest_s on 2026-09-12'),
    ('dgtests', 1, 0, 6,
     'No events recorded for dgtests on 2026-09-12'),
    ('emcdb', 3, 0, 0,
     'emcdb 2026-09-12 digest reports zero events and no '
     'notable activity across alert, listener, and dataguard '
     'groups'),
    ('pss', 0, 0, 0,
     'Oracle log digest for pss on 2026-09-12: 0 events, 0 '
     'notable across alert, listener, and dataguard groups'),
    ('pss_s', 0, 0, 0,
     'No events recorded for pss_s on 2026-09-12; all groups '
     'empty, notable false'),
    ('test5', 0, 0, 0,
     'No events recorded for test5 on 2026-09-12; all groups '
     'reported zero occurrences'),
    ('test5s', 0, 0, 0,
     'No events recorded for test5s on 2026-09-12; all '
     'categories reported zero activity.'),
    ('testcdb_s', 0, 0, 0,
     'No events recorded for testcdb_s on 2026-09-12'),
            ]],
}


#: `heat.WINDOW_DAYS` days ending on a fixed day, so a walk can name a column
#: by its date rather than by whatever today is when the suite runs.
HEAT_LAST = "2026-08-31"

HEAT_DAYS = [(dt.date.fromisoformat(HEAT_LAST) - dt.timedelta(days=back))
             .isoformat() for back in reversed(range(heat.WINDOW_DAYS))]

#: Which days each database has no digest for, counted back from `HEAT_LAST`.
#: `cdb1` is silent inside the last fortnight and again outside the last
#: month, so the toggle changes how many blank cells the map draws; the
#: unknown-host row is observed every day, so a blank is never just a row.
HEAT_SILENT = {"cdb1": {3, 40}, "cdb1_stby": {1}, "poug2db": {60},
               "orphandb": set()}

HEAT_HOSTS = [("cdb1", "lab-dg1.localdomain"),
              ("cdb1_stby", "lab-dg1.localdomain"),
              ("poug2db", "lab-dg2.localdomain"),
              ("orphandb", "")]


def heat_counts(db, name):
    """One class's column of one row: a spread of counts with zeros in it,
    and None on the days `HEAT_SILENT` says the wiki holds no digest."""
    return [None if (heat.WINDOW_DAYS - 1 - at) in HEAT_SILENT[db]
            else (at * len(name) + len(db)) % 9
            for at in range(heat.WINDOW_DAYS)]


#: `wire.heat_json` over the whole window. The rows arrive in the order the
#: server sorts them, hosts alphabetical and the unknown one last, because
#: the page draws its bands by walking that order rather than grouping again.
HEAT = {
    "revision": REV, "built_at": BUILT_AT, "head": REV,
    "days": HEAT_DAYS,
    "classes": list(heat.CLASSES),
    "rows": [{"db": db, "host": host,
              "counts": {name: heat_counts(db, name) for name in heat.CLASSES}}
             for db, host in HEAT_HOSTS],
}

PAGE_PATH = "reports/2026-08-30-0615.md"

PAGE_HTML = (
    '<h1 id="wikipage--fleet-report-2026-08-30">Fleet report 2026-08-30'
    '<a class="anchor" href="#wikipage--fleet-report-2026-08-30"'
    ' aria-label="link">#</a></h1>'
    '<p>One window over one fleet, and what it did overnight.</p>'
    '<h2 id="wikipage--summary">Summary'
    '<a class="anchor" href="#wikipage--summary" aria-label="link">#</a></h2>'
    '<div class="scroll"><table>'
    '<thead><tr><th>db</th><th>events</th></tr></thead>'
    f'<tbody><tr><td>{DB_NAME}</td><td>41</td></tr></tbody></table></div>'
    f'<p>The standing page is <a href="#/page/databases/{DB_NAME}.md">'
    f'{DB_NAME}</a>, <a href="https://example.invalid/note" target="_blank"'
    ' rel="noopener">the vendor note</a> explains it, '
    '<span class="nolink">nothing/here</span> is no page, and the digests are '
    'under <code class="path">digests/</code>.</p>')

PAGE = {
    "revision": REV, "built_at": BUILT_AT, "head": REV,
    "links": [LIVE_LINK, DEAD_LINK],
    "path": PAGE_PATH, "type": "report", "title": "Fleet report 2026-08-30",
    "html": PAGE_HTML,
    "headings": [
        {"level": 1, "text": "Fleet report 2026-08-30",
         "slug": "wikipage--fleet-report-2026-08-30"},
        {"level": 2, "text": "Summary", "slug": "wikipage--summary"},
    ],
    "origin": "agent", "commit": OWNER_COMMIT,
    "backlinks": ["index.md", f"databases/{DB_NAME}.md"],
}

DB_PAGE = {
    "path": f"databases/{DB_NAME}.md", "type": "database", "title": "cdb1",
    "html": ('<h2 id="wikipage--standby">Standby'
             '<a class="anchor" href="#wikipage--standby" aria-label="link">'
             '#</a></h2><p>The standby carries the overnight redo.</p>'),
    "headings": [{"level": 2, "text": "Standby", "slug": "wikipage--standby"}],
    "origin": "agent", "commit": OWNER_COMMIT,
    "backlinks": ["index.md"],
}

PAGES = [PAGE, {"revision": REV, "built_at": BUILT_AT, "head": REV,
                "links": [], **DB_PAGE}]

DB = {
    "revision": REV, "built_at": BUILT_AT, "head": REV, "db": DB_NAME,
    "page": DB_PAGE,
    "incidents": [
        {"slug": SLUG, "path": PATH, "db": DB_NAME,
         "title": "Oracle internal errors on cdb1", "status": "monitoring",
         "label": "Monitoring", "unknown_status": None,
         "opened": "2026-08-11T06:14:22Z", "updated": "2026-08-28T15:00:00Z",
         "origin": "operator", "commit": OWNER_COMMIT},
        {"slug": "2026-07-01-cdb1-old-news", "path": "incidents/old.md",
         "db": DB_NAME, "title": "old news", "status": "resolved",
         "label": "Resolved", "unknown_status": None,
         "opened": "2026-07-01T09:00:00Z", "updated": "2026-07-04T09:00:00Z",
         "origin": "hand", "commit": None},
    ],
    "errors": [
        {"code": "ORA-00600", "count": 12, "last_day": "2026-08-30",
         "researched": "2026-08-17", "resolved": 0,
         "page": "errors/ORA-00600.md"},
        {"code": "TNS-12564", "count": 2, "last_day": "2026-08-28",
         "researched": "", "resolved": 2, "page": "errors/TNS-12564.md"},
    ],
    "journal": [
        {"day": "2026-08-29", "headline": "standby lag cleared",
         "path": f"databases/{DB_NAME}/journal/2026-08.md"},
        {"day": "2026-08-14", "headline": "listener rebuilt",
         "path": f"databases/{DB_NAME}/journal/2026-08.md"},
    ],
}

#: The live shape of `cdb1`, which is what the database screen was redrawn
#: for: twenty-three incidents, sixty-one codes, thirty-seven journal days
#: and a standing page of four screens. Every date is written down rather
#: than counted back from today, because what is asserted about this fixture
#: is which rows are above a fold and which are under it.
BIG_OPEN = 15

BIG_DONE = 8

BIG_CODES_HELD = 61

BIG_JOURNAL_DAYS = 37

BIG_LAST = dt.date.fromisoformat("2026-09-14")


def big_incident(day, index, status):
    slug = f"{day}-{DB_NAME}-{status}-{index}"
    return {"slug": slug, "path": f"incidents/{slug}.md", "db": DB_NAME,
            "title": f"Oracle internal errors on {DB_NAME}, round {index}",
            "status": status,
            "label": "Open incident" if status == "open" else status.title(),
            "unknown_status": None, "opened": f"{day}T06:14:22Z",
            "updated": "", "origin": "operator", "commit": OWNER_COMMIT}


#: One of the fifteen live ones is monitoring, so the fold is proved to be
#: keyed by "not resolved" and not by the word "open".
BIG_INCIDENTS = (
    [big_incident(f"2026-09-{BIG_OPEN - at:02d}", at,
                  "monitoring" if at == 1 else "open")
     for at in range(BIG_OPEN)]
    + [big_incident(f"2026-08-{26 - at:02d}", at, "resolved")
       for at in range(BIG_DONE)])

#: The newest of them is the incident the shim serves, so a click on the
#: first row lands on the case file rather than on a refusal.
BIG_INCIDENTS[0] = dict(BIG_INCIDENTS[0], slug=SLUG, path=PATH)

BIG_RESOLVED_LAST = "2026-08-26"


def big_code(at):
    """Codes in the order the read model sorts them, loudest and newest
    first, so the twelfth row is the edge of the fold."""
    return {"code": f"ORA-{1000 + at}", "count": 200 - 3 * at,
            "last_day": (BIG_LAST - dt.timedelta(days=at)).isoformat(),
            "researched": "" if at % 7 == 3 else "2026-09-09",
            "resolved": 0 if at % 3 else at,
            "page": f"errors/ORA-{1000 + at}.md"}


BIG_CODE_ROWS = [big_code(at) for at in range(BIG_CODES_HELD)]

BIG_UNRESEARCHED = sum(1 for row in BIG_CODE_ROWS if not row["researched"])

BIG_HEADLINE = ("CDB1 ORA-1110 data file corruption and ORA-60 deadlock, "
                "plus media recovery and parameter changes on day ")

BIG_JOURNAL = [
    {"day": (BIG_LAST - dt.timedelta(days=at)).isoformat(),
     "headline": BIG_HEADLINE + str(at),
     "path": f"databases/{DB_NAME}/journal/2026-09.md"}
    for at in range(BIG_JOURNAL_DAYS)]

#: Title, slug and how many bullets each section of the standing page holds.
BIG_SECTIONS = [("Data Guard", "wikipage--data-guard", 5),
                ("Other settings", "wikipage--other-settings", 2),
                ("Open issues", "wikipage--open-issues", 1)]


def big_section(title, slug, points):
    items = "".join(f"<li>{title}, point {at + 1}</li>"
                    for at in range(points))
    return (f'<h2 id="{slug}">{title}<a class="anchor" href="#{slug}"'
            f' aria-label="link">#</a></h2><ul>{items}</ul>')


BIG_LEAD = ("Host <code>lab-dg1.localdomain</code>. Oracle Database 19c "
            "Enterprise Edition 19.27.0.0.0.")

BIG_PAGE_HTML = (
    f'<h1 id="wikipage--{DB_NAME}">{DB_NAME}<a class="anchor"'
    f' href="#wikipage--{DB_NAME}" aria-label="link">#</a></h1>'
    f'<p>{BIG_LEAD}</p>'
    + "".join(big_section(*one) for one in BIG_SECTIONS))

#: One other database and sixteen reports: the chips are addresses and the
#: reports are a count.
BIG_REPORTS = [f"reports/2026-08-{at + 1:02d}.md" for at in range(16)]

BIG_STANDBY = f"{DB_NAME}_stby"

BIG_PAGE = {
    "path": f"databases/{DB_NAME}.md", "type": "database", "title": DB_NAME,
    "html": BIG_PAGE_HTML,
    "headings": [{"level": 1, "text": DB_NAME,
                  "slug": f"wikipage--{DB_NAME}"}]
    + [{"level": 2, "text": title, "slug": slug}
       for title, slug, _ in BIG_SECTIONS],
    "origin": "hand", "commit": OWNER_COMMIT,
    "backlinks": [f"databases/{BIG_STANDBY}.md"] + BIG_REPORTS,
}

BIG_DB = {
    "revision": REV, "built_at": BUILT_AT, "head": REV, "db": DB_NAME,
    "page": BIG_PAGE, "incidents": BIG_INCIDENTS, "errors": BIG_CODE_ROWS,
    "journal": BIG_JOURNAL,
}

#: Two days inside the last thirty with no digest, so one walk of the bands
#: proves the hatch and the ramp on the same row.
BIG_SILENT = {3, 12}

BIG_HOST = "ol9-19-dg1.localdomain"


def big_counts(name):
    return [None if (heat.WINDOW_DAYS - 1 - at) in BIG_SILENT
            else (at * len(name) + 4) % 9
            for at in range(heat.WINDOW_DAYS)]


BIG_HEAT = dict(HEAT, rows=[
    {"db": DB_NAME, "host": BIG_HOST,
     "counts": {name: big_counts(name) for name in heat.CLASSES}}]
    + [row for row in HEAT["rows"] if row["db"] != DB_NAME])


def big_band(name):
    """The thirty days of one class the screen draws, which is the tail of
    the window the shim answers with."""
    return big_counts(name)[-30:]


SEARCH = {
    "revision": REV, "built_at": BUILT_AT, "head": REV, "query": "ORA-00600",
    "hits": [
        {"path": f"databases/{DB_NAME}.md", "type": "database",
         "title": "cdb1", "db": DB_NAME, "snippet": "cdb1"},
        {"path": PATH, "type": "incident",
         "title": "Oracle internal errors on cdb1", "db": DB_NAME,
         "snippet": "ORA-00600 fired 41 times overnight"},
        {"path": PAGE_PATH, "type": "report",
         "title": "cdb1 2026-08-30", "db": "",
         "snippet": "ORA-00600 on cdb1, 41 events"},
    ],
}


DIGEST_PAGE = f"digests/{DB_NAME}/2026-08-30.md"

STAGES = [
    {"name": "tick", "status": "succeeded", "started": RUN_STARTED,
     "finished": RUN_FINISHED, "duration_s": 41.6, "agentic": False,
     "detail": {}},
    {"name": "discover/compact", "status": "succeeded", "started": "",
     "finished": "", "duration_s": None, "agentic": False,
     "detail": {"databases": 2, "inferred": True}},
    {"name": "decide", "status": "succeeded", "started": "", "finished": "",
     "duration_s": None, "agentic": False,
     "detail": {"decisions": {"ingest": 1, "skip": 1}, "inferred": True}},
    {"name": "ingest", "status": "warning", "started": "", "finished": "",
     "duration_s": None, "agentic": True,
     "detail": {"attempted": 2, "ingested": 1, "unchanged": 0, "failed": 1,
                "attempts": 3, "timed_out": 0, "inferred": True}},
    {"name": "report", "status": "failed", "started": "", "finished": "",
     "duration_s": None, "agentic": True,
     "detail": {"error_category": "model_timeout"}},
    {"name": "render", "status": "skipped", "started": "", "finished": "",
     "duration_s": None, "agentic": False,
     "detail": {"reason": "nothing to commit"}},
    {"name": "alerts", "status": "pending", "started": "", "finished": "",
     "duration_s": None, "agentic": False,
     "detail": {"reason": "the tick did not reach them", "inferred": True}},
]

RETRY_STAGES = [dict(STAGES[0], status="failed",
                     detail={"error_category": "git_push"})] + [
    {"name": name, "status": "skipped", "started": "", "finished": "",
     "duration_s": None, "agentic": name in ("ingest", "report"),
     "detail": {"reason": "the retry command does not run this stage"}}
    for name in events.STAGE_NAMES[1:]
]

INFERRED_STAGES = sum(1 for stage in STAGES if stage["detail"].get("inferred"))

def measured(**over):
    """`wire.totals_json`'s nine keys, exactly."""
    row = {"stages": 0, "rolled_back": 0, "tokens": 0, "token_stages": 0,
           "seconds": 0.0, "timed_stages": 0, "cost_usd": 0.0,
           "priced_stages": 0, "unpriced_stages": 0}
    row.update(over)
    return row


def loop_day(date, runs=0, failed=0, **over):
    """`wire.day_json`'s four keys, exactly."""
    return {"day": date, "runs": runs, "failed": failed,
            "totals": measured(**over)}


#: Three consecutive days: one the loop worked and priced most of its stages,
#: one nothing ran on, and one whose stages reported tokens and no price. The
#: middle row is what proves an unmeasured cell draws no bar, and the last is
#: what proves one column can be unmeasured while its neighbour is not.
TREND = [
    loop_day("2026-08-29", runs=2, failed=1, stages=4, rolled_back=1,
             tokens=455298, token_stages=4, seconds=9092.72, timed_stages=4,
             cost_usd=0.31, priced_stages=3, unpriced_stages=1),
    loop_day("2026-08-30"),
    loop_day("2026-08-31", runs=2, failed=1, stages=2, tokens=12900,
             token_stages=2, seconds=82.4, timed_stages=2, priced_stages=0,
             unpriced_stages=2),
]

ALERTED_FP = "0bf83a578ab2"
QUIET_FP = "5f10a3c9d2b7"

#: `wire.failure_group_json`'s ten keys, exactly: one group an alert is open
#: about and one with none, which must say nothing rather than "recovered".
FAILURES = [
    {"fingerprint": ALERTED_FP, "category": "harness_error", "db": DB_NAME,
     "count": 23, "days": 7, "first_seen": "2026-08-10T04:15:11Z",
     "last_seen": RUN_FINISHED, "commands": ["retry", "run"],
     "sample_error": "pi produced no answer — provider unreachable",
     "alert": {"first_seen": "2026-08-29T18:16:16Z",
               "last_seen": RUN_FINISHED, "count": 43}},
    {"fingerprint": QUIET_FP, "category": "lock_busy", "db": "-", "count": 6,
     "days": 1, "first_seen": "2026-08-30T04:00:00Z",
     "last_seen": "2026-08-30T04:00:12Z", "commands": ["run"],
     "sample_error": "", "alert": None},
]

RUNS = {
    "generated_at": "2026-08-31T10:05:00Z",
    "coverage": [
        {"name": "run_health", "lines": 451, "cap": 2000,
         "oldest": "2026-07-27T04:15:00Z", "newest": RUN_FINISHED,
         "truncated": False},
        {"name": "agent_runs", "lines": 2000, "cap": 2000,
         "oldest": "2026-08-02T02:00:04Z", "newest": RUN_FINISHED,
         "truncated": True},
        {"name": "run_starts", "lines": 88, "cap": 2000,
         "oldest": "2026-08-12T00:00:11Z", "newest": RUN_FINISHED,
         "truncated": False},
    ],
    "freshness": [
        {"task": "run", "at": RUN_FINISHED, "age_h": 1.8, "threshold_h": 26,
         "stale": False},
        {"task": "report", "at": "2026-08-29T06:15:00Z", "age_h": 51.8,
         "threshold_h": 26, "stale": True},
        {"task": "research", "at": "", "age_h": None,
         "threshold_h": 192, "stale": False},
    ],
    "runs": [
        {"run_id": RUN_ID, "command": "run", "started": RUN_STARTED,
         "finished": RUN_FINISHED, "outcome": "ok", "error_category": "",
         "dbs_ingested": 1, "dbs_skipped": 1, "stages": STAGES},
        {"run_id": "1c0de5a77b31", "command": "retry",
         "started": "2026-08-31T04:00:00Z", "finished": "2026-08-31T04:00:12Z",
         "outcome": "failed", "error_category": "git_push",
         "dbs_ingested": 0, "dbs_skipped": 0, "stages": RETRY_STAGES},
    ],
    "backlog": [
        {"digest": f"digests/{DB_NAME}/2026-08-31.json", "db": DB_NAME,
         "decision": "ingest"},
    ],
    "pending": [
        {"run_id": "7b19c0aa4f52", "command": "run",
         "started": "2026-08-31T10:00:02Z"},
    ],
    "trend": TREND,
    "failures": FAILURES,
}

#: A month of the loop the size the live console holds: forty-two days and
#: five hundred and forty-six runs, a failed health or research run on
#: two days in three, and forty failure groups whose recorded message is a
#: two-hundred-character stack line. This is the payload that made the runs
#: screen twenty-three thousand pixels tall, and the reason a folded day
#: cannot be "a day that went right".
BIG_DAYS = 42
BIG_PER_DAY = 13

#: Every third day the loop got through without a failure, which is what the
#: folded row has to say when there is nothing red to count.
BIG_CLEAN_EVERY = 3
BIG_MESSAGE = (
    "pi produced no answer - the model hit its context limit while reading "
    "digests/cdb1/2026-08-29.md and the harness gave up after three "
    "attempts, last error: read timeout after 600s on stage %02d")


def big_runs():
    """Newest first and grouped by day, which is the order the fold reads.

    Every run is stamped between 11:00 and 17:00 UTC, because the fold groups
    by the reader's own day and a run at 23:05 is tomorrow in half of Europe.
    """
    rows = []
    first = dt.date.fromisoformat("2026-08-31")
    for back in range(BIG_DAYS):
        day = (first - dt.timedelta(days=back)).isoformat()
        clean = back % BIG_CLEAN_EVERY == 1
        for step in range(BIG_PER_DAY):
            index = len(rows)
            failed = not clean and step % 6 == 2
            at = f"{17 - step // 2:02d}:{30 if step % 2 == 0 else 0:02d}"
            rows.append({
                "run_id": RUN_ID if index == 0 else f"{index:012x}",
                "command": ["run", "retry", "report", "research"][index % 4],
                "started": f"{day}T{at}:00Z",
                "finished": f"{day}T{at}:41Z",
                "outcome": "failed" if failed else "ok",
                "error_category": "harness_error" if failed else "",
                "dbs_ingested": 0 if failed else 2, "dbs_skipped": 1,
                "stages": RETRY_STAGES if failed else STAGES})
    return rows


def big_failures():
    return [{"fingerprint": f"{index:012x}",
             "category": ["harness_error", "es_unreachable", "dirty_tree"]
                         [index % 3],
             "db": DB_NAME if index % 2 else "-",
             "count": 40 - index, "days": 12,
             "first_seen": "2026-08-19T06:47:50Z",
             "last_seen": "2026-08-30T00:40:01Z",
             "commands": ["retry", "run"],
             "sample_error": BIG_MESSAGE % index, "alert": None}
            for index in range(40)]


BIG_RUNS = dict(RUNS, runs=big_runs(), failures=big_failures())


PREVIOUS_RUN = "5a71b3c90ef2"

#: This run priced one of its two stages and the one before it priced
#: neither, so the compare table has to draw a measured column beside an
#: unmeasured one without subtracting them.
RUN_TOTALS = measured(stages=2, rolled_back=1, tokens=49830, token_stages=2,
                      seconds=94.2, timed_stages=2, cost_usd=0.0413,
                      priced_stages=1, unpriced_stages=1)

COMPARE = {"run_id": PREVIOUS_RUN, "command": "run",
           "started": "2026-08-30T08:15:00Z",
           "finished": "2026-08-30T08:15:39Z", "outcome": "failed",
           "error_category": "harness_error", "dbs_ingested": 0,
           "dbs_skipped": 2,
           "totals": measured(stages=2, tokens=41000, token_stages=2,
                              seconds=88.1, timed_stages=2, priced_stages=0,
                              unpriced_stages=2)}

RUN = {
    "run": {"run_id": RUN_ID, "command": "run", "started": RUN_STARTED,
            "finished": RUN_FINISHED, "outcome": "ok", "error_category": "",
            "dbs_ingested": 1, "dbs_skipped": 1},
    "stages": STAGES,
    "agents": [
        agent_stage("ev-run-1", "ingest", RUN_STARTED, db=DB_NAME),
        {**agent_stage("ev-run-2", "report", RUN_FINISHED, ok=False),
         "trace": {"state": "unavailable", "url": None,
                   "label": "Open this stage's trace",
                   "description": "The Langfuse trace of stage ev-run-2",
                   "note": "no links.langfuse.base is configured"}}],
    "links": [LIVE_LINK, DEAD_LINK],
    "totals": RUN_TOTALS,
    "compare": COMPARE,
    "dbs": [
        {"event_id": f"{RUN_ID}:{DB_NAME}", "db": DB_NAME,
         "decision": "ingest",
         "reasons": [
             {"code": "new_notable_group",
              "evidence": {"groups": 3, "top_code": "ORA-00600"}},
             {"code": "volume_delta", "evidence": {"delta_pct": 180}},
         ],
         "outcome": "ingested", "error": "", "error_category": "",
         "model_tier": "deep", "commit": SHA,
         "digest": f"wiki/digests/{DB_NAME}/2026-08-30.json",
         "digest_page": DIGEST_PAGE,
         "usage": {"input_tokens": 48210, "output_tokens": 1620,
                   "cost_usd": 0.0413, "known": True, "cost_known": True}},
        {"event_id": f"{RUN_ID}:cdb1_stby", "db": "cdb1_stby",
         "decision": "skip", "reasons": [],
         "outcome": "skipped", "error": "", "error_category": "",
         "model_tier": "", "commit": "",
         "digest": "wiki/digests/cdb1_stby/2026-08-30.json",
         "digest_page": "", "usage": None},
    ],
}


def _err_hunk(code, blob):
    return (f"diff --git a/errors/{code}.md b/errors/{code}.md\n"
            f"index {blob} 100644\n"
            f"--- a/errors/{code}.md\n"
            f"+++ b/errors/{code}.md\n"
            "@@ -24,4 +24,4 @@ Oracle's note is the authority.\n"
            " ## Seen on this fleet\n"
            " \n"
            f"-- [[incidents/{SLUG}]] open since 2026-08-11 (cdb1)\n"
            f"+- [[incidents/{SLUG}]] resolved 2026-08-30 (cdb1)\n")


DIFF = (
    f"diff --git a/{PATH} b/{PATH}\n"
    "index 4a1c9e2..b73f0d8 100644\n"
    f"--- a/{PATH}\n"
    f"+++ b/{PATH}\n"
    "@@ -1,7 +1,8 @@\n"
    " ---\n"
    " type: incident\n"
    "-status: monitoring\n"
    "+status: resolved\n"
    " db: cdb1\n"
    "+resolved: 2026-08-30T12:04:11Z\n"
    " ---\n"
    "diff --git a/index.md b/index.md\n"
    "index 8f2b41a..c0d9e17 100644\n"
    "--- a/index.md\n"
    "+++ b/index.md\n"
    "@@ -38,7 +38,6 @@ Rebuilt 2026-07-27.\n"
    " ## Open incidents\n"
    " \n"
    f"-- [[incidents/{SLUG}]] Oracle internal errors on cdb1\n"
    "diff --git a/log.md b/log.md\n"
    "index 1d2e3f4..5a6b7c8 100644\n"
    "--- a/log.md\n"
    "+++ b/log.md\n"
    "@@ -1412,3 +1412,4 @@\n"
    " [2026-08-30T11:25:00Z] lint deterministic lint clean.\n"
    f"+[2026-08-30T12:04:11Z] incident resolve {SLUG}\n"
) + "".join(_err_hunk(code, blob) for code, blob in zip(
    CODES, ["2c4d1a9..e81b3f5", "77b0e14..a3c6d92",
            "0d5f8ba..91e2c47", "b6a3711..4f0d8ce"]))

#: Seven files in the diff, and seven paths on the wire beside it.
DIFF_FILES = 7

FINDINGS = [
    {"file": PATH, "rule": "digest-missing", "severity": "error",
     "suppressed": True,
     "message": "evidence digests/cdb1/2026-08-30.md is not in the tree",
     "hint": "suppressed by lint.suppress in dbwiki.yaml"},
    {"file": "index.md", "rule": "index-order", "severity": "warning",
     "suppressed": False,
     "message": "resolved incidents are not in date order after this edit",
     "hint": "dbwiki lint --fix reorders the list"},
]

PREVIEW = {
    "verb": "resolve", "base": REV, "head": REV, "at": AT,
    "status_after": "resolved", "blocked": False, "nothing_to_do": False,
    "strays": [MISSING_DIGEST], "requires": [],
    "actor": PRINCIPAL,
    "notes": [f"stray: {MISSING_DIGEST} is uncommitted and outside this commit"],
    "fields": {
        "summary": "patch 36150540 held; ORA-00600 absent for three digests",
        "residual_risk": "ORA-06512 still fires from the nightly stats job.",
        "ticket": "CHG-8812",
        "evidence": ["digests/cdb1/2026-08-29.md", "digests/cdb1/2026-08-30.md"],
        "update_error_pages": True,
    },
    "message": f"incident: resolve {SLUG} — patch 36150540 held",
    "paths": [PATH, "index.md", "log.md"] + [f"errors/{c}.md" for c in CODES],
    "diff": DIFF,
    "findings": FINDINGS,
    "cli": f"dbwiki incident resolve {SLUG} --base {REV} --at {AT} --commit",
}

COMMITTED = {"status": 200, "body": {
    "sha": SHA, "short": SHA[:7], "paths": PREVIEW["paths"], "pushed": True,
    "base": REV, "at": AT}}

#: What the page's `costWords` says when the harness measured nothing: every
#: unknown spelled out, never a 0 and never a null duration read as `nulls`.
UNKNOWN_COST = ("model unrecorded · duration unrecorded · tokens unknown "
                "· cost unknown")

TOOL_RUN = "b71c0d9e4a52"
FIELD_RUN = "0c8e1f4a7b36"
ADVISORY_AT = "2026-08-31T11:00:00Z"


def packed(kind, path, heading, chars):
    """`wire.advisory_context_json`: what a row would read, never its body."""
    return {"kind": kind, "path": path, "heading": heading, "chars": chars,
            "truncated": False}


CONTEXT = [
    packed("incident", PATH, "the incident page", 4120),
    packed("error_page", f"errors/{CODES[0]}.md", "error pages", 2210),
    packed("digest", CITED_DIGESTS[0], "digests", 1884),
]


def ceiling(reason="", *, runs=3, unmeasured=0, cost=0.0413):
    """`wire.advisory_ceiling_json`'s eight keys, exactly."""
    return {"window_h": 24, "max_runs": 20, "max_cost_usd": None,
            "runs": runs, "measured_runs": runs - unmeasured,
            "unmeasured_runs": unmeasured, "cost_usd": cost, "reason": reason}


def tool_row(id_, label, sources, *, target_field="", chars=8214, ceil=None,
             asks=False):
    """`wire.advisory_tool_json`'s twelve keys, exactly. `asks` is the one
    that says the row takes the operator's own question, which is what puts
    it in the interview panel rather than on the panel of buttons."""
    return {"id": id_, "label": label, "role": "operator", "tier": "cheap",
            "enabled": True, "target_field": target_field, "asks": asks,
            "max_answer_chars": 900, "sources": list(sources),
            "context": CONTEXT, "context_chars": chars,
            "ceiling": ceiling() if ceil is None else ceil}


#: Three rows: one that runs, one a ceiling has closed, and the one whose
#: `target_field` lands in the form. The third's window holds runs nobody
#: priced, so the panel has to say the spend is unknown rather than add it up.
TOOLS = {"slug": SLUG, "evidence_revision": REV, "tools": [
    tool_row("explain-incident", "Explain this incident",
             ["incident", "error_page", "actions", "closure"]),
    tool_row("similar-incidents", "Find similar incidents",
             ["incident", "occurrences", "neighbours"], chars=3106,
             ceil=ceiling("max_runs", runs=20)),
    tool_row("draft-note", "Draft the summary line",
             ["incident", "error_page", "digest"], target_field="summary",
             chars=5540, ceil=ceiling(runs=4, unmeasured=2)),
]}


def advisory_run(tool, run_id, status, **over):
    """`wire.advisory_run_json`'s thirteen keys, exactly. `question` is empty
    for every row but the asking one, which records what was typed."""
    run = {"run_id": run_id, "tool": tool, "target": SLUG, "at": ADVISORY_AT,
           "status": status, "started": "2026-08-31T11:00:01Z", "finished": "",
           "duration_s": None, "evidence_revision": REV, "context": CONTEXT,
           "question": "", "answer": None, "error": ""}
    run.update(over)
    return run


#: The answer names one digest the wiki holds and one it does not, so the
#: panel's citations see both arms of the page's `reference()`.
EXPLAINED = {
    "text": "ORA-00600 fired 41 times overnight on cdb1 and has been absent "
            "since patch 36150540 went on.",
    "cites": [CITED_DIGESTS[0], MISSING_DIGEST],
    "model": "gemma-3",
    "usage": {"input_tokens": 4820, "output_tokens": 63, "cost_usd": 0.0021,
              "known": True},
}

DRAFTED_LINE = "patch 36150540 went on; ORA-00600 has been absent since."

#: The harness reported nothing at all for this one, so every count on the
#: panel has to read as a word.
DRAFTED = {"text": DRAFTED_LINE, "cites": [], "model": "gemma-3",
           "usage": "unknown"}

STARTS = {
    "explain-incident": {"status": 202, "body": advisory_run(
        "explain-incident", TOOL_RUN, "queued")},
    "draft-note": {"status": 202, "body": advisory_run(
        "draft-note", FIELD_RUN, "queued")},
}

POLLS = {
    TOOL_RUN: [
        advisory_run("explain-incident", TOOL_RUN, "running"),
        advisory_run("explain-incident", TOOL_RUN, "succeeded",
                     finished="2026-08-31T11:00:04Z", duration_s=2.4,
                     answer=EXPLAINED),
    ],
    FIELD_RUN: [
        advisory_run("draft-note", FIELD_RUN, "succeeded",
                     finished="2026-08-31T11:00:03Z", duration_s=1.9,
                     answer=DRAFTED),
    ],
}

#: The interview row: free text in, one run per question. Held out of the
#: default manifest so every walk that never asks a question sees exactly the
#: three rows it saw before.
ASK_TOOL = tool_row("ask-incident", "Interview this incident",
                    ["incident", "error_page", "actions", "closure",
                     "digest", "neighbours"], chars=9711, asks=True)

ASK_TOOLS = {**TOOLS, "tools": TOOLS["tools"] + [ASK_TOOL]}

ASK_RUN = "c4f70a19b862"

QUESTION = "Why is the window still not met?"

ANSWERED = {
    "text": "Five of the seven window days have not been observed yet, so "
            "the rule cannot say the error is absent for the whole window.",
    "cites": [CITED_DIGESTS[0], MISSING_DIGEST],
    "model": "gemma-3",
    "usage": {"input_tokens": 9711, "output_tokens": 71, "cost_usd": 0.0031,
              "known": True},
}

ASK_STARTS = {"ask-incident": {"status": 202, "body": advisory_run(
    "ask-incident", ASK_RUN, "queued", question=QUESTION)}}

ASK_POLLS = {**POLLS, ASK_RUN: [
    advisory_run("ask-incident", ASK_RUN, "running", question=QUESTION),
    advisory_run("ask-incident", ASK_RUN, "succeeded", question=QUESTION,
                 finished="2026-08-31T11:00:06Z", duration_s=3.1,
                 answer=ANSWERED),
]}

#: What `api.advisory_start` answers a question a tool's instruction has no
#: `{question}` placeholder for, and an asking row asked with nothing typed.
BAD_QUESTION = {"status": 400, "body": {
    "error": "bad_question",
    "message": "this tool takes no question", "field": "question"}}

CEILING_REFUSED = {"status": 429, "body": {
    "error": "ceiling_reached",
    "message": "this tool has reached its max_runs ceiling for the window; "
               "nothing was spent",
    "reason": "max_runs", "ceiling": ceiling("max_runs", runs=20),
    "run_id": "6d3a91f0c8b4"}}

BASE_MOVED = {"status": 409, "body": {
    "error": "base_moved", "message": "the wiki moved while you were reviewing",
    "expected": REV, "actual": MOVED, "base": REV, "at": AT}}

LINT_BLOCKED = {"status": 422, "body": {
    "error": "lint_blocked", "message": "lint refuses this edit",
    "findings": [dict(FINDINGS[0], suppressed=False), FINDINGS[1]],
    "base": REV, "at": AT}}

CONFIRMATION_REQUIRED = {"status": 422, "body": {
    "error": "confirmation_required", "field": "residual_risk",
    "message": "a resolve records what is still not safe",
    "base": REV, "at": AT}}

LOCK_BUSY = {"status": 423, "body": {
    "error": "lock_busy", "message": "another workbench request is publishing",
    "holder": "another workbench request is publishing", "retry_after_s": 3,
    "base": REV, "at": AT}}

REVIEW_ID = "2026-W36"
LAST_REVIEW_ID = "2026-W35"
REVIEW_AT = "2026-09-07T10:00:00Z"
ACKED_AT = "2026-09-08T09:30:00Z"
SUPPRESSED_TO = "2026-09-15T09:30:00Z"
ACTOR = PRINCIPAL["email"]

STALE_FP = "7164c7e860e9"
CROSS_FP = "3adef51f4ea0"


#: Evidence shaped the way each of `review.py`'s detectors emits it, so what
#: the screen turns into words is what it will be given.
REASON_EVIDENCE = {
    "stale_open": {"opened": "2026-07-28T09:00:00Z", "updated": "",
                   "age_days": 44.18},
    "occurrences_up": {"code": "TNS-12514", "db": DB_NAME, "recent": 13,
                       "prior": 1, "increase": 12, "window_days": 28},
    "multi_db": {"code": "ORA-00600", "count": 4,
                 "databases": ["cdb1", "cdb1_stby", "dgnonc", "emcdb"]},
    "repeat_occurrences": {"code": "ORA-00060", "db": DB_NAME, "count": 4,
                           "days": ["2026-09-01", "2026-09-03"],
                           "window_days": 14},
}


def review_finding(fingerprint, kind, code, slug, db, severity, movement, *,
                   path="", ack=None, reasons=None, title=None):
    return {"fingerprint": fingerprint, "kind": kind, "code": code,
            "slug": slug, "db": db, "title": title or f"{code} on {db}",
            "path": path, "severity": severity, "movement": movement,
            "explanation": f"{slug} on {db}: {kind} ({code}, {severity}).",
            "reasons": reasons or [{"code": code,
                                    "evidence": REASON_EVIDENCE[code]}],
            "ack": ack or {"acknowledged_at": None, "suppressed_until": None,
                           "actor": ""}}


REVIEW_FINDINGS = [
    review_finding(STALE_FP, "stale_open", "stale_open", SLUG, DB_NAME,
                   "high", "carried", path=PATH,
                   title="Oracle internal errors on cdb1"),
    review_finding(CROSS_FP, "cross_db", "multi_db", "ORA-00600", "-",
                   "normal", "new", title="ORA-00600 across 4 databases"),
]

REVIEW_COUNTS = {"selected": 2, "high": 1, "normal": 1, "shadowed": 0,
                 "new": 1, "changed": 0, "carried": 1, "resolved": 0,
                 "acknowledged": 3, "suppressed": 1, "capped": 2}

REVIEW = {
    "review_id": REVIEW_ID,
    "generated_at": REVIEW_AT,
    "source_revision": REV,
    "window": {"from": "2026-08-10T10:00:00Z", "to": REVIEW_AT, "days": 28},
    "counts": REVIEW_COUNTS,
    "changes": {"new": [CROSS_FP], "carried": [STALE_FP]},
    "explanation": "2026-W36: 2 selected (1 high, 1 normal) over 28 days.",
    "findings": REVIEW_FINDINGS,
    "synthesis": {
        "summary": "one incident has been open and untouched for a month",
        "themes": [{"title": "nobody has acted",
                    "detail": "the page has recorded nothing since 2026-08-06",
                    "evidence_refs": [PATH]}],
        "evidence_refs": [PATH], "model_tier": "cheap"},
    "synthesis_error": "",
    "pack_manifest": [{"kind": "summary", "heading": "the selection",
                       "path": "", "chars": 412, "truncated": False}],
    "deliveries": [
        {"key": REVIEW_ID + "|inbox", "channel": "inbox", "recipient": "inbox",
         "content_class": "internal_full", "status": "delivered",
         "at": REVIEW_AT, "error": ""},
        {"key": REVIEW_ID + "|ops", "channel": "email", "recipient": "ops",
         "content_class": "internal_full", "status": "failed",
         "at": REVIEW_AT, "error": "timeout"},
    ],
}

#: ---- a week shaped like the ones the workbench actually holds ----
#: Nineteen findings over five databases in both bands, three of them already
#: answered for, a covering note with three themes and a delivery that failed.
#: The movements are mixed inside each band on the wire, so the order the
#: screen draws them in is the screen's and not the server's.
BIG_REVIEW_ID = "2026-W35"

BIG_SELECTION = [
    ("stale_open", "stale_open", "cdb1", "high", "carried"),
    ("stale_open", "stale_open", "cdb1_stby", "high", "carried"),
    ("worsening", "occurrences_up", "dgnonc", "high", "new"),
    ("stale_open", "stale_open", "cdb1", "high", "carried"),
    ("stale_open", "stale_open", "emcdb", "high", "carried"),
    ("stale_open", "stale_open", "cdb1", "high", "new"),
    ("stale_open", "stale_open", "cdb1_stby", "high", "carried"),
    ("worsening", "occurrences_up", "dgnonc_s", "high", "new"),
    ("stale_open", "stale_open", "dgnonc", "high", "carried"),
    ("stale_open", "stale_open", "emcdb", "high", "carried"),
    ("stale_open", "stale_open", "cdb1_stby", "high", "new"),
    ("stale_open", "stale_open", "cdb1", "high", "carried"),
    ("stale_open", "stale_open", "cdb1", "normal", "carried"),
    ("cross_db", "multi_db", "-", "normal", "new"),
    ("stale_open", "stale_open", "dgnonc", "normal", "carried"),
    ("stale_open", "stale_open", "emcdb", "normal", "carried"),
    ("recurring", "repeat_occurrences", "cdb1", "normal", "carried"),
    ("stale_open", "stale_open", "dgnonc_s", "normal", "carried"),
    ("stale_open", "stale_open", "cdb1_stby", "normal", "carried"),
]

BIG_ACKED_AT = (0, 6)

BIG_SUPPRESSED_AT = 14

#: One slug long enough that its chip has to be cut, so the card is proved to
#: keep the whole address in the title it cuts from.
BIG_LONG = "2026-08-06-cdb1-dataguard-transport-failure"

BIG_TOUCHED = "2026-08-12T11:30:00Z"

BIG_REPEAT = [
    {"code": "repeat_occurrences",
     "evidence": {"code": "ORA-00060", "db": "cdb1", "count": 4,
                  "days": ["2026-09-01", "2026-09-03"], "window_days": 14}},
] + [{"code": "repeat_occurrences",
      "evidence": {"code": "ORA-00060", "db": "cdb1", "day": day}}
     for day in ("2026-09-01", "2026-09-03")]


def big_evidence(code, at):
    """Every third stale row has been touched since it was opened, so both of
    the sentences that reason can say are on the screen."""
    held = dict(REASON_EVIDENCE[code])
    if code == "stale_open":
        held["age_days"] = 44.18 if at % 3 else 21.62
        held["updated"] = "" if at % 3 else BIG_TOUCHED
    return held


def big_slug(at, db):
    return BIG_LONG if at == 5 else f"2026-08-{at + 1:02d}-{db}-oracle-errors"


#: Where a finding of each kind has its page, and which kind has none: a
#: `multi_db` code is named across databases and `review.py` has no one page
#: to point at for it.
BIG_PATHS = {"stale_open": "incidents/{slug}.md",
             "occurrences_up": "errors/{slug}.md",
             "repeat_occurrences": "errors/{slug}.md"}


def big_finding(at, kind, code, db, severity, movement):
    slug = big_slug(at, db) if code == "stale_open" else f"ORA-006{at:02d}"
    ack = None
    if at in BIG_ACKED_AT:
        ack = {"acknowledged_at": ACKED_AT, "suppressed_until": None,
               "actor": ACTOR}
    elif at == BIG_SUPPRESSED_AT:
        ack = {"acknowledged_at": None, "suppressed_until": SUPPRESSED_TO,
               "actor": ACTOR}
    return review_finding(
        f"{at:012x}", kind, code, slug, db, severity, movement,
        path=BIG_PATHS.get(code, "").format(slug=slug),
        ack=ack, reasons=BIG_REPEAT if code == "repeat_occurrences" else [
            {"code": code, "evidence": big_evidence(code, at)}],
        title=f"{kind.replace('_', ' ').capitalize()} on {db}" if db != "-"
        else f"{slug} across 4 databases")


BIG_FINDINGS = [big_finding(at, *row)
                for at, row in enumerate(BIG_SELECTION)]

BIG_REVIEW_COUNTS = {"selected": 19, "high": 12, "normal": 7, "shadowed": 12,
                     "new": 5, "changed": 0, "carried": 14, "resolved": 4,
                     "acknowledged": 2, "suppressed": 1, "capped": 21}

#: Seven pages the note cites, three themes citing from among them and
#: nothing else: a theme that names a page the note names has named it once,
#: and the card counts that set rather than the two lists laid end to end.
BIG_NOTE_REFS = [BIG_FINDINGS[at]["path"] for at in (0, 1, 3, 4, 5, 8, 11)]

BIG_THEMES = [
    {"title": "Data Guard transport keeps failing on the standby pair",
     "detail": "Four incidents on cdb1 and cdb1_stby name TNS-12564 and "
               "ORA-16603 against the same transport.",
     "evidence_refs": BIG_NOTE_REFS[:3]},
    {"title": "First-ever error codes on cdb1",
     "detail": "ORA-334, ORA-353 and ORA-603 have no page before this week.",
     "evidence_refs": BIG_NOTE_REFS[3:5]},
    {"title": "TNS-12514 is climbing on both dgnonc nodes",
     "detail": "Thirteen occurrences in the window against one before it.",
     "evidence_refs": BIG_NOTE_REFS[5:]},
]

BIG_PACK_ROWS = 20

BIG_MANIFEST = [
    {"kind": "summary" if at == 0 else "incident",
     "heading": f"section {at}", "path": "" if at == 0 else f"p/{at}.md",
     "chars": 300 + at, "truncated": at == 7}
    for at in range(BIG_PACK_ROWS)]

BIG_REVIEW = dict(
    REVIEW, review_id=BIG_REVIEW_ID, counts=BIG_REVIEW_COUNTS,
    findings=BIG_FINDINGS, pack_manifest=BIG_MANIFEST,
    explanation="", changes={},
    synthesis={"summary": "Five databases carried this week, and the standby "
                          "pair carried most of it.",
               "themes": BIG_THEMES, "evidence_refs": BIG_NOTE_REFS,
               "model_tier": "cheap"})

BIG_ONE_BAND = dict(BIG_REVIEW, findings=[
    row for row in BIG_FINDINGS if row["severity"] == "high"])


INBOX = {"generated_at": ACKED_AT, "reviews": [
    {"review_id": REVIEW_ID, "generated_at": REVIEW_AT,
     "source_revision": REV, "counts": REVIEW_COUNTS, "synthesis_error": "",
     "synthesized": True},
    {"review_id": LAST_REVIEW_ID, "generated_at": "2026-08-31T10:00:00Z",
     "source_revision": MOVED,
     "counts": {**REVIEW_COUNTS, "selected": 1, "high": 0, "normal": 1},
     "synthesis_error": "timeout", "synthesized": False},
]}

#: ---- fourteen weeks, shaped like a workbench that has been running ----
#: What the inbox holds after a quarter: a heavy week at the top, a week that
#: selected nothing at all, two the model never wrote a note for and one it
#: was never asked to. `selected` runs from nothing to the reviewer's own cap
#: and `high` runs the whole of it, so the bar beside a week is proved against
#: a week that carried twelve and not only against itself.
BIG_INBOX_WEEKS = [
    (38, 12, 12), (37, 12, 5), (36, 9, 0), (35, 7, 3), (34, 11, 11),
    (33, 0, 0), (32, 6, 2), (31, 2, 1), (30, 10, 4), (29, 3, 3),
    (28, 8, 6), (27, 5, 2), (26, 1, 1), (25, 4, 0),
]

BIG_INBOX_ID = "2026-W38"

BIG_INBOX_OLDEST = "2026-W25"

BIG_INBOX_NEWEST_DAY = dt.date(2026, 9, 14)

BIG_INBOX_NOTES = {37: "harness_error", 35: "timeout"}

BIG_INBOX_SILENT = 34


def big_week(at, week, selected, high):
    """One inbox item, with a revision of its own so the `judged at` column is
    fourteen different shas and not one repeated. The three movements
    partition the selection, so the week that selected nothing is a week that
    reports nothing rather than one whose columns contradict each other."""
    changed = min(selected, week % 3)
    carried = (selected - changed) // 2
    return {
        "review_id": f"2026-W{week}",
        "generated_at": (BIG_INBOX_NEWEST_DAY
                         - dt.timedelta(days=7 * at)).isoformat()
                        + "T08:00:00Z",
        "source_revision": hashlib.sha1(f"2026-W{week}".encode()).hexdigest(),
        "counts": {"selected": selected, "high": high,
                   "normal": selected - high, "new": selected - changed
                   - carried, "changed": changed, "carried": carried,
                   "resolved": week % 7, "acknowledged": 0, "suppressed": 0,
                   "capped": 20 + selected, "shadowed": 0},
        "synthesis_error": BIG_INBOX_NOTES.get(week, ""),
        "synthesized": week not in BIG_INBOX_NOTES and week != BIG_INBOX_SILENT,
    }


BIG_INBOX = {"generated_at": ACKED_AT, "reviews": [
    big_week(at, week, selected, high)
    for at, (week, selected, high) in enumerate(BIG_INBOX_WEEKS)]}

#: What each fingerprint's `acks.json` item is before the walk touches it. An
#: id absent from here is one no published review holds, which is the 404 the
#: `/api/run?id=` precedent answers.
ACKS = {STALE_FP: {"acknowledged_at": None, "suppressed_until": None,
                   "actor": ""},
        CROSS_FP: {"acknowledged_at": None, "suppressed_until": None,
                   "actor": ""}}

#: Installed right after `<body>`, so it replaces `window.fetch` before the
#: page's own script runs. Every request is recorded; `base_moved` advances
#: the head the next preview is answered against, which is the whole point
#: of that arm. The routing matches the slug exactly, so a page that builds
#: its URL out of a key the read model does not hold gets a 404 here rather
#: than a preview it did not earn.
AGENTS_NOW = "2026-08-31T10:05:00Z"

LINT_STAGES = [
    agent_stage("ev-4", "lint", "2026-08-31T09:40:00Z", model="gpt-5.6-luna",
                adapter="codex", mode="agentic", tier="cheap", seconds=29.9),
]

#: A tick from the night before, so the 24-hour toggle has something to drop
#: and the chart has a command other than `run` to write over a bar.
OLD_STAGES = [
    agent_stage("ev-0", "ingest", "2026-08-30T02:00:00Z", db="cdb1",
                seconds=201.0),
]

AGENT_TICKS = [
    {"run_id": "3c0de5a77b31", "command": "lint",
     "started": "2026-08-31T09:40:00Z", "finished": "2026-08-31T09:40:30Z",
     "stages": LINT_STAGES, "incidents": [],
     "totals": agent_totals(LINT_STAGES)},
    {"run_id": RUN_ID, "command": "run", "started": "2026-08-31T08:15:00Z",
     "finished": "2026-08-31T08:26:00Z", "stages": TICK_STAGES,
     "incidents": [
         {"slug": SLUG, "title": "Oracle internal errors on cdb1",
          "status": "monitoring", "db": "cdb1",
          "commits": ["a" * 40, "b" * 40]},
         {"slug": "2026-08-30-cdb2-tns-12564", "title": "TNS-12564 on cdb2",
          "status": "open", "db": "cdb2", "commits": ["c" * 40]}],
     "totals": agent_totals(TICK_STAGES, rolled_back=1)},
    {"run_id": "778899aabbcc", "command": "retry",
     "started": "2026-08-30T02:00:00Z", "finished": "2026-08-30T02:03:21Z",
     "stages": OLD_STAGES, "incidents": [],
     "totals": agent_totals(OLD_STAGES)},
]

AGENT_MODELS = [
    {"model": NEMOTRON, "adapters": ["pi"], "tiers": {"cheap": 1, "strong": 1},
     "ok": 2, "failed": 1,
     "totals": agent_totals(TICK_STAGES[:2] + OLD_STAGES)},
    {"model": "gpt-5.6-luna", "adapters": ["codex"],
     "tiers": {"cheap": 1, "strong": 1}, "ok": 1, "failed": 0,
     "totals": agent_totals(LINT_STAGES + TICK_STAGES[2:], rolled_back=1)},
]

AGENTS = {
    "revision": REV, "built_at": AGENTS_NOW, "head": REV,
    "generated_at": AGENTS_NOW, "window_hours": 48,
    "coverage": RUNS["coverage"],
    "ticks": AGENT_TICKS,
    "models": AGENT_MODELS,
    "totals": agent_totals(LINT_STAGES + TICK_STAGES + OLD_STAGES,
                           rolled_back=1),
}

#: Three researches inside two hours, which is what the loop does when it is
#: catching up on codes nobody has looked at. The live chart printed the word
#: three times over neighbouring bars and over the gridline under them.
RESEARCH_TICKS = [
    {"run_id": f"researc{index}0000", "command": "research",
     "started": f"2026-08-31T0{7 - index}:20:00Z",
     "finished": f"2026-08-31T0{7 - index}:22:00Z",
     "stages": [agent_stage(f"ev-r{index}", "research",
                            f"2026-08-31T0{7 - index}:20:00Z", seconds=90.0)],
     "incidents": [],
     "totals": agent_totals([agent_stage(f"ev-r{index}", "research",
                                         f"2026-08-31T0{7 - index}:20:00Z",
                                         seconds=90.0)])}
    for index in range(3)]

CROWDED_AGENTS = dict(AGENTS, ticks=AGENT_TICKS[:2] + RESEARCH_TICKS
                      + AGENT_TICKS[2:])




LINKS = {
    "title": "dbhost Link Board",
    "intro": "Everything reachable over `tailscale` for the pipeline.",
    "sections": [
        {"title": "Dashboards",
         "note": "Generated by `build_dashboards.py`, never edited in the UI.",
         "links": [
             {"name": "Fleet triage",
              "what": "Where on call starts: `oracle_error` by database.",
              "url": "http://box.example.invalid:5601/app/dashboards",
              "tag": "tailscale"},
             {"name": "Verification report",
              "what": "",
              "url": "https://claude.ai/code/artifact/abc",
              "tag": "artifact"}]},
        {"title": "Repositories",
         "note": "",
         "links": [
             {"name": "logbook",
              "what": "",
              "url": "https://github.com/example/logbook",
              "tag": "github"}]},
    ],
}


SHIM = """
<script>
window.__requests = [];
window.__mock = %s;
window.fetch = async function (url, init) {
  const method = (init && init.method) || "GET";
  const body = init && init.body ? JSON.parse(init.body) : null;
  window.__requests.push({method: method, url: url, body: body,
                          headers: (init && init.headers) || {}});
  const m = window.__mock;
  const one = "/api/incidents/" + m.slug;
  const path = url.split("?")[0];
  const args = new URLSearchParams(url.split("?")[1] || "");
  let reply = {status: 404, body: {error: "no_route", message: url}};
  if (method === "GET" && path === "/api/fleet") {
    reply = {status: 200, body: m.fleet};
  } else if (method === "GET" && path === "/api/db") {
    reply = args.get("name") === m.db.db
      ? {status: 200, body: m.db}
      : {status: 404, body: {error: "unknown_db", message: url}};
  } else if (method === "GET" && path === "/api/page") {
    const found = m.pages.find(p => p.path === args.get("path"));
    reply = found
      ? {status: 200, body: found}
      : {status: 404, body: {error: "no_such_page", message: url}};
  } else if (method === "GET" && path === "/api/runs") {
    reply = {status: 200, body: m.runs};
  } else if (method === "GET" && path === "/api/agents") {
    const asked = parseInt(args.get("hours") || "48", 10);
    if (!asked || asked < 1 || asked > 720) {
      reply = {status: 400, body: {error: "bad_hours", message: url}};
    } else {
      const edge = Date.parse(m.agents.generated_at) - asked * 3600000;
      const held = m.agents.ticks.filter(
        (tick) => Date.parse(tick.started) >= edge);
      reply = {status: 200, body: Object.assign({}, m.agents,
        {window_hours: asked, ticks: held})};
    }
  } else if (method === "GET" && path === "/api/links") {
    reply = {status: 200, body: m.links};
  } else if (method === "GET" && path === "/api/run") {
    reply = args.get("id") === m.run.run.run_id
      ? {status: 200, body: m.run}
      : {status: 404, body: {error: "unknown_run", message: url}};
  } else if (method === "GET" && path === "/api/inbox") {
    reply = {status: 200, body: m.inbox};
  } else if (method === "POST" && path === "/api/inbox") {
    const held = m.acks[body.fingerprint];
    reply = held
      ? {status: 200, body: {fingerprint: body.fingerprint,
          ack: Object.assign({}, held, {actor: m.actor},
            body.action === "acknowledge"
              ? {acknowledged_at: m.acked_at}
              : {suppressed_until: m.suppressed_to})}}
      : {status: 404, body: {error: "unknown_item", message: url}};
    if (held) m.acks[body.fingerprint] = reply.body.ack;
  } else if (method === "GET" && path === "/api/review") {
    reply = args.get("id") === m.review.review_id
      ? {status: 200, body: m.review}
      : {status: 404, body: {error: "unknown_review", message: url}};
  } else if (method === "GET" && path === "/api/search") {
    reply = {status: 200, body: Object.assign({}, m.search,
                                              {query: args.get("q") || ""})};
  } else if (method === "GET" && path === "/api/heat") {
    const asked = parseInt(args.get("days") || "30", 10);
    const span = Math.min(90, Math.max(1, asked || 30));
    const slice = (row) => Object.assign({}, row, {counts:
      Object.fromEntries(Object.keys(row.counts).map(
        (name) => [name, row.counts[name].slice(-span)]))});
    reply = {status: 200, body: Object.assign({}, m.heat,
      {days: m.heat.days.slice(-span), rows: m.heat.rows.map(slice)})};
  } else if (method === "GET" && path === "/api/incidents") {
    reply = {status: 200,
             body: args.get("all") === "1" ? m.queue_all : m.queue};
  } else if (method === "GET" && url === one) {
    reply = {status: 200, body: m.incident};
  } else if (url === one + "/preview") {
    const pv = JSON.parse(JSON.stringify(m.preview));
    if (body) {
      pv.verb = body.verb;
      pv.fields = body.fields;
      pv.at = body.at || pv.at;
      pv.base = body.base || m.head_now;
    }
    reply = {status: 200, body: pv};
  } else if (url === one + "/commit") {
    reply = m.commits.length > 1 ? m.commits.shift() : m.commits[0];
    if (reply.body && reply.body.error === "base_moved") {
      m.head_now = reply.body.actual;
    }
  } else if (method === "GET" && url === one + "/tools") {
    reply = {status: 200, body: m.tools};
  } else if (method === "POST" && url === one + "/advisory") {
    reply = m.starts[body.tool]
      || {status: 404, body: {error: "unknown_tool", message: url}};
  } else if (method === "GET" && path === "/api/advisory") {
    const queue = m.polls[args.get("run")] || [];
    reply = queue.length
      ? {status: 200, body: queue.length > 1 ? queue.shift() : queue[0]}
      : {status: 404, body: {error: "unknown_run", message: url}};
  }
  /* One endpoint answered 500 whatever it holds, so a walk can prove what a
     screen does when a fetch it does not depend on is refused. */
  if ((m.refuse || []).indexOf(path) >= 0) {
    reply = {status: 500, body: {error: "broken", message: url}};
  }
  return {status: reply.status, ok: reply.status < 400,
          json: async function () { return reply.body; }};
};
</script>
"""


def mock_page(tmp_path, name, commits=None, incident=None, tools=None,
              starts=None, polls=None, inbox=None, week=None, acks=None,
              run=None, queue=None, queue_all=None, links=None, agents=None,
              fleet=None, runs=None, refuse=None, db=None, heat=None,
              pages=None):
    """`ui.page()` over the mock wiki, written into `tmp_path` as a file the
    browser can open. Every argument replaces one shape the shim answers with,
    so one test can serve a read model, a manifest or a click a ceiling
    refuses without a second copy of the other eight."""
    data = {"slug": SLUG,
            "queue": QUEUE if queue is None else queue,
            "queue_all": QUEUE_ALL if queue_all is None else queue_all,
            "incident": incident or INCIDENT,
            "fleet": FLEET if fleet is None else fleet,
            "heat": HEAT if heat is None else heat,
            "db": DB if db is None else db,
            "pages": PAGES if pages is None else pages,
            "search": SEARCH,
            "runs": RUNS if runs is None else runs,
            "run": RUN if run is None else run,
            "agents": AGENTS if agents is None else agents,
            "links": LINKS if links is None else links,
            "inbox": INBOX if inbox is None else inbox,
            "review": json.loads(json.dumps(week if week else REVIEW)),
            "acks": json.loads(json.dumps(ACKS if acks is None else acks)),
            "refuse": list(refuse or []),
            "actor": ACTOR, "acked_at": ACKED_AT,
            "suppressed_to": SUPPRESSED_TO,
            "preview": PREVIEW, "head_now": REV,
            "commits": list(commits or [COMMITTED]),
            "tools": TOOLS if tools is None else tools,
            "starts": {**STARTS, **(starts or {})},
            "polls": {run: list(queue)
                      for run, queue in (polls or POLLS).items()}}
    html = ui.page().replace("<body>", "<body>" + SHIM % json.dumps(data), 1)
    out = tmp_path / f"{name}.html"
    out.write_text(html, encoding="utf-8")
    return out


#: Virtual key code and the text a key press carries, if any.
KEYS = {"Tab": (9, "\t"), "Enter": (13, "\r"), "Escape": (27, None)}

CENTRE_JS = """(() => {
  const el = document.querySelector(%s);
  if (!el) return null;
  el.scrollIntoView({block: "center", inline: "center"});
  const r = el.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2};
})()"""


def _frame(payload, opcode=0x1):
    # RFC 6455 requires a fresh random mask on every client frame; an
    # unmasked one is a protocol error and the peer hangs up.
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
    elif n < 1 << 16:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
    key = os.urandom(4)
    return header + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class Chrome:
    """One headless browser on one page target. Standard library only."""

    def __init__(self, size=(1280, 900), timeout=30.0):
        self.timeout = timeout
        self.profile = tempfile.mkdtemp(prefix="workbench-chrome-")
        self.sock = None
        self.buf = bytearray()
        self.pending = {}
        self.last_id = 0
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", "--no-sandbox", f"--user-data-dir={self.profile}",
             "--remote-debugging-port=0", "--remote-allow-origins=*",
             f"--window-size={size[0]},{size[1]}", "--no-first-run",
             "--disable-gpu"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self._connect(self._port())
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        # Chrome's helpers keep re-creating profile directories for a moment
        # after the parent exits, so a single rmtree races them, leaves the
        # re-made ones behind, and ignore_errors swallows the failure.
        deadline = time.monotonic() + 2.0
        while os.path.exists(self.profile) and time.monotonic() < deadline:
            shutil.rmtree(self.profile, ignore_errors=True)
            time.sleep(0.05)

    def goto(self, url):
        result = self.send("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise AssertionError(f"could not load {url}: {result['errorText']}")
        self.until("document.readyState === 'complete' "
                   "&& typeof STATE !== 'undefined'")

    def eval(self, js):
        result = self.send("Runtime.evaluate", {
            "expression": js, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            raise AssertionError(
                detail.get("exception", {}).get("description")
                or detail.get("text") or "javascript exception")
        return result.get("result", {}).get("value")

    def until(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while True:
            value = self.eval(predicate)
            if value:
                return value
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out waiting for {predicate}")
            time.sleep(0.05)

    def click(self, selector):
        centre = self.eval(CENTRE_JS % json.dumps(selector))
        if centre is None:
            raise AssertionError(f"no element matches {selector}")
        for kind, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            self.send("Input.dispatchMouseEvent", {
                "type": kind, "x": centre["x"], "y": centre["y"],
                "button": "left", "buttons": buttons, "clickCount": 1})

    def press(self, key):
        code, text = KEYS[key]
        for kind in ("keyDown", "keyUp"):
            params = {"type": kind, "windowsVirtualKeyCode": code,
                      "nativeVirtualKeyCode": code, "key": key, "code": key}
            if text and kind == "keyDown":
                params["text"] = text
            self.send("Input.dispatchKeyEvent", params)

    def send(self, method, params=None):
        self.last_id += 1
        message_id = self.last_id
        self.sock.sendall(_frame(json.dumps(
            {"id": message_id, "method": method,
             "params": params or {}}).encode()))
        deadline = time.monotonic() + self.timeout
        while message_id not in self.pending:
            message = json.loads(self._message(deadline))
            if "id" in message:
                self.pending[message["id"]] = message
        reply = self.pending.pop(message_id)
        if "error" in reply:
            raise AssertionError(f"{method}: {reply['error']}")
        return reply.get("result", {})

    def _port(self):
        deadline = time.monotonic() + self.timeout
        path = os.path.join(self.profile, "DevToolsActivePort")
        while True:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"chrome exited with status {self.proc.returncode}")
            try:
                with open(path) as handle:
                    lines = handle.read().splitlines()
            except OSError:
                lines = []
            if len(lines) >= 2 and lines[0].isdigit():
                return int(lines[0])
            if time.monotonic() >= deadline:
                raise AssertionError("chrome never wrote DevToolsActivePort")
            time.sleep(0.02)

    def _connect(self, port):
        deadline = time.monotonic() + self.timeout
        while True:
            # urllib honours Content-Length; the devtools HTTP server keeps
            # the connection alive, so a read-to-EOF would never return.
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list",
                    timeout=self.timeout) as response:
                targets = json.load(response)
            url = next((t["webSocketDebuggerUrl"] for t in targets
                        if t.get("type") == "page"
                        and t.get("webSocketDebuggerUrl")), None)
            if url:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("chrome exposed no page target")
            time.sleep(0.05)
        split = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection(
            (split.hostname, split.port), timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.sendall((
            f"GET {split.path or '/'} HTTP/1.1\r\n"
            f"Host: {split.hostname}:{split.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        deadline = time.monotonic() + self.timeout
        while b"\r\n\r\n" not in self.buf:
            self._fill(deadline)
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        self.buf = bytearray(rest)
        if b" 101" not in head.split(b"\r\n", 1)[0]:
            raise AssertionError(f"devtools handshake failed: {head[:80]!r}")

    def _message(self, deadline):
        parts, started = [], False
        while True:
            first, second = self._take(2, deadline)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._take(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._take(8, deadline))[0]
            payload = self._take(length, deadline)
            opcode = first & 0x0F
            if opcode == 0x9:
                self.sock.sendall(_frame(payload, 0xA))
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise AssertionError("the browser closed the devtools socket")
            if opcode == 0x0 and started:
                parts.append(payload)
            else:
                parts, started = [payload], True
            if first & 0x80:
                return b"".join(parts)

    def _take(self, n, deadline):
        while len(self.buf) < n:
            self._fill(deadline)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _fill(self, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError("timed out reading from devtools")
        self.sock.settimeout(left)
        chunk = self.sock.recv(1 << 16)
        if not chunk:
            raise AssertionError("the devtools socket closed")
        self.buf += chunk


@pytest.fixture
def browser():
    chrome = Chrome()
    try:
        yield chrome
    finally:
        chrome.close()


FILL = {
    "summary": "patch 36150540 held; ORA-00600 absent for three digests",
    "ticket": "CHG-8812",
    "evidence": "digests/cdb1/2026-08-29.md\ndigests/cdb1/2026-08-30.md",
}

EVIDENCE = ["digests/cdb1/2026-08-29.md", "digests/cdb1/2026-08-30.md"]

#: The form builds one `.why` block, on the field the server's confirmation
#: policy names, and removes it from every other field. Its button stands the
#: standing sentence in for a paragraph the closer would otherwise write.
NONE_IDENTIFIED = "#fields .why button"

VERB_JS = """(() => {
  const b = Array.from(document.querySelectorAll(".kind"))
    .find(x => x.querySelector(".verb").textContent === %s);
  if (!b) return null;
  b.dataset.pick = "1";
  return {disabled: b.disabled, role: b.querySelector(".role").textContent};
})()"""


def find_verb(page, name):
    """The verb button's state, marked so `click` can reach it by selector."""
    found = page.eval(VERB_JS % json.dumps(name))
    assert found, f"the incident screen offers a {name} button"
    return found


def notice(page):
    return page.eval("document.getElementById('notice').innerText")


def last_request(page):
    return page.eval("window.__requests[window.__requests.length - 1]")


def open_incident(page):
    page.until("document.querySelectorAll('#queue-rows button').length === 2")
    page.click("#queue-rows button")
    page.until("STATE.screen === 'incident'")


def lens(page, facet):
    page.click(f'#lens-bar button[data-facet="{facet}"]')
    page.until(f"STATE.lens.facet === {json.dumps(facet)}")


def group_names(page):
    return page.eval(TEXTS % "#queue-rows .group-head .gname")


def open_ask(page):
    page.click("#ask summary")
    page.until("STATE.tools !== null")


def ask_question(page, text):
    page.eval("(() => { const n = document.getElementById('ask-q'); n.value = "
              "%s; return true; })()" % json.dumps(text))
    page.click("#ask-go")
    page.until("STATE.thread.length > 0")


def pick_resolve(page):
    find_verb(page, "resolve")
    page.click(".kind[data-pick]")
    page.until("STATE.screen === 'form'")


def pick_record_action(page):
    find_verb(page, "record-action")
    page.click(".kind[data-pick]")
    page.until("STATE.screen === 'form'")


def fill_form(page, values=FILL):
    """Fields written the way keystrokes write them: the `input` event is
    what has the page read the form back into `STATE.values`."""
    page.eval(
        "(() => { const v = %s; for (const k in v) { const n = "
        "document.getElementById('f-' + k); n.value = v[k]; "
        "n.dispatchEvent(new Event('input', {bubbles: true})); } "
        "return true; })()" % json.dumps(values))


def walk_to_confirm(page):
    open_incident(page)
    pick_resolve(page)
    page.click(NONE_IDENTIFIED)
    fill_form(page)
    page.click("#form-go")
    page.until("STATE.screen === 'preview'")
    page.click("#preview-go")
    page.until("STATE.screen === 'confirm'")


@needs_chrome
def test_the_page_walks_from_the_queue_to_a_published_commit(browser, tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "walk").as_uri())

    page.until("document.querySelectorAll('#queue-rows button').length === 2")
    dirty = "document.querySelectorAll('#queue-rows .dirty')"
    assert page.eval(f"{dirty}[0].hidden"), \
        "the first queue row is clean and hides its dirty marker"
    assert not page.eval(f"{dirty}[1].hidden"), \
        "the second queue row shows its dirty marker"
    assert f"wiki at {REV[:7]}" in page.eval(
        "document.getElementById('queue-strip').innerText"), \
        "the queue strip names the revision the queue was read at"
    assert "1 uncommitted path" in page.eval(
        "document.getElementById('queue-strip').innerText"), \
        "the queue strip counts the strays the server named"
    page.press("Escape")
    assert page.eval("STATE.screen") == "queue", \
        "Escape on the queue is refused: the state table has no back row for it"

    page.click("#queue-rows button")
    page.until("STATE.screen === 'incident'")
    reopen = find_verb(page, "reopen")
    assert reopen["disabled"], "reopen is refused to a principal without its role"
    assert "operator" in reopen["role"], \
        "the disabled reopen button names the role it needs"
    cited = "#context-in .links"
    assert page.eval(f"document.querySelectorAll('{cited} a').length") == 2, \
        "a cited digest the wiki holds is a link"
    assert page.eval(
        f"document.querySelector('{cited} .nolink').textContent") \
        == MISSING_DIGEST, \
        "a cited digest the wiki does not hold degrades to text, not a link"

    pick_resolve(page)
    assert page.eval("document.getElementById('context').open"), \
        "the resolve form opens the incident context it is judged against"
    assert page.eval("document.getElementById('form-go').disabled"), \
        "preview is refused while summary and residual_risk are empty"
    assert page.eval("document.getElementById('f-summary').value") == ""
    assert page.eval("document.getElementById('f-residual_risk').value") == ""

    assert page.eval(f"document.querySelector('{NONE_IDENTIFIED}').textContent") \
        == "None identified"
    page.click(NONE_IDENTIFIED)
    assert page.eval("document.getElementById('f-residual_risk').value"), \
        "the None identified button writes the standing sentence into the field"
    fill_form(page)
    assert not page.eval("document.getElementById('form-go').disabled"), \
        "every field the record needs is written"

    page.click("#form-go")
    page.until("STATE.screen === 'preview'")
    sent = last_request(page)
    assert sent["method"] == "POST"
    assert sent["url"] == f"/api/incidents/{SLUG}/preview", \
        "the page builds its URL from the slug on the read model"
    assert sent["body"]["verb"] == "resolve"
    assert sent["body"]["fields"]["evidence"] == EVIDENCE, \
        "the page splits the evidence textarea into one path per line"
    assert sent["body"]["fields"]["update_error_pages"] is True, \
        "the checked flag goes on the wire as a boolean"

    assert page.eval("document.querySelectorAll('#diff .filerow').length") \
        == DIFF_FILES
    assert page.eval("document.querySelectorAll('#diff ins').length") > 0
    assert page.eval("document.querySelectorAll('#diff del').length") > 0
    assert f"{DIFF_FILES} files" in page.eval(
        "document.getElementById('diff').getAttribute('aria-label')"), \
        "the one scroll region names how many files it holds"
    assert page.eval("document.querySelectorAll('#lint .finding').length") == 2
    assert not page.eval("document.getElementById('preview-go').disabled"), \
        "an unblocked preview can be carried to the publish step"

    page.click("#preview-go")
    page.until("STATE.screen === 'confirm'")
    assert page.eval("Array.from(document.querySelectorAll("
                     "'#confirm-panel dl dt')).map(n => n.textContent)") \
        == ["base", "at", "actor", "paths"]
    assert page.eval(
        "document.querySelectorAll('#confirm-panel dl dd')[2].textContent") \
        == "dba@example.com (git)", \
        "the actor row names the identity the preview response carried"
    page.press("Escape")
    assert page.eval("STATE.screen") == "preview", \
        "Escape on the publish step goes back to the diff"
    page.click("#preview-go")
    page.until("STATE.screen === 'confirm'")

    minted = page.eval("STATE.preview.at")
    page.click("#confirm-go")
    page.until("STATE.screen === 'result'")
    commit = last_request(page)
    assert commit["url"].endswith("/commit")
    assert set(commit["body"]) == {"verb", "fields", "base", "at"}
    assert commit["body"]["at"] == minted == AT, \
        "the commit carries the at the preview minted, so a retry converges"
    assert SHA[:7] in page.eval(
        "document.getElementById('result-panel').innerText"), \
        "the result names the commit it wrote"


def refused_at_confirm(page, tmp_path, name, reply):
    page.goto(mock_page(tmp_path, name, [reply]).as_uri())
    walk_to_confirm(page)
    minted = page.eval("STATE.preview.at")
    page.click("#confirm-go")
    return minted


@needs_chrome
def test_every_refusal_lands_the_operator_where_it_can_be_fixed(browser,
                                                                tmp_path):
    page = browser

    minted = refused_at_confirm(page, tmp_path, "base-moved", BASE_MOVED)
    page.until("STATE.screen === 'preview'")
    assert page.eval("window.__requests.filter("
                     "r => r.url.indexOf('/preview') !== -1).length") == 2, \
        "a moved base re-previews the same act against the new head"
    assert page.eval("STATE.preview.at") == minted, \
        "the record's at survives the re-preview, so publishing writes one record"
    assert page.eval("STATE.preview.base") == MOVED, \
        "the re-preview is against the head the server named"
    assert "the wiki moved while you were reviewing" in notice(page)
    assert MOVED[:7] in notice(page), "the notice names the new head"

    refused_at_confirm(page, tmp_path, "lint-blocked", LINT_BLOCKED)
    page.until("STATE.screen === 'preview'")
    assert page.eval("document.getElementById('preview-go').disabled"), \
        "a lint-blocked edit cannot be carried past the review step"
    assert "lint refuses this edit" in notice(page)

    refused_at_confirm(page, tmp_path, "confirmation", CONFIRMATION_REQUIRED)
    page.until("STATE.screen === 'form'")
    assert page.eval("document.activeElement.id") == "f-residual_risk", \
        "the form comes back focused on the field the server named"
    assert "a resolve records what is still not safe" in notice(page)

    refused_at_confirm(page, tmp_path, "lock-busy", LOCK_BUSY)
    page.until("document.getElementById('retry') !== null")
    assert page.eval("STATE.screen") == "confirm", \
        "a busy lock leaves the operator on the publish step"
    assert page.eval("document.getElementById('retry').disabled"), \
        "the retry button waits out retry_after_s before it becomes a button"
    label = page.eval("document.getElementById('retry').textContent")
    assert re.fullmatch(r"Publish again in [123]s", label), \
        f"the retry button names the seconds left, said {label!r}"
    assert "another workbench request is publishing" in notice(page)


#: The panel is appended to the field it filled, so it is found through that
#: field rather than through the form: a panel floating above the whole form
#: would satisfy a bare `.proposal` selector.
PANEL = ("document.getElementById('f-summary').closest('.field')"
         ".querySelector('.proposal')")

#: The one request whose absence proves the ladder stopped.
POLLED = "window.__requests.filter(r => r.url.indexOf('/api/advisory?') === 0)"

MANIFEST = "window.__requests.filter(r => r.url.indexOf('/tools') !== -1)"

#: Longer than the ladder's 5s tail, so a poll that was still scheduled would
#: have fired inside it.
POLL_QUIET = 6.0


@needs_chrome
def test_the_tools_panel_prices_every_row_before_a_click_spends_anything(
        browser, tmp_path):
    """The panel's whole argument: an advisory click is the one button on this
    workbench that costs money, so what it would read and what the window has
    left are on screen before it is pressed, and the manifest that says so is
    not packed at all until the operator asks."""
    page = browser
    page.goto(mock_page(tmp_path, "tools").as_uri())
    open_incident(page)
    assert page.eval(f"{MANIFEST}.length") == 0, \
        "opening an incident packs nothing: the manifest costs the server " \
        "five real packs and nobody asked for it yet"
    assert "opening this asks" in strip_text(page, "tools-strip")

    page.click("#tools summary")
    page.until("STATE.tools !== null")
    assert page.eval(f"{MANIFEST}[0].url") == f"/api/incidents/{SLUG}/tools"
    page.click("#tools summary")
    page.click("#tools summary")
    assert page.eval(f"{MANIFEST}.length") == 1, \
        "the manifest is read once per incident, not once per opening"

    strip = strip_text(page, "tools-strip")
    assert "3 tools" in strip and "2 you can run now" in strip
    assert page.eval(TEXTS % "#tool-rows .name") == [
        row["label"] for row in TOOLS["tools"]]
    reads = page.eval(TEXTS % "#tool-rows li:nth-child(1) .reads")[0]
    assert "incident, error page, actions, closure" in reads, \
        f"the row says which evidence a click would read, said {reads!r}"
    assert "8214 characters packed" in reads, "and how much of it there is"
    ceilings = page.eval(TEXTS % "#tool-rows .ceiling")
    assert ceilings[0] == "3 of 20 runs in the last 24 hours · $0.0413"
    assert "cost unknown" in ceilings[2] and "$" not in ceilings[2], \
        f"a window holding runs nobody priced never reports a total, " \
        f"said {ceilings[2]!r}"
    assert page.eval("Array.from(document.querySelectorAll("
                     "'#tool-rows button')).map(b => b.disabled)") \
        == [False, True, False]
    assert "as often as the window allows" in page.eval(
        TEXTS % "#tool-rows li:nth-child(2) .why")[0], \
        "a greyed-out row says in words what the server said in a code"

    page.click("#tool-rows li:nth-child(1) button")
    page.until("STATE.advisory['explain-incident'] !== undefined")
    sent = last_request(page)
    assert sent["url"] == f"/api/incidents/{SLUG}/advisory"
    assert sent["headers"].get("content-type") == "application/json", \
        "the start carries the content type server._dispatch demands; " \
        "without it the real server answers 415 and this shim cannot tell"
    assert set(sent["body"]) == {"tool", "at"}
    assert sent["body"]["tool"] == "explain-incident"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                        sent["body"]["at"]), \
        f"the page mints the click's own at, said {sent['body']['at']!r}"

    page.until("STATE.advisory['explain-incident'].status === 'succeeded'",
               timeout=20)
    assert page.eval(f"{POLLED}.length") == 2, \
        "the ladder polled until the run settled and no more often"
    panel = "#tool-rows li:nth-child(1) .proposal"
    assert page.eval(f"document.querySelector('{panel} .badge').textContent") \
        == "Agent proposal", "the answer says whose words these are"
    assert EXPLAINED["text"] in page.eval(
        f"document.querySelector('{panel} .says').textContent")
    assert page.eval(f"document.querySelectorAll('{panel} .links a').length") \
        == 1, "a cited digest the wiki holds is a link"
    assert page.eval(
        f"document.querySelector('{panel} .links .nolink').textContent") \
        == MISSING_DIGEST, \
        "a cited digest the wiki does not hold degrades to text, not a link"
    cost = page.eval(f"document.querySelector('{panel} .cost').textContent")
    assert "gemma-3" in cost and "$0.0021" in cost, \
        f"the cost line names the model and what it cost, said {cost!r}"
    assert page.eval("costWords({duration_s: null, "
                     "answer: {model: null, usage: 'unknown'}})") \
        == UNKNOWN_COST, \
        "a run the harness measured nothing about reads as unknown"

    time.sleep(POLL_QUIET)
    assert page.eval(f"{POLLED}.length") == 2, \
        "a settled run stops the ladder rather than polling forever"

    page.click(f"{panel} button")
    page.until("STATE.advisory['explain-incident'] === undefined")
    assert page.eval("document.querySelector('#tool-rows .proposal') === null"), \
        "dismissing takes the answer off the row it was rendered in"



@needs_chrome
def test_two_rows_started_together_each_keep_their_own_ladder(browser,
                                                              tmp_path):
    """`advisory.Settings.max_concurrent` admits more than one run at a time,
    so the page has to hold more than one timer. With a single handle the
    second start cancels the first row's ladder, and that row sits at whatever
    status it last read with a button it has disabled against itself."""
    page = browser
    page.goto(mock_page(tmp_path, "two-rows").as_uri())
    open_incident(page)
    page.click("#tools summary")
    page.until("STATE.tools !== null")

    page.click("#tool-rows li:nth-child(1) button")
    page.until("STATE.advisory['explain-incident'] !== undefined")
    page.until("inflight === false")
    page.click("#tool-rows li:nth-child(3) button")
    page.until("STATE.advisory['draft-note'] !== undefined")

    page.until("STATE.advisory['explain-incident'].status === 'succeeded' "
               "&& STATE.advisory['draft-note'].status === 'succeeded'",
               timeout=20)
    assert page.eval(TEXTS % "#tool-rows .badge .word") \
        == ["succeeded", "", "succeeded"], \
        "both started rows settled, and the row nobody ran shows no status"
    assert page.eval(
        "document.querySelectorAll('#tool-rows .proposal').length") == 2, \
        "and each renders the answer its own run came back with"
    assert page.eval(f"{POLLED}.length") == 3, \
        "two polls for the run that queued, one for the run that did not"


@needs_chrome
def test_a_start_a_ceiling_refuses_leaves_a_notice_and_nothing_else(browser,
                                                                    tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "ceiling",
                        starts={"explain-incident": CEILING_REFUSED}).as_uri())
    open_incident(page)
    page.click("#tools summary")
    page.until("STATE.tools !== null")
    page.click("#tool-rows li:nth-child(1) button")
    page.until("document.getElementById('notice').hidden === false")

    assert page.eval("document.querySelector('#notice .badge').textContent") \
        == "ceiling reached", "the refusal wears its REFUSALS row's label"
    assert CEILING_REFUSED["body"]["message"] in notice(page), \
        "the notice carries what the server said, not a paraphrase"
    assert "Nothing was spent" in notice(page)
    assert page.eval("STATE.screen") == "incident", \
        "a refused click leaves the operator where they were"
    assert page.eval("Object.keys(STATE.advisory).length") == 0, \
        "and holds no run: the refusal is not a run the panel can show"
    assert page.eval("document.querySelector('#tool-rows .proposal') === null")
    time.sleep(2.0)
    assert page.eval(f"{POLLED}.length") == 0, \
        "there is nothing to poll for, so nothing is polled for"


@needs_chrome
def test_a_tool_answer_lands_in_the_field_and_never_reaches_the_commit(
        browser, tmp_path):
    """The one row whose `target_field` names a form control, walked end to
    end. What the wiki gets is the operator's own line: the answer is a
    prefill, and no key of the run travels with the commit."""
    page = browser
    page.goto(mock_page(tmp_path, "field").as_uri())
    open_incident(page)
    pick_record_action(page)
    page.until("STATE.tools !== null")
    assert page.eval(f"{MANIFEST}.length") == 1, \
        "the form needs the manifest to know which row fills its field"
    button = "document.getElementById('form-tool')"
    assert not page.eval(f"{button}.hidden"), \
        "record-action has a landed field, so the row's button is offered"
    assert page.eval(f"{button}.textContent") == "Draft the summary line", \
        "and it is labelled with the row it would run"

    page.click("#form-tool")
    page.until("STATE.proposal !== null", timeout=20)
    started = page.eval("window.__requests.filter("
                        "r => r.method === 'POST' "
                        "&& r.url.indexOf('/advisory') !== -1)")
    assert len(started) == 1 and started[0]["body"]["tool"] == "draft-note"
    assert page.eval("document.getElementById('f-summary').value") \
        == DRAFTED_LINE, "the model's line is an editable prefill"
    assert page.eval("document.activeElement.id") == "f-summary", \
        "focus lands on the field the operator now has to read and edit"
    assert page.eval(f"{PANEL} !== null"), \
        "the panel sits with the text it describes"
    assert page.eval(f"{PANEL}.querySelector('.badge').textContent") \
        == "Agent proposal", "the panel says whose line this is"
    assert "not from the record" in page.eval(
        f"{PANEL}.querySelector('.says').textContent"), \
        "and says the line is the model's and not a record of what was done"
    cost = page.eval(f"{PANEL}.querySelector('.cost').textContent")
    assert cost == "gemma-3 · 1.9s · tokens unknown · cost unknown", \
        f"a run nobody measured says so in words, never as 0, said {cost!r}"

    page.click("#form .proposal button")
    page.until("STATE.proposal === null")
    assert page.eval("document.getElementById('f-summary').value") == "", \
        "Reject puts back what the field held before the answer landed"
    assert page.eval("document.querySelector('#form .proposal') === null"), \
        "the panel goes with the proposal it described"
    assert page.eval("document.activeElement.id") == "f-summary", \
        "focus comes back to the field"

    page.click("#form-tool")
    page.until("STATE.proposal !== null", timeout=20)
    mine = "restarted the apply process and watched it hold overnight"
    intent = "confirm patch 36150540 held before closing the window"
    fill_form(page, {"summary": mine, "intent": intent})
    page.click("#form-go")
    page.until("STATE.screen === 'preview'")
    sent = last_request(page)
    assert set(sent["body"]) == {"verb", "fields"}, \
        "a form with an answer in it previews the way any other form does"
    assert sent["body"]["fields"]["summary"] == mine

    page.click("#preview-go")
    page.until("STATE.screen === 'confirm'")
    page.click("#confirm-go")
    page.until("STATE.screen === 'result'")
    commit = last_request(page)
    assert set(commit["body"]) == {"verb", "fields", "base", "at"}
    assert commit["body"]["fields"]["summary"] == mine, \
        "what is published is the operator's own line, edited over the model's"
    body = json.dumps(commit["body"])
    assert DRAFTED_LINE not in body and FIELD_RUN not in body, \
        "no trace of the advisory run travels with the commit"
    assert "advisory" not in body and "answer" not in body, \
        "and the commit body's vocabulary is the write vocabulary, whole"


CLOSURE_RUN = "3e9a4c1b8d70"

#: The narration the closure row comes back with. It names the digest the
#: wiki does not hold as well as one it does, so the panel under the evidence
#: sees both arms of `reference()` the way the tools panel does.
NARRATED = {
    "text": "The window still argues against closing: 5 window days from "
            "2026-08-31 have not been observed. Two digests show ORA-00600 "
            "absent.",
    "cites": [CITED_DIGESTS[0], MISSING_DIGEST],
    "model": "gemma-3",
    "usage": {"input_tokens": 1980, "output_tokens": 44, "cost_usd": 0.0008,
              "known": True},
}

NARRATION_STARTS = {"explain-closure": {"status": 202, "body": advisory_run(
    "explain-closure", CLOSURE_RUN, "queued")}}

NARRATION_POLLS = {**POLLS, CLOSURE_RUN: [
    advisory_run("explain-closure", CLOSURE_RUN, "succeeded",
                 finished="2026-08-31T11:00:02Z", duration_s=1.2,
                 answer=NARRATED)]}

ASK = "document.getElementById('closure-ask')"

NARRATION_PANEL = "#context-in .proposal"

#: The narration is painted after the case it narrates. Asserted with the DOM
#: order rather than a selector count, because "beside the closure evidence"
#: is a claim about where the operator's eye lands.
AFTER_THE_CASE = (
    "(document.querySelector('#context-in .forag.against')"
    ".compareDocumentPosition(document.getElementById('closure-ask'))"
    " & Node.DOCUMENT_POSITION_FOLLOWING) !== 0")


def resolve_walk(page, *, narrate):
    """The resolve form from the queue to the preview it sends, optionally
    asking for the narration in the middle. Returns the preview's body."""
    open_incident(page)
    pick_resolve(page)
    if narrate:
        page.click("#closure-ask")
        page.until("STATE.advisory['explain-closure'] !== undefined "
                   "&& STATE.advisory['explain-closure'].status "
                   "=== 'succeeded'", timeout=20)
    page.click(NONE_IDENTIFIED)
    fill_form(page)
    page.click("#form-go")
    page.until("STATE.screen === 'preview'")
    return last_request(page)["body"]


@needs_chrome
def test_the_closure_narration_sits_under_the_case_and_changes_no_resolve(
        browser, tmp_path):
    """3.2's whole surface: a painter beside `closureBlock`.

    The narration is offered for every verdict a case can carry — the fixture
    is `not_met`, the one the page would most plausibly want to hide, and it
    is exactly the one an operator reads it for. Nothing about the resolve
    moves because of it: the same fields go to the same preview, so the two
    walks send byte-identical bodies.
    """
    page = browser
    mock = mock_page(tmp_path, "narration", starts=NARRATION_STARTS,
                     polls=NARRATION_POLLS).as_uri()

    page.goto(mock)
    plain = resolve_walk(page, narrate=False)

    page.goto(mock)
    open_incident(page)
    assert page.eval(f"{ASK} !== null"), \
        "a case exists, so the narration is offered"
    assert page.eval(f"{MANIFEST}.length") == 0, \
        "and it costs the server nothing to offer: the button needs none of " \
        "the manifest's real packs to work"
    assert page.eval(f"{ASK}.textContent") == "Explain the monitoring facts"

    pick_resolve(page)
    assert page.eval(AFTER_THE_CASE), \
        "the narration is painted under the for-and-against it narrates"
    assert page.eval(
        "document.getElementById('closure-ask-cap').textContent") == \
        "describes the monitoring facts; the closure decision and its rules " \
        "are unchanged", "the caption says what the narration is not"
    assert page.eval(f"document.querySelector('{NARRATION_PANEL}') === null")

    page.goto(mock)
    narrated = resolve_walk(page, narrate=True)
    assert narrated == plain, \
        "the same form previews the same edit whether or not it was narrated"
    started = page.eval("window.__requests.filter("
                        "r => r.method === 'POST' "
                        "&& r.url.indexOf('/advisory') !== -1)")
    assert len(started) == 1 and started[0]["body"]["tool"] == "explain-closure"
    assert page.eval("STATE.proposal === null "
                     "&& STATE.filling === null"), \
        "a narration is not a proposal: it fills no field and lands nowhere"

    page.click("#preview-go")
    page.until("STATE.screen === 'confirm'")
    assert page.eval(f"document.querySelector('{NARRATION_PANEL}') !== null"), \
        "the narration stays beside the evidence the confirmation is read " \
        "against"
    assert NARRATED["text"] in page.eval(
        f"document.querySelector('{NARRATION_PANEL} .says').textContent")
    assert page.eval(
        f"document.querySelectorAll('{NARRATION_PANEL} .links a').length") == 1
    page.click("#confirm-go")
    page.until("STATE.screen === 'result'")
    commit = last_request(page)
    assert set(commit["body"]) == {"verb", "fields", "base", "at"}
    body = json.dumps(commit["body"])
    assert NARRATED["text"] not in body and CLOSURE_RUN not in body, \
        "nothing the model said travels with the resolve"


def hash_of(page):
    return page.eval("location.hash")


def strip_text(page, node):
    """`textContent`, not `innerText`: `.badge` is uppercased in CSS and
    rendered text would hide the words the badge was written with."""
    return page.eval(f"document.getElementById('{node}').textContent")


@needs_chrome
def test_the_nav_strip_and_the_row_buttons_walk_the_read_screens(browser,
                                                                 tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "navigate").as_uri())
    page.until("STATE.screen === 'queue'")
    assert hash_of(page) == "#/queue", \
        "boot writes the address of the screen it landed on"

    page.click("#nav-fleet")
    page.until("STATE.screen === 'fleet'")
    assert hash_of(page) == "#/fleet"
    fleet = strip_text(page, "fleet-strip")
    assert "read model behind HEAD" in fleet, \
        "the snapshot is a rebuild behind HEAD, and the page says so"
    assert MOVED[:7] in fleet, "the badge names the head the snapshot is behind"
    assert page.eval(
        "document.getElementById('nav-fleet').getAttribute('aria-current')") \
        == "page", "the strip marks where the operator is"
    said = page.eval("document.getElementById('fleet-said').textContent")
    assert "2 databases" in said and "2 open" in said, \
        f"the fleet is summed before it is listed, said {said!r}"
    assert page.eval(TEXTS % "#fleet-table tbody tr:nth-child(1) td.num") \
        == ["1", "1", "4"], "every measure is the cell's own text"
    assert page.eval(
        "document.querySelectorAll('#fleet-table .bar').length") == 0, \
        "and no bar under any of them: a count of one is a count of one " \
        "whether the row beside it holds two or nothing"

    page.click("#fleet-table tbody button")
    page.until("STATE.screen === 'db'")
    assert hash_of(page) == "#/db/" + DB_NAME
    assert "read model behind HEAD" not in strip_text(page, "db-strip"), \
        "this envelope's revision is its head, so there is no badge to draw"
    assert page.eval(
        "document.querySelector('#db-about .owner .badge').textContent") \
        == "agent", "the origin chip names who last wrote the standing page"
    assert OWNER_COMMIT["short"] in page.eval(
        "document.querySelector('#db-about .owner .sha').textContent"), \
        "and the commit it was written in"
    assert page.eval(TEXTS % "#db-rows tbody td.day") == \
        [day_words(row["opened"]) for row in DB["incidents"]
         if row["status"] != "resolved"], \
        "the live incidents are rows, and the resolved ones are behind a fold"

    page.click("#db-rows tbody td.what a")
    page.until("STATE.screen === 'incident'")
    assert hash_of(page) == "#/incident/" + SLUG
    assert last_request(page)["url"] == f"/api/incidents/{SLUG}", \
        "the row opens the incident by the slug the database view carried"


SPARKS = "#fleet-table tbody tr:nth-child(1) .spark-svg rect"

FLEET_TICK = "#fleet-table tbody tr:nth-child(1) td.tick a"


@needs_chrome
def test_the_fleet_says_how_loud_a_month_was_and_when_the_loop_last_wrote(
        browser, tmp_path):
    """Two columns the fleet answer does not carry. Both are fetched beside
    the table rather than before it, so the counts are on the screen while
    they land; both come off endpoints the console already serves."""
    page = browser
    page.goto(mock_page(tmp_path, "fleet-extra").as_uri() + "#/fleet")
    page.until("STATE.screen === 'fleet'")
    page.until("STATE.heat30.data !== null && STATE.fleetTicks !== null")

    assert page.eval(TEXTS % "#fleet-table thead th") == \
        ["database", "open", "monitoring", "errors 30d", "last tick",
         "last journal"]
    assert page.eval(f"document.querySelectorAll('{SPARKS}').length") == 30, \
        "the month behind the count is a cell a day"
    assert page.eval(f"document.querySelectorAll('{SPARKS}.blank').length") \
        == len([back for back in HEAT_SILENT[DB_NAME] if back < 30]), \
        "and a day no digest covers is the hatch the heat card draws"

    assert page.eval(f"document.querySelector('{FLEET_TICK}')"
                     ".getAttribute('href')") == "#/run/" + RUN_ID, \
        "the newest tick that wrote about this database is an address"
    said = page.eval(f"document.querySelector('{FLEET_TICK}').textContent")
    assert said == f"{stamp_words('2026-08-31T08:15')} · {RUN_ID[:7]}", \
        f"which says when it ran and which tick it was, said {said!r}"
    assert page.eval(TEXTS % "#fleet-table tbody tr:nth-child(2) td.tick") \
        == ["—"], "a database no tick in the window wrote about says so"

    page.click(FLEET_TICK)
    page.until("STATE.screen === 'run'")
    assert hash_of(page) == "#/run/" + RUN_ID


@needs_chrome
def test_the_fleet_table_stands_when_either_of_its_two_extras_is_refused(
        browser, tmp_path):
    """Neither column is what the operator opened the fleet for, so a refusal
    takes the column away and nothing else: no notice, no empty screen."""
    page = browser
    for name, refused in (("no-heat", ["/api/heat"]),
                          ("no-ticks", ["/api/agents"])):
        page.goto(mock_page(tmp_path, name, refuse=refused).as_uri()
                  + "#/fleet")
        page.until("STATE.screen === 'fleet'")
        page.until("document.querySelectorAll('#fleet-table tbody tr')"
                   ".length === 2")
        assert page.eval("document.getElementById('notice').hidden"), \
            f"a refused {refused[0]} leaves the fleet standing and silent"
        assert page.eval(TEXTS % "#fleet-table tbody tr:nth-child(1) td.num") \
            == ["1", "1", "4"], "every count the fleet answer carried is drawn"


@needs_chrome
def test_the_page_boots_onto_the_address_it_was_opened_at(browser, tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "reload").as_uri() + "#/db/" + DB_NAME)
    page.until("STATE.screen === 'db'")
    assert page.eval("STATE.db.db") == DB_NAME, \
        "the hash named the database and the page fetched that one"
    assert hash_of(page) == "#/db/" + DB_NAME, \
        "and the address the operator arrived on survives the first render"
    assert page.eval("window.__requests[0].url") == "/api/db?name=" + DB_NAME, \
        "the database is the first thing the page asked for, not the queue"

    page.eval("location.hash = '#/nonsense'")
    page.until("STATE.screen === 'queue'")
    assert hash_of(page) == "#/queue", \
        "an address no route parses lands on the queue and says so"


@needs_chrome
def test_a_rendered_page_opens_at_its_address_and_its_own_links_navigate(
        browser, tmp_path):
    """The one screen that inserts a server string as markup. What is proved
    here is that the rendered elements reach the DOM, that a heading permalink
    is a jump and not a route, and that a `[[link]]` the server turned into a
    `#/page/` href navigates the way the backlinks do."""
    page = browser
    page.goto(mock_page(tmp_path, "wikipage").as_uri() + "#/page/" + PAGE_PATH)
    page.until("STATE.screen === 'wikipage'")
    assert hash_of(page) == "#/page/" + PAGE_PATH, \
        "the address the operator arrived on survives the first render"
    assert page.eval("window.__requests[0].url") \
        == "/api/page?path=" + urllib.parse.quote(PAGE_PATH, safe=""), \
        "the page is the first thing the page asked for, not the queue"

    for selector, what in (("h2", "a heading"), ("table", "a table"),
                           ('a[href^="#/page/"]', "an internal link")):
        assert page.eval("document.querySelectorAll('#wikipage-body %s')"
                         ".length" % selector) == 1, \
            f"the rendered body carries {what}"
    assert page.eval(
        "Array.from(document.querySelectorAll('#wikipage-backlinks a'))"
        ".map(a => a.getAttribute('href'))") \
        == ["#/page/index.md", f"#/page/databases/{DB_NAME}.md"], \
        "a backlink is an address, so it renders as an anchor"

    page.click("#wikipage-body .anchor")
    page.until("location.hash === '#wikipage--fleet-report-2026-08-30'")
    assert page.eval("STATE.screen") == "wikipage", \
        "a heading permalink is a jump inside the body, never a route"
    assert page.eval("STATE.wikipage.path") == PAGE_PATH

    page.click('#wikipage-body a[href^="#/page/"]')
    page.until("STATE.wikipage.path === 'databases/%s.md'" % DB_NAME)
    assert page.eval("STATE.screen") == "wikipage"
    assert hash_of(page) == f"#/page/databases/{DB_NAME}.md", \
        "a link the renderer wrote reaches the page it names"


STBY_NAME = f"{DB_NAME}_stby"

#: `wire.change_row_json` rows as a day's report carries them: one fleet
#: timeline, a database whose digest page the revision holds and one whose
#: page it does not, and a message holding markup that must stay text.
DAY_CHANGES = [
    {"db": DB_NAME, "day": "2026-08-30", "ts": "2026-08-30T02:10:00Z",
     "rule": "alter_system_set", "count": 9,
     "message": "ALTER SYSTEM SET log_archive_dest_state_2=DEFER;",
     "page": f"digests/{DB_NAME}/2026-08-30.md"},
    {"db": STBY_NAME, "day": "2026-08-30", "ts": "2026-08-30T03:00:00Z",
     "rule": "mrp_start", "count": 1,
     "message": "MRP0 <b>started</b>", "page": ""},
]


def change_cells(page):
    return page.eval(
        "Array.from(document.querySelectorAll('#wikipage-changes-list li'))"
        ".map(li => Array.from(li.children).map(c => c.textContent))")


@needs_chrome
def test_a_days_changes_are_a_strip_above_the_page_that_opens_each_digest(
        browser, tmp_path):
    """The day's report carries what the operators did, above its body, in
    the row shape the daily page's strip draws: clock, database, rule with
    its count, the line. A database whose digest page exists is a route to
    it; a page with no changes draws no strip at all."""
    page = browser
    pages = [{**PAGE, "changes": DAY_CHANGES}, PAGES[1]]
    page.goto(mock_page(tmp_path, "changes", pages=pages).as_uri()
              + "#/page/" + PAGE_PATH)
    page.until("STATE.screen === 'wikipage'")
    assert page.eval("document.getElementById('wikipage-changes').hidden") \
        is False
    assert change_cells(page) == [
        ["02:10:00", DB_NAME, "alter_system_set ×9",
         "ALTER SYSTEM SET log_archive_dest_state_2=DEFER;"],
        ["03:00:00", STBY_NAME, "mrp_start", "MRP0 <b>started</b>"]], \
        "a message is text, never markup"
    assert page.eval(
        "Array.from(document.querySelectorAll('#wikipage-changes-list a'))"
        ".map(a => a.getAttribute('href'))") \
        == [f"#/page/digests/{DB_NAME}/2026-08-30.md"], \
        "only the change whose digest page the revision holds is a route"
    assert page.eval(
        "document.getElementById('wikipage-changes')"
        ".compareDocumentPosition(document.getElementById('wikipage-body'))"
        " & Node.DOCUMENT_POSITION_FOLLOWING") > 0, \
        "the changes are read before the page they sit on"

    page.eval("location.hash = "
              + json.dumps(f"#/page/databases/{DB_NAME}.md"))
    page.until("STATE.wikipage.path === 'databases/%s.md'" % DB_NAME)
    assert page.eval("document.getElementById('wikipage-changes').hidden") \
        is True, "a page with no changes draws no strip"


@needs_chrome
def test_a_busy_day_stops_at_the_daily_pages_cap_and_says_how_many_it_left(
        browser, tmp_path):
    many = [{**DAY_CHANGES[0], "ts": f"2026-08-30T02:{at:02d}:00Z"}
            for at in range(daily_html.MAX_CHANGE_ROWS + 5)]
    page = browser
    page.goto(mock_page(tmp_path, "busy", pages=[{**PAGE, "changes": many}])
              .as_uri() + "#/page/" + PAGE_PATH)
    page.until("STATE.screen === 'wikipage'")
    cells = change_cells(page)
    assert len(cells) == daily_html.MAX_CHANGE_ROWS + 1
    assert cells[-1] == [], "the overflow line is a sentence, not a row"
    assert page.eval("document.querySelector('#wikipage-changes-list "
                     "li.more').textContent").startswith("+5 more")


@needs_chrome
def test_the_search_screen_reloads_its_term_and_opens_what_it_can(browser,
                                                                  tmp_path):
    page = browser
    term = SEARCH["query"]
    page.goto(mock_page(tmp_path, "search").as_uri()
              + "#/search/" + urllib.parse.quote(term))
    page.until("STATE.screen === 'search'")
    assert page.eval("document.getElementById('search-q').value") == term, \
        "the box shows the term the address searched for"
    assert page.eval("window.__requests[0].url") \
        == "/api/search?q=" + urllib.parse.quote(term)
    assert page.eval(
        "document.querySelectorAll('#search-hits button').length") == 3

    disabled = ("Array.from(document.querySelectorAll('#search-hits button'))"
                ".map(n => n.disabled)")
    assert page.eval(disabled) == [False, False, False], \
        "every page renders now, so no hit is a row that cannot be opened"

    page.click("#search-hits li:nth-child(3) button")
    page.until("STATE.screen === 'wikipage'")
    assert hash_of(page) == "#/page/" + PAGE_PATH, \
        "a hit that is neither a database nor an incident opens as a page"

    page.eval("location.hash = "
              + json.dumps("#/search/" + urllib.parse.quote(term)))
    page.until("STATE.screen === 'search'")
    page.click("#search-hits li:nth-child(1) button")
    page.until("STATE.screen === 'db'")
    assert hash_of(page) == "#/db/" + DB_NAME, \
        "a hit on a database page opens that database"


@needs_chrome
def test_the_search_screen_asks_for_nothing_until_a_term_is_typed(browser,
                                                                  tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "search-empty").as_uri())
    page.until("STATE.screen === 'queue'")
    page.until("STATE.heat.data !== null")
    page.click("#nav-search")
    page.until("STATE.screen === 'search'")
    assert page.eval("window.__requests.map(r => r.url)") \
        == ["/api/incidents", "/api/heat?days=30"], \
        "an empty needle is a 400 at the endpoint, so the page does not send it"
    assert not page.eval("document.getElementById('search-empty').hidden")
    assert "Type a term" in page.eval(
        "document.getElementById('search-empty').textContent")


TEXTS = ("Array.from(document.querySelectorAll('%s')).map(n => n.textContent)")


@needs_chrome
def test_the_run_screens_tell_every_status_apart_with_the_colour_off(browser,
                                                                    tmp_path):
    """The one screen whose whole job is reading a machine's work back. What
    is proved here is that each status carries its own glyph beside its own
    word, that an agent stage is marked as one, that an inferred status says
    so, and that a digest the wiki holds is an address."""
    page = browser
    page.goto(mock_page(tmp_path, "runs").as_uri() + "#/runs")
    page.until("STATE.screen === 'runs'")
    assert hash_of(page) == "#/runs", \
        "the address the operator arrived on survives the first render"
    assert page.eval(
        "document.getElementById('nav-runs').getAttribute('aria-current')") \
        == "page", "the strip marks where the operator is"
    assert stamp_words(RUNS["generated_at"]) in strip_text(page, "runs-strip"), \
        "the strip names when the answer was generated, not a wiki revision"

    fresh = strip_text(page, "runs-freshness")
    assert "stale" in fresh and "never run" in fresh, \
        f"a watermark nothing proves ran is its own answer, said {fresh!r}"
    assert "threshold 26h" in fresh, "and each says what it is judged against"
    coverage = strip_text(page, "runs-coverage")
    assert "451/2000" in coverage \
        and stamp_words("2026-07-27T04:15:00Z") in coverage, \
        "every coverage row is drawn, spans included"
    assert "truncated" in coverage, "and a full file says history is gone"
    assert page.eval(
        "document.querySelectorAll('#runs-rows tbody tr.runrow').length") == 2
    assert page.eval("document.querySelector('#runs-rows tr.runrow')"
                     ".querySelectorAll('.mini span').length") == 7, \
        "a run row carries one glyph per stage of the timeline"

    page.click("#runs-rows tr.runrow .tid")
    page.until("STATE.screen === 'run'")
    assert hash_of(page) == "#/run/" + RUN_ID
    assert page.eval("window.__requests[window.__requests.length - 1].url") \
        == "/api/run?id=" + RUN_ID
    assert page.eval(
        "document.querySelectorAll('#run-timeline .stage-box').length") == 7, \
        "the timeline is always all seven stages, absent ones included"

    badges = page.eval(TEXTS % "#run-timeline .badge")
    drawn = {}
    for stage, text in zip(STAGES, badges):
        drawn.setdefault(stage["status"], set()).add(text)
    mixed = sorted(name for name, texts in drawn.items() if len(texts) > 1)
    assert not mixed, f"{mixed} draw themselves two different ways"
    assert len({next(iter(texts)) for texts in drawn.values()}) == len(drawn), \
        f"two statuses render the same badge text: {badges}"
    for status in drawn:
        assert status in next(iter(drawn[status])), \
            f"the {status} badge does not carry its own word"

    assert page.eval("document.querySelectorAll("
                     "'#run-timeline .inferred:not([hidden])').length") \
        == INFERRED_STAGES, \
        "a status derived from the shape of the record says it was inferred"
    marks = page.eval(TEXTS % "#run-timeline .kindmark")
    assert marks == ["code" if not s["agentic"] else "agent" for s in STAGES], \
        f"an agent stage is marked apart from deterministic code, said {marks}"
    assert len(set(marks)) == 2, "and the two marks are different words"

    reasons = page.eval(TEXTS % "#run-dbs li:nth-child(1) .reasons span")
    assert reasons == ["new_notable_group (groups=3, top_code=ORA-00600)",
                       "volume_delta (delta_pct=180)"], \
        f"each reason carries the evidence behind it, said {reasons}"
    usage = page.eval(TEXTS % "#run-dbs .usage")
    assert "48210 in / 1620 out" in usage[0] and "$0.0413" in usage[0]

    agents = page.eval(TEXTS % "#run-agents tbody tr.stage td.tstage")
    assert agents == ["ingest", "report"], \
        "one row per agent stage the ledger holds for this run, oldest first"
    assert page.eval("Array.from(document.querySelectorAll("
                     "'#run-agents td.trace a')).map((a) => a.href)") \
        == ["https://langfuse.example.invalid/project/p/traces/ev-run-1"], \
        "a stage with a trace is one click from it"
    assert page.eval(TEXTS % "#run-agents td.trace .nolink") \
        == ["unavailable"], "and a stage without one says so in words"
    assert usage[1] == "tokens unknown · cost unknown", \
        "a database nothing was measured on reads unknown, never a 0"
    assert page.eval(
        "document.querySelector('#run-dbs a').getAttribute('href')") \
        == "#/page/" + DIGEST_PAGE, \
        "a digest the wiki renders is an address into the page screen"
    assert page.eval("document.querySelectorAll('#run-dbs a').length") == 1, \
        "and one with no rendered page stays text"

    page.click("#nav-runs")
    page.until("STATE.screen === 'runs'")
    assert hash_of(page) == "#/runs", "the nav strip reaches runs from a run"


@needs_chrome
def test_the_trend_draws_every_day_and_never_a_bar_for_a_measure_nobody_took(
        browser, tmp_path):
    """A number always in the text and a bar only beside a measured one: the
    bar is decoration, and a zero-length one would read as a measured zero."""
    page = browser
    page.goto(mock_page(tmp_path, "trend").as_uri() + "#/runs")
    page.until("STATE.screen === 'runs'")

    days = page.eval(TEXTS % "#runs-trend tbody td.db")
    assert days == [day_words(row["day"]) for row in TREND], \
        f"the calendar is drawn whole, gaps included, said {days}"
    quiet = page.eval(TEXTS % "#runs-trend tbody tr:nth-child(2) td")
    assert quiet[1:3] == ["0", "0"], "a day nothing ran on counts zero runs"
    assert quiet[4:] == ["unmeasured"] * 3, \
        f"a measure with no stage behind it reads as a word, said {quiet}"
    assert page.eval("document.querySelectorAll("
                     "'#runs-trend tbody tr:nth-child(2) .bar').length") == 0
    assert page.eval("document.querySelectorAll("
                     "'#runs-trend tbody tr:nth-child(1) .bar').length") > 0
    assert page.eval("document.querySelector("
                     "'#runs-trend .bar').getAttribute('aria-hidden')") \
        == "true", "the bar is decoration and is not announced"

    last = page.eval(TEXTS % "#runs-trend tbody tr:nth-child(3) td")
    assert "12900 · 2 of 2" in last[5], \
        f"a sum crosses beside the stages that reported it, said {last}"
    assert last[6] == "unmeasured", \
        "one currency being unmeasured never silences the one beside it"


@needs_chrome
def test_a_failure_group_says_what_is_open_and_never_that_it_recovered(
        browser, tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "failures").as_uri() + "#/runs")
    page.until("STATE.screen === 'runs'")

    rows = page.eval(TEXTS % "#runs-failures tbody tr")
    assert len(rows) == len(FAILURES)
    assert ALERTED_FP in rows[0] and "43 assessments" in rows[0], \
        f"the open alert rides beside the group's own count, said {rows[0]}"
    assert "23" in rows[0] and "retry, run" in rows[0], \
        "and the commands ride under the fingerprint"
    assert "provider unreachable" in rows[0], \
        "the recorded message is what tells two failures of one category apart"
    alerts_drawn = page.eval(
        TEXTS % "#runs-failures tbody tr:nth-child(2) td")
    assert alerts_drawn[-1] == "—", \
        f"a group no alert is open about says nothing, said {alerts_drawn}"


#: Two days of `BIG_RUNS`: one the loop got through and one it did not, both
#: old enough to be folded at first paint.
BIG_BAD_DAY = "2026-08-29"
BIG_CLEAN_DAY = "2026-08-27"

#: How many of a bad day's runs failed, which is what `big_runs` stamps.
BIG_FAILED_PER_DAY = 2

OPEN_DAYS = "#runs-rows tr.day:not(.folded)"


@needs_chrome
def test_a_month_of_runs_opens_two_days_and_folds_every_other_one(browser,
                                                                  tmp_path):
    """Folding only the days that went right folds nothing on this payload:
    two days in three carry a failed health or research run, and opening
    them all is the twenty-three-thousand-pixel page the fold exists to
    prevent. The runs screen opens the two newest days and says on each
    folded row how many of that day's runs failed."""
    page = browser
    page.goto(mock_page(tmp_path, "many-runs", runs=BIG_RUNS).as_uri()
              + "#/runs")
    page.until("STATE.screen === 'runs'")
    assert page.eval("document.querySelectorAll('#runs-rows tr.day').length") \
        == BIG_DAYS, "one day row per day the logs hold"
    open_days = page.eval(f"document.querySelectorAll('{OPEN_DAYS}').length")
    assert open_days <= 2, \
        f"at most the two newest days are open at first paint, said {open_days}"
    open_rows = page.eval(
        "document.querySelectorAll('#runs-rows tr.runrow').length")
    assert open_rows == BIG_PER_DAY * 2, \
        f"and only those two days draw their runs, said {open_rows}"

    tall = page.eval("document.documentElement.scrollHeight")
    assert tall < 6000, f"the page is {tall}px tall rather than 23000"
    listed = page.eval(
        "document.getElementById('runs-rows').getBoundingClientRect().height")
    assert listed < 2600, \
        f"and the run list itself is {listed}px of that, not all of it"

    bad = f'#runs-rows tr.day[data-day="{BIG_BAD_DAY}"]'
    assert page.eval(f"document.querySelector('{bad} .sum').textContent") == \
        f"{BIG_PER_DAY} runs · {BIG_FAILED_PER_DAY} failed · " \
        f"{(BIG_PER_DAY - BIG_FAILED_PER_DAY) * 2} ingested", \
        "a folded day that holds failures counts them on the row"
    assert page.eval(f"document.querySelector('{bad} .sum .t-bad')"
                     ".textContent") == f"{BIG_FAILED_PER_DAY} failed", \
        "and the count is the one part of the row drawn in red"
    clean = f'#runs-rows tr.day[data-day="{BIG_CLEAN_DAY}"] .sum'
    assert page.eval(f"document.querySelector('{clean} .t-ok').textContent") \
        == "all ok", "a day nothing failed on says so instead"

    page.click(f"{bad} .dayx")
    page.until("document.querySelectorAll('#runs-rows tr.runrow').length === "
               + str(BIG_PER_DAY * 3))
    assert page.eval(f"document.querySelector('{bad} .dayx')"
                     ".getAttribute('aria-expanded')") == "true", \
        "and the reader can still open it"

    page.click("#runs-rows tr.runrow td:nth-child(2)")
    page.until("STATE.screen === 'run'")
    assert hash_of(page) == "#/run/" + RUN_ID, \
        "the row is the control the card was"


@needs_chrome
def test_a_two_hundred_character_failure_message_keeps_one_line(browser,
                                                                tmp_path):
    """The recorded message is what tells two failures of one category apart,
    and it is also a stack line. The cell holds a readable clause of it and
    the whole string is on the pointer."""
    page = browser
    page.goto(mock_page(tmp_path, "long-failures", runs=BIG_RUNS).as_uri()
              + "#/runs")
    page.until("STATE.screen === 'runs'")
    cell = "#runs-failures tbody tr:nth-child(1) td.msg"
    assert page.eval(f"document.querySelector('{cell}').getAttribute('title')")\
        == BIG_MESSAGE % 0, "the pointer carries every character of it"
    drawn, held = page.eval(
        f"[document.querySelector('{cell} .clip').getBoundingClientRect().width,"
        f" document.querySelector('{cell} .clip').scrollWidth]")
    assert drawn < held, "and the cell draws less than it holds"
    assert drawn < 500, f"one line of it, not all of it, said {drawn}"
    held_by, shown = page.eval(
        "(() => {const box ="
        " document.querySelector('#runs-failures .tablewrap');"
        " return [box.scrollWidth, box.clientWidth];})()")
    assert held_by <= shown, \
        f"and the table needs no scroll region of its own, said {held_by}"


@needs_chrome
def test_a_run_is_drawn_beside_its_predecessor_and_never_a_difference(
        browser, tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "compare").as_uri() + "#/run/" + RUN_ID)
    page.until("STATE.screen === 'run'")

    assert page.eval("document.getElementById('run-no-compare').hidden") \
        is True
    heads = page.eval(TEXTS % "#run-compare thead th")
    assert heads == ["", "this run", PREVIOUS_RUN], \
        f"the column is named by the run it holds, said {heads}"
    cost = page.eval(TEXTS % "#run-compare tbody tr:last-child td")
    assert cost[0] == "cost"
    assert cost[1] == "0.0413 · 1 of 2" and cost[2] == "unmeasured", \
        f"two columns as measured, and no third that subtracts them, {cost}"
    assert "delta" not in strip_text(page, "run-compare").lower()

    alone = json.loads(json.dumps(RUN))
    alone["compare"] = None
    page.goto(mock_page(tmp_path, "alone", run=alone).as_uri()
              + "#/run/" + RUN_ID)
    page.until("STATE.screen === 'run'")
    assert page.eval("document.getElementById('run-no-compare').hidden") \
        is False
    assert page.eval("document.getElementById('run-compare').children.length") \
        == 0, "and the region it would have filled is empty"


@needs_chrome
def test_a_deep_link_is_an_anchor_only_while_it_is_available(browser,
                                                             tmp_path):
    """A dead anchor teaches the operator to click into a refusal, so every
    state but `available` renders as text naming the state and its reason."""
    page = browser
    page.goto(mock_page(tmp_path, "deeplinks").as_uri()
              + "#/page/" + PAGE_PATH)
    page.until("STATE.screen === 'wikipage'")
    assert page.eval(
        "Array.from(document.querySelectorAll('#wikipage-links a'))"
        ".map(a => [a.getAttribute('href'), a.textContent])") \
        == [[LIVE_LINK["url"], LIVE_LINK["label"]]], \
        "the available link, and only it, is an anchor"
    assert page.eval("document.querySelectorAll('#wikipage-links "
                     "a[href=\"null\"], #wikipage-links a:not([href])')"
                     ".length") == 0, "no state renders as a dead anchor"
    text = page.eval("document.getElementById('wikipage-links').textContent")
    assert DEAD_LINK["label"] in text
    assert "missing" in text and DEAD_LINK["note"] in text
    assert LIVE_LINK["description"] in text, \
        "the sentence shown before navigation is on the page, not in a title"

    page.eval("location.hash = '#/run/" + RUN_ID + "'")
    page.until("STATE.screen === 'run'")
    assert page.eval(
        "document.querySelectorAll('#run-links a').length") == 1
    assert DEAD_LINK["note"] in page.eval(
        "document.getElementById('run-links').textContent")


ROWS = "#review-findings tbody tr:not(.day)"

ITEM_GLYPHS = TEXTS % (ROWS + " .badge .mk")

DONE_FOLD = '#review-findings tr.day[data-day="done"]'


def row_of(fingerprint):
    return f'#review-findings tr[data-fp="{fingerprint}"]'


def open_rows(page):
    return page.eval(f"document.querySelectorAll('{ROWS}').length")

NO_NOTE_WEEK = dict(REVIEW, synthesis=None, synthesis_error="timeout")


@needs_chrome
def test_the_inbox_opens_a_week_and_records_what_was_read(browser, tmp_path):
    """The inbox walk: the weeks, one week's findings, an acknowledgement and
    a suppression, with the address checked at every step and the two POST
    bodies read off the shim. What it proves is the page's own behaviour —
    that the ack state it draws is the one the server answered with, and that
    nothing else about the published week moves when it does."""
    page = browser
    page.goto(mock_page(tmp_path, "inbox").as_uri())
    page.until("STATE.screen === 'queue'")

    page.click("#nav-inbox")
    page.until("STATE.screen === 'inbox'")
    assert hash_of(page) == "#/inbox", \
        "the address the operator arrived on survives the render"
    assert page.eval(
        "document.getElementById('nav-inbox').getAttribute('aria-current')") \
        == "page", "the strip marks where the operator is"
    strip = strip_text(page, "inbox-strip")
    assert stamp_words(INBOX["generated_at"]) in strip \
        and "2 weeks held" in strip
    assert "read model behind HEAD" not in strip, \
        "this envelope reads .state and names no revision to be behind"
    assert page.eval(
        "document.querySelectorAll('#inbox-rows tbody tr').length") == 2
    weeks = page.eval(TEXTS % "#inbox-rows td.week a")
    assert weeks == [REVIEW_ID, LAST_REVIEW_ID], "newest first, as sent"
    notes = page.eval(TEXTS % "#inbox-rows td.note .badge")
    assert notes == ["written", "failed · timeout"], \
        "the note column says whether the model wrote one and names the "\
        f"category when it did not, said {notes!r}"

    page.click("#inbox-rows td.week a")
    page.until("STATE.screen === 'review'")
    assert hash_of(page) == "#/review/" + REVIEW_ID
    assert last_request(page)["url"] == "/api/review?id=" + REVIEW_ID
    week = strip_text(page, "review-strip")
    assert REV[:7] in week, "the strip names the revision the week was judged at"
    assert "read model behind HEAD" not in week, \
        "there is no head beside source_revision to compare it against"
    assert page.eval(TEXTS % "#review-tiles dt") == [
        "to look at", "new this week", "resolved", "dealt with", "left out"]
    assert page.eval(TEXTS % "#review-tiles .v") \
        == ["2", "1", "0", "4", "2"], \
        "the sentence the screen used to open with is these five numbers"
    assert page.eval(TEXTS % "#review-tiles .s") == [
        "1 high · 1 normal", "0 changed · 1 carried", "since the last review",
        "3 acknowledged · 1 suppressed", "2 capped · 0 shadowed"], \
        "every count the envelope carries is on the screen, capped and " \
        "shadowed included"
    assert page.eval(
        "document.querySelectorAll('#review-tiles .tile.hot').length") == 1, \
        "a week with a high band in it is the one tile drawn hot"

    note = page.eval("document.getElementById('review-synthesis').textContent")
    assert REVIEW["synthesis"]["summary"] in note
    assert "the selection is deterministic" in note, \
        "the note is captioned so nobody reads it as the finding"
    assert page.eval(
        "Array.from(document.querySelectorAll('#review-synthesis a'))"
        ".map(a => a.getAttribute('href'))") == [f"#/page/{PATH}"], \
        "a theme's citation is an address into the wiki, and the note's own " \
        "are not the same list printed twice"

    assert open_rows(page) == 2
    assert page.eval(TEXTS % "#review-findings tr.day td") \
        == ["1 high", "1 normal"], \
        "both bands are here, so each says which one it is"
    glyphs = page.eval(ITEM_GLYPHS)
    assert len(set(glyphs)) == 4, \
        f"each band and each movement carries its own mark, said {glyphs}"
    words = page.eval(TEXTS % (ROWS + " .badge .word"))
    assert words == ["high", "carried", "normal", "new"], \
        f"and its own word beside it, said {words}"
    why = page.eval(TEXTS % (ROWS + " .what .why"))
    assert why == ["stale open · open 44 d, untouched",
                   "cross db · on 4 databases: cdb1, cdb1_stby, dgnonc, "
                   "emcdb"], \
        f"a reason is read as a sentence and not as the units the detector " \
        f"measured in, said {why}"
    assert page.eval("Array.from(document.querySelectorAll("
                     f"'{ROWS} .what .why')).map(n => n.title)") == [
        "stale_open (age_days=44.18, opened=2026-07-28T09:00:00Z, updated=)",
        "multi_db (code=ORA-00600, count=4, "
        "databases=cdb1,cdb1_stby,dgnonc,emcdb)"], \
        "and the evidence it was drawn from is still there to be read"
    assert page.eval(
        f"document.querySelector('{row_of(STALE_FP)} .what a')"
        ".getAttribute('href')") == f"#/page/{PATH}"
    assert page.eval(
        f"document.querySelector('{row_of(CROSS_FP)} .what a')") is None, \
        "a finding with no wiki page is a name and not a dead address"
    assert page.eval(f"document.querySelector('{row_of(CROSS_FP)} .db')"
                     ".textContent") == "the fleet", \
        "a finding no one database owns says so"

    went = page.eval(TEXTS % "#review-deliveries .badge .word")
    assert went == ["delivered", "failed"], \
        f"both attempts are on the screen with their status word, said {went}"
    where = page.eval(
        "document.getElementById('review-deliveries').textContent")
    assert "inbox" in where and "ops" in where and "email" in where, \
        f"each row names its recipient and the channel that carried it, " \
        f"said {where!r}"
    assert "timeout" in where, \
        f"and a failed row names the category it failed with, said {where!r}"
    assert page.eval("document.getElementById('review-nowhere').hidden") \
        is True, "the empty line is for a week that reached nobody"

    page.click(f"{row_of(STALE_FP)} .ack")
    page.until(f"document.querySelector('{DONE_FOLD}') !== null")
    acked = last_request(page)
    assert acked["method"] == "POST" and acked["url"] == "/api/inbox"
    assert acked["body"] == {"fingerprint": STALE_FP, "action": "acknowledge"}, \
        "an acknowledgement carries no duration; it records what was read"
    assert open_rows(page) == 1 and page.eval(
        f"document.querySelector('{row_of(STALE_FP)}')") is None, \
        "the row somebody has now answered for leaves what is left to look at"
    assert page.eval(
        f"document.querySelector('{DONE_FOLD} .nm').textContent") \
        == "1 already dealt with", "and is one line saying it is under there"

    page.click(f"{DONE_FOLD} .dayx")
    page.until(f"document.querySelector('{row_of(STALE_FP)}') !== null")
    said = page.eval(f"document.querySelector('{row_of(STALE_FP)} .said')"
                     ".textContent")
    assert said == "acked " + day_words(ACKED_AT), \
        f"the row shows the ack the server answered with, said {said!r}"
    assert stamp_words(ACKED_AT) in page.eval(
        f"document.querySelector('{row_of(STALE_FP)} .said').title") \
        and ACTOR in page.eval(
            f"document.querySelector('{row_of(STALE_FP)} .said').title"), \
        "with the instant and who recorded it behind the day"
    assert page.eval(f"document.querySelector('{row_of(STALE_FP)} .ack')"
                     ".textContent") == "Acknowledged"

    page.eval(f"document.querySelector('{row_of(CROSS_FP)} .days').value "
              "= '30'")
    page.click(f"{row_of(CROSS_FP)} .hush")
    page.until(f"document.querySelector('{row_of(CROSS_FP)} .said')"
               ".textContent.indexOf('quiet until') >= 0")
    hushed = last_request(page)
    assert hushed["body"] == {"fingerprint": CROSS_FP, "action": "suppress",
                              "days": 30}, \
        "the horizon the operator chose is the one that is posted"
    assert page.eval(
        f"document.querySelector('{row_of(CROSS_FP)} .said').textContent") \
        == "quiet until " + day_words(SUPPRESSED_TO)
    assert open_rows(page) == 2, \
        "a group the reader opened stays open while the rows move into it"
    assert page.eval(
        "STATE.review.findings.map(f => f.ack.acknowledged_at)") \
        == [ACKED_AT, None], "one item moved and the other did not"
    assert hash_of(page) == "#/review/" + REVIEW_ID, \
        "recording something is not navigation"

    sent = page.eval("window.__requests.map(r => r.method + ' ' + r.url)")
    assert sent.count("POST /api/inbox") == 2, \
        f"one POST per click and no more, sent {sent}"
    assert sent.count("GET /api/review?id=" + REVIEW_ID) == 1, \
        f"the server answered what acks.json now holds, so the week is not " \
        f"re-fetched for it; sent {sent}"


@needs_chrome
def test_a_week_whose_model_said_nothing_still_shows_every_finding(browser,
                                                                   tmp_path):
    """The model's silence is a fact about the model, not a gap in the week:
    the deterministic selection is the inbox item and is never hidden for
    it."""
    page = browser
    page.goto(mock_page(tmp_path, "no-note", week=NO_NOTE_WEEK).as_uri()
              + "#/review/" + REVIEW_ID)
    page.until("STATE.screen === 'review'")
    note = page.eval("document.getElementById('review-synthesis').textContent")
    assert "No covering note" in note and "timeout" in note, \
        f"the category the failure was classified as is named, said {note!r}"
    assert open_rows(page) == 2, \
        "and every finding is still on the screen"
    assert page.eval("window.__requests[0].url") == "/api/review?id=" + REVIEW_ID


def big_review(browser, tmp_path, name, **over):
    """The review screen on the live-shaped week, painted and settled."""
    page = browser
    week = over.pop("week", BIG_REVIEW)
    page.goto(mock_page(tmp_path, name, week=week, **over).as_uri()
              + "#/review/" + week["review_id"])
    page.until("STATE.screen === 'review'")
    return page


@needs_chrome
def test_a_live_sized_week_opens_with_its_numbers_and_who_it_touched(browser,
                                                                     tmp_path):
    """What the screen is for, before any of it is read."""
    page = big_review(browser, tmp_path, "rvtiles")
    assert page.eval(TEXTS % "#review-tiles .v") == ["19", "5", "4", "3", "33"]
    assert page.eval(TEXTS % "#review-tiles .s") == [
        "12 high · 7 normal", "0 changed · 14 carried",
        "since the last review", "2 acknowledged · 1 suppressed",
        "21 capped · 12 shadowed"], \
        "the counts the old sentence buried, capped and shadowed included"
    assert page.eval(
        "document.querySelectorAll('#review-tiles .tile.hot').length") == 1

    chips = page.eval(TEXTS % "#review-dbs .dbchip")
    assert chips == ["cdb1 6", "cdb1_stby 4", "dgnonc 3", "emcdb 3",
                     "dgnonc_s 2", "the fleet 1"], \
        f"the loudest database first, and ties by name, said {chips}"
    assert page.eval("document.querySelector('#review-dbs .dbchip')"
                     ".tagName") == "A"
    assert page.eval("document.querySelectorAll('#review-dbs a').length") \
        == 5, "the fleet is a name and not an address: there is no screen " \
              "for it"
    assert page.eval("document.getElementById('review-kinds').textContent") \
        == "stale open 15 · worsening 2 · cross db 1 · recurring 1"
    page.click("#review-dbs .dbchip")
    page.until("location.hash === '#/db/cdb1'")


@needs_chrome
def test_the_note_cites_each_page_once_and_says_how_much_it_read(browser,
                                                                 tmp_path):
    """The themes above the card used to list the same addresses the card
    ended in, and the set is counted once."""
    page = big_review(browser, tmp_path, "rvnote")
    assert page.eval("document.querySelectorAll('#review-synthesis .tt')"
                     ".length") == 3
    assert page.eval(TEXTS % "#review-synthesis .tt")[0] == \
        BIG_THEMES[0]["title"]
    chips = page.eval(TEXTS % "#review-synthesis .chips a")
    assert chips[0] == "2026-08-01-cdb1-oracle-errors", \
        f"a chip is the page's own name and not its address, said {chips[0]}"
    assert len(chips) == 7, "each theme's citations and no others"
    long_chip = ("document.querySelector("
                 "'#review-synthesis .chips a[title$=\"%s.md\"]')"
                 % BIG_LONG)
    assert page.eval(long_chip + ".textContent").endswith("\u2026") \
        and page.eval(long_chip + ".title") \
        == f"incidents/{BIG_LONG}.md", \
        "a name too long for the column is cut, and the address it was cut " \
        "from is still on it"
    assert page.eval("document.querySelector('#review-synthesis .chips a')"
                     ".getAttribute('href')") \
        == "#/page/" + BIG_NOTE_REFS[0]
    foot = page.eval("document.querySelector('#review-synthesis .foot')"
                     ".textContent")
    assert foot.startswith("cites 7 pages · read 20 sections · 1 truncated"), \
        f"what the model was shown against what it leans on, said {foot!r}"

    assert page.eval("document.getElementById('review-cited')") is None, \
        "the note's own list is not the themes' citations printed again"
    page.click("#review-synthesis .foot button")
    page.until("document.getElementById('review-cited') !== null")
    assert page.eval(
        "document.querySelectorAll('#review-cited a').length") == 7
    assert page.eval("document.querySelector('#review-synthesis .foot button')"
                     ".textContent") == "Hide the pages cited"


@needs_chrome
def test_a_week_is_read_by_band_and_what_was_answered_for_folds_away(browser,
                                                                     tmp_path):
    """The bands are the order, and what somebody has already answered for
    is one line."""
    page = big_review(browser, tmp_path, "rvrows")
    assert open_rows(page) == 16, "the three already dealt with are not here"
    assert page.eval(TEXTS % "#review-findings tr.day td") \
        == ["10 high", "6 normal", "+3 already dealt with"], \
        "each group says how many rows are under it"
    bands = page.eval(TEXTS % (ROWS + " .band .word"))
    assert bands == ["high"] * 10 + ["normal"] * 6, \
        f"the loudest band first and never interleaved, said {bands}"
    moves = page.eval(TEXTS % (ROWS + " .moved .word"))
    assert moves == ["new"] * 4 + ["carried"] * 6 + ["new"] \
        + ["carried"] * 5, \
        f"and inside a band, what moved this week first, said {moves}"

    page.click(f"{DONE_FOLD} .dayx")
    page.until(f"document.querySelectorAll('{ROWS}').length === 19")
    done = page.eval(TEXTS % ("#review-findings tr.done .said"))
    assert done == ["acked " + day_words(ACKED_AT)] * 2 \
        + ["quiet until " + day_words(SUPPRESSED_TO)], \
        f"and each of them says what was said about it, said {done}"

    why = page.eval(TEXTS % (ROWS + " .what .why"))
    assert "worsening · 13 in 28 d, was 1" in why
    assert "stale open · open 44 d, untouched" in why
    assert "stale open · open 22 d, last touched 12 Aug" in why
    assert ("cross db · on 4 databases: cdb1, cdb1_stby, dgnonc, emcdb"
            in why)
    assert "recurring · 4 times in 14 d" in why, \
        "the span is the finding; the days inside it are the same fact again"
    fleet = row_of(BIG_FINDINGS[13]["fingerprint"])
    assert page.eval(f"document.querySelector('{fleet} .what a')") is None \
        and page.eval(f"document.querySelector('{fleet} .db').textContent") \
        == "the fleet", "a finding with no page of its own is a name"
    assert page.eval(f"document.querySelector('{fleet} .what .why').title") \
        == ("multi_db (code=ORA-00600, count=4, "
            "databases=cdb1,cdb1_stby,dgnonc,emcdb)"), \
        "with the evidence it was drawn from still on it"


@needs_chrome
def test_no_band_header_is_drawn_over_the_only_band_on_the_page(browser,
                                                               tmp_path):
    page = big_review(browser, tmp_path, "rvoneband", week=BIG_ONE_BAND)
    assert open_rows(page) == 10
    assert page.eval(TEXTS % "#review-findings tr.day td") \
        == ["+2 already dealt with"], \
        "the fold is still a row; the band it is under is not"


@needs_chrome
def test_a_live_sized_week_is_one_screen_and_a_half_at_1280(browser,
                                                            tmp_path):
    """Three thousand one hundred and eighty pixels for twelve findings was
    the whole complaint."""
    page = big_review(browser, tmp_path, "rvtall")
    tall = page.eval("Math.ceil(document.documentElement.scrollHeight)")
    assert tall < 2000, f"the review screen is {tall}px tall at 1280"


@needs_chrome
def test_a_week_the_workbench_no_longer_holds_lands_on_the_inbox(browser,
                                                                 tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "pruned").as_uri() + "#/review/2019-W01")
    page.until("STATE.screen === 'queue'")
    said = notice(page)
    assert "NO SUCH REVIEW" in said and "pruned to the newest" in said, \
        f"a week older than the retention is gone, and the page says so " \
        f"rather than showing an empty screen; said {said!r}"
    page.click("#nav-inbox")
    page.until("STATE.screen === 'inbox'")
    assert page.eval(
        "document.querySelectorAll('#inbox-rows tbody tr').length") == 2, \
        "the inbox still lists the weeks that are here"


INBOX_ROWS = "#inbox-rows tbody tr:not(.day)"

INBOX_FOLD = "#inbox-rows tbody tr.day .dayx"


def big_inbox(browser, tmp_path, name, **over):
    """The inbox on the quarter-sized fixture, painted and settled. The mock
    answers `/api/review` for one id, so it is pointed at the newest week and
    a click on the top row proves navigation rather than the 404 refusal."""
    page = browser
    over.setdefault("week", dict(REVIEW, review_id=BIG_INBOX_ID))
    page.goto(mock_page(tmp_path, name, inbox=BIG_INBOX, **over).as_uri()
              + "#/inbox")
    page.until("STATE.screen === 'inbox'")
    return page


@needs_chrome
def test_the_inbox_opens_with_the_newest_week_read_out(browser, tmp_path):
    """The five numbers a reader came for are the newest week's, and the tile
    that is red is red because that week has a high band and not because the
    inbox is long."""
    page = big_inbox(browser, tmp_path, "ibtiles")
    assert page.eval(TEXTS % "#inbox-tiles dt") == [
        "latest week", "to look at", "new this week", "resolved",
        "weeks held"]
    assert page.eval(TEXTS % "#inbox-tiles .v") \
        == [BIG_INBOX_ID, "12", "5", "3", "14"]
    assert page.eval(TEXTS % "#inbox-tiles .s") == [
        "generated " + stamp_words(BIG_INBOX["reviews"][0]["generated_at"]),
        "12 high · 0 normal", "2 changed · 5 carried", "since the week before",
        "back to " + BIG_INBOX_OLDEST]
    assert page.eval(
        "Array.from(document.querySelectorAll('#inbox-tiles .tile'))"
        ".map(n => n.classList.contains('hot'))") \
        == [False, True, False, False, False], \
        "the week with twelve high rows is the one thing coloured"


@needs_chrome
def test_eight_weeks_open_and_the_rest_of_the_quarter_folds(browser,
                                                            tmp_path):
    """Fourteen weeks are eight rows and one line saying what is under the
    rest, and the line opens the other six where the reader asks for them."""
    page = big_inbox(browser, tmp_path, "ibfold")
    assert page.eval(f"document.querySelectorAll('{INBOX_ROWS}').length") == 8
    said = page.eval(f"document.querySelector('{INBOX_FOLD}').textContent")
    assert "6 older weeks" in said and "back to " + BIG_INBOX_OLDEST in said, \
        f"the fold says how many weeks and how far back, said {said!r}"
    page.click(INBOX_FOLD)
    assert page.eval(f"document.querySelectorAll('{INBOX_ROWS}').length") == 14
    weeks = page.eval(TEXTS % "#inbox-rows td.week a")
    assert weeks == [row["review_id"] for row in BIG_INBOX["reviews"]], \
        "newest first, in the order the envelope sent them"


BAR_WIDTHS = """
Array.from(document.querySelectorAll('#inbox-rows tbody tr:not(.day)'))
  .map(tr => Array.from(tr.querySelectorAll('td.cbar i'))
    .reduce((wide, part) => wide + parseFloat(part.style.width), 0))
"""


@needs_chrome
def test_a_weeks_bar_is_measured_against_the_busiest_week_held(browser,
                                                               tmp_path):
    """The bar answers one question — is this week heavier than that one —
    so a longer bar is a bigger selection everywhere in the table, and the
    week that selected nothing draws no bar at all."""
    page = big_inbox(browser, tmp_path, "ibbars")
    page.click(INBOX_FOLD)
    widths = page.eval(BAR_WIDTHS)
    selected = [row["counts"]["selected"] for row in BIG_INBOX["reviews"]]
    assert len(widths) == len(selected)
    ranked = sorted(zip(selected, widths))
    assert all(one <= two for (_, one), (_, two) in zip(ranked, ranked[1:])), \
        f"a heavier week draws a longer bar, drawn {ranked!r}"
    assert ranked[0][1] == 0, "and the week that selected nothing draws none"

    quiet = selected.index(0)
    assert page.eval(
        f"document.querySelectorAll('{INBOX_ROWS}')[{quiet}]"
        ".querySelectorAll('td.cbar i').length") == 0, \
        "no length is drawn as no bar and never as a bar of no length"
    assert page.eval(
        f"document.querySelectorAll('{INBOX_ROWS}')[{quiet}]"
        ".querySelector('td.num').textContent") == "\u2014", \
        "and the count reads as the dash every other nought on the page does"


@needs_chrome
def test_a_week_says_whether_its_note_was_written_failed_or_never_asked_for(
        browser, tmp_path):
    """Three states and three different words. A week nothing was asked of is
    not a week that failed, and the column never says so."""
    page = big_inbox(browser, tmp_path, "ibnotes")
    page.click(INBOX_FOLD)
    notes = page.eval(TEXTS % "#inbox-rows td.note .badge")
    said = dict(zip([row["review_id"] for row in BIG_INBOX["reviews"]], notes))
    assert said["2026-W37"] == "failed · harness error"
    assert said["2026-W35"] == "failed · timeout"
    assert said[f"2026-W{BIG_INBOX_SILENT}"] == "none"
    assert said[BIG_INBOX_ID] == "written"


@needs_chrome
def test_a_week_in_the_table_is_an_address_and_asks_for_itself_once(browser,
                                                                    tmp_path):
    """The week column is a plain anchor, which the address router already
    knows how to open. What that buys is one request: a handler beside the
    href would fetch the week and then the hash change would fetch it
    again."""
    page = big_inbox(browser, tmp_path, "ibopen")
    page.click("#inbox-rows td.week a")
    page.until("STATE.screen === 'review'")
    assert hash_of(page) == "#/review/" + BIG_INBOX_ID
    asked = page.eval(
        "window.__requests.filter(r => r.method === 'GET' && r.url === "
        + json.dumps("/api/review?id=" + BIG_INBOX_ID) + ").length")
    assert asked == 1, f"the week was asked for {asked} times"


@needs_chrome
def test_the_button_beside_the_stamp_opens_the_newest_week(browser, tmp_path):
    """The one week most visits are after, without reading the table for
    it."""
    page = big_inbox(browser, tmp_path, "ibbutton")
    assert page.eval(
        "document.getElementById('inbox-open').textContent") \
        == "Open " + BIG_INBOX_ID
    page.click("#inbox-open")
    page.until("STATE.screen === 'review'")
    assert hash_of(page) == "#/review/" + BIG_INBOX_ID


@needs_chrome
def test_a_quarter_of_weeks_is_one_screen_at_1280(browser, tmp_path):
    """A quarter of weeks is read without scrolling for it, which is what the
    card per week cost: fourteen of them stacked, and the numbers in each
    laid out in a different place."""
    page = big_inbox(browser, tmp_path, "ibtall")
    tall = page.eval("Math.ceil(document.documentElement.scrollHeight)")
    assert tall < 1500, f"the inbox is {tall}px tall at 1280"




@needs_chrome
def test_the_lens_swaps_the_dimension_and_a_group_filters_the_rows(browser,
                                                                   tmp_path):
    """The board's whole argument: one queue, five ways of looking at it, and
    the way you are looking at it is in the address. A facet whose rows name
    several groups puts one incident in each of them, which is why the code
    lens draws more groups than there are incidents."""
    page = browser
    page.goto(mock_page(tmp_path, "lens").as_uri())
    page.until("STATE.screen === 'queue'")
    assert hash_of(page) == "#/queue", \
        "the dimension the queue opens on writes the bare address"
    assert group_names(page) == ["cdb1", "cdb1_stby"]
    assert page.eval("document.querySelectorAll('#queue-rows button').length") \
        == 2

    lens(page, "status")
    assert hash_of(page) == "#/queue/status", "the lens is in the address"
    assert group_names(page) == ["open", "monitoring"], \
        "a facet with a fixed vocabulary is drawn in that order, not by count"

    page.click('#facet-chart .gseg[data-group="open"]')
    page.until("STATE.lens.group === 'open'")
    assert hash_of(page) == "#/queue/status/open"
    assert page.eval("document.querySelectorAll('#queue-rows button').length") \
        == 1, "the filtered board holds only the group that was clicked"
    assert group_names(page) == ["open"]
    assert page.eval("document.querySelectorAll("
                     "'#facet-chart .gseg').length") == 2, \
        "and the bar still draws every group, so there is a way back out"
    assert page.eval('document.querySelector(\'#facet-chart '
                     '.gseg[data-group="open"]\').getAttribute'
                     "('aria-pressed')") == "true", \
        "the segment the board is filtered to says so"
    assert "1 of 2 shown" in page.eval(
        "document.getElementById('lens-said').textContent")

    page.click('#facet-chart .gseg[data-group="open"]')
    page.until("STATE.lens.group === null")
    assert hash_of(page) == "#/queue/status", \
        "clicking the group again clears the filter"
    assert page.eval("document.querySelectorAll('#queue-rows button').length") \
        == 2

    lens(page, "code")
    assert group_names(page) == ["ORA-00600", "ORA-00604", "ORA-00607",
                                 "ORA-06512", "TNS-12564"], \
        "a row naming four codes lands in four groups"
    assert page.eval("document.querySelectorAll('#queue-rows button').length") \
        == 5, "and is drawn once under each of them"

    page.eval("location.hash = '#/queue/code/TNS-12564'")
    page.until("STATE.lens.group === 'TNS-12564'")
    assert page.eval("STATE.lens.facet") == "code", \
        "an address carries the lens back, facet and group both"
    assert page.eval("document.querySelectorAll('#queue-rows button').length") \
        == 1


@needs_chrome
def test_including_resolved_asks_the_server_for_them(browser, tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "resolved").as_uri())
    page.until("STATE.screen === 'queue'")
    page.until("STATE.heat.data !== null")
    assert page.eval("window.__requests.map(r => r.url)") \
        == ["/api/incidents", "/api/heat?days=30"], \
        "the board is fetched before the maps behind it, and each once"

    page.click("#lens-all")
    page.until("STATE.lens.all === true")
    assert last_request(page)["url"] == "/api/incidents?all=1", \
        "the toggle is a different question for the server, not a filter here"
    page.until("document.querySelectorAll('#queue-rows button').length === 3")
    assert page.eval(
        "document.getElementById('lens-all').getAttribute('aria-pressed')") \
        == "true"
    assert "resolved included" in page.eval(
        "document.getElementById('lens-said').textContent")
    words = page.eval(TEXTS % "#queue-rows .badge .word")
    assert "Resolved" in words, "the closed incident is on the board"

    page.click("#lens-all")
    page.until("STATE.lens.all === false")
    page.until("document.querySelectorAll('#queue-rows button').length === 2")


QUIET_CHIPS = TEXTS % "#queue-rows .quiet:not([hidden])"

def quiet_incident():
    return dict(INCIDENT, status="open", label="Open",
                last_seen=day_back(40))


@needs_chrome
def test_a_quiet_open_row_is_named_and_filtered_to_without_a_second_request(
        browser, tmp_path):
    """The board's answer to a case nobody closed. The span is the page's own
    arithmetic over the day the wire sends, so what is proved here is the
    rule and not a boolean: an open row whose codes stopped landing gets the
    chip, a row still being hit does not, and neither does a monitoring row
    that is just as quiet, because someone is already watching that one."""
    page = browser
    page.goto(mock_page(tmp_path, "quiet", queue=quiet_queue(),
                        queue_all=quiet_queue()).as_uri())
    page.until("STATE.screen === 'queue'")
    page.until("document.querySelectorAll('#queue-rows button').length === 4")
    assert page.eval(QUIET_CHIPS) == ["quiet 40d"], \
        "one row on this board has been quiet long enough to say so"
    page.until("STATE.heat.data !== null")
    asked = page.eval("window.__requests.length")

    page.click("#lens-quiet")
    page.until("STATE.lens.quiet === true")
    page.until("document.querySelectorAll('#queue-rows button').length === 1")
    assert page.eval(TEXTS % "#queue-rows .name") == [QUIET_SLUG]
    assert page.eval("window.__requests.length") == asked, \
        "the rows are already in hand, so the filter asks the server nothing"
    assert page.eval("document.getElementById('lens-quiet')"
                     ".getAttribute('aria-pressed')") == "true"
    assert "quiet only" in page.eval(
        "document.getElementById('lens-said').textContent")
    assert "1 of 4 shown" in page.eval(
        "document.getElementById('lens-said').textContent")

    page.click("#lens-quiet")
    page.until("STATE.lens.quiet === false")
    page.until("document.querySelectorAll('#queue-rows button').length === 4")
    assert page.eval("window.__requests.length") == asked


@needs_chrome
def test_the_case_header_says_how_long_an_open_incident_has_been_quiet(
        browser, tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "quiet-case",
                        incident=quiet_incident()).as_uri())
    open_incident(page)
    assert page.eval(
        "document.getElementById('incident-quiet').textContent") \
        == "quiet 40d", "the chip the board draws is the chip the case draws"
    assert page.eval("document.getElementById('incident-quiet').hidden") \
        is False


@needs_chrome
def test_the_case_header_says_nothing_quiet_about_a_watched_incident(
        browser, tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "watched-case").as_uri())
    open_incident(page)
    assert page.eval("document.getElementById('incident-quiet').hidden") \
        is True, "a monitoring case is someone's open question, not a stale one"


HEAT_CELLS = "#heat-maps .hcell"

HEAT_ROW = f"#heat-maps .hrow[data-db=\"{DB_NAME}\"]"

#: The band a claim is about, since a row now holds one per message class.
ERROR_BAND = f'{HEAT_ROW} .hb[data-kind="error"]'

#: The busiest day of any class on any row, which is what the one ramp under
#: the card is scaled against and what its darkest swatch is labelled with.
HEAT_MOST = max(count for db in HEAT_SILENT for name in heat.CLASSES
                for count in heat_counts(db, name) if count is not None)


def heat_blanks(span):
    """How many cells no digest covers inside the last `span` days: a silent
    day is silent in all three of a database's bands."""
    return len(heat.CLASSES) * sum(1 for silent in HEAT_SILENT.values()
                                   for back in silent if back < span)


@needs_chrome
def test_the_heat_maps_draw_a_class_each_and_group_the_rows_by_host(browser,
                                                                   tmp_path):
    """The board's second question: not what is open, but how loud each
    database has been. One map per message class over one axis of days, the
    rows banded by the host they run on, a day nobody observed drawn as
    nothing rather than as quiet, and a cell or its row label opening the
    database it counts."""
    page = browser
    page.goto(mock_page(tmp_path, "heat").as_uri())
    page.until("STATE.screen === 'queue'")
    page.until("STATE.heat.data !== null")

    assert page.eval("document.querySelectorAll('#heat-maps .heatmap')"
                     ".length") == 1, \
        "one card, and three bands per database inside it"
    assert page.eval(TEXTS % "#heat-maps .hband") \
        == ["lab-dg1.localdomain", "lab-dg2.localdomain", "host unknown"], \
        "the bands follow the order the rows arrived in, unknown host last"
    assert page.eval(
        "Array.from(document.querySelectorAll('%s .hb'))"
        ".map(n => n.dataset.kind)" % HEAT_ROW) == list(heat.CLASSES), \
        "a row is three bands, in the order the compactor counts them"
    assert page.eval(f"document.querySelectorAll('{HEAT_CELLS}').length") \
        == len(HEAT_HOSTS) * 30 * len(heat.CLASSES), \
        "a cell per database per class per day of the span"
    assert page.eval(
        f"document.querySelectorAll('{HEAT_CELLS}.blank').length") \
        == heat_blanks(30), "and a day with no digest is one of them, blank"
    legend = page.eval(TEXTS % "#heat-maps .heat-legend li")
    assert legend[:3] == ["Errors", "Warnings", "Unmatched lines"], \
        f"the three classes are named once, beside the card, said {legend}"
    assert legend[3] == "0" and legend[7] == f"{HEAT_MOST} at most", \
        f"and the ramp under them is one ramp over all three, said {legend}"
    assert legend[-1] == "no digest"

    loud = heat_counts(DB_NAME, "error")[-1]
    assert page.eval(f"document.querySelector('{ERROR_BAND} "
                     "rect:last-of-type title').textContent") \
        == f"{DB_NAME} · {day_words(HEAT_LAST)} · {loud} error lines", \
        "the newest day is the rightmost column, and it says what it counted"
    silent = 30 - min(HEAT_SILENT[DB_NAME])
    unseen = day_words(HEAT_DAYS[-1 - min(HEAT_SILENT[DB_NAME])])
    assert page.eval(f"document.querySelector('{ERROR_BAND} "
                     f"rect:nth-of-type({silent}) title').textContent") \
        == f"{DB_NAME} · {unseen} · no digest", \
        "a day the wiki holds no digest for says so rather than reading zero"

    page.click('#heat-days button[data-days="90"]')
    page.until("STATE.heat.days === 90")
    assert last_request(page)["url"] == "/api/heat?days=90", \
        "the span is a different question for the server, not a slice here"
    page.until(f"document.querySelectorAll('{HEAT_CELLS}').length === "
               f"{len(HEAT_HOSTS) * 90 * len(heat.CLASSES)}")
    assert page.eval(
        f"document.querySelectorAll('{HEAT_CELLS}.blank').length") \
        == heat_blanks(90), "the wider window uncovers the older silent days"
    assert hash_of(page) == "#/queue", \
        "how far back the maps look is not a queue the operator bookmarks"

    page.click(f"{HEAT_CELLS}")
    page.until("STATE.screen === 'db'")
    assert hash_of(page) == "#/db/" + DB_NAME, \
        "a cell opens the database whose row it sits on"

    page.click("#nav-queue")
    page.until("STATE.screen === 'queue'")
    page.until("STATE.heat.data !== null")
    page.click(f"{HEAT_ROW} .hlabel")
    page.until("STATE.screen === 'db'")
    assert hash_of(page) == "#/db/" + DB_NAME, \
        "and so does the row label beside it, which is the tab stop"


#: Every count of every class on every row turned to null, which is what a
#: wiki that holds no digest sidecar for the span answers with.
UNOBSERVED = """(() => {
  const heat = window.__mock.heat;
  const nothing = heat.days.map(() => null);
  heat.rows.forEach((row) => Object.keys(row.counts).forEach(
    (name) => { row.counts[name] = nothing; }));
  loadHeat();
  return true;
})()"""


@needs_chrome
def test_a_span_no_digest_covers_says_so_rather_than_drawing_empty_maps(
        browser, tmp_path):
    """A wiki whose digests do not reach back this far draws no map at all.
    Three maps of nothing but blank cells would say the databases were quiet,
    which is the one thing the null cell exists to deny."""
    page = browser
    page.goto(mock_page(tmp_path, "heat-empty").as_uri())
    page.until("STATE.heat.data !== null")
    assert page.eval("document.getElementById('heat-empty').hidden")

    page.eval(UNOBSERVED)
    page.until("document.getElementById('heat-empty').hidden === false")
    assert page.eval("document.querySelectorAll('#heat-maps .heatmap').length")\
        == 0, "and the region is empty rather than a card of nothing"
    assert "No digest sidecar" in page.eval(
        "document.getElementById('heat-empty').textContent")
    assert page.eval("document.getElementById('heat-note').textContent") == "", \
        "the note counts what was drawn, and nothing was"


@needs_chrome
def test_a_marker_on_the_timeline_opens_the_incident_it_stands_for(browser,
                                                                   tmp_path):
    """Every marker carries its own glyph as well as its own colour, a title
    for the pointer, a label for the reader, and a row in the list under the
    axis; and it opens with the keyboard as well as with the mouse."""
    page = browser
    page.goto(mock_page(tmp_path, "timeline").as_uri())
    page.until("STATE.screen === 'queue'")
    assert page.eval("document.querySelectorAll('#when-chart .mark').length") \
        == 2
    glyphs = page.eval(TEXTS % "#when-chart .mark-glyph")
    assert len(set(glyphs)) == 2, \
        f"the two statuses carry different marks, said {glyphs}"
    assert page.eval("document.querySelectorAll('#when-list li').length") == 2, \
        "and every marker is a line of text under the axis as well"
    assert "opened 2026-08-11T06:14:22Z" in page.eval(
        f"document.querySelector('#when-chart .mark[data-slug=\"{SLUG}\"] "
        "title').textContent")

    page.eval(f"document.querySelector('#when-chart "
              f"[data-slug=\"{SLUG}\"]').focus()")
    page.press("Enter")
    page.until("STATE.screen === 'incident'")
    assert hash_of(page) == "#/incident/" + SLUG, \
        "a marker opens with the keyboard, not only under a pointer"


#: Every inline label on the when chart as a bounding rectangle the way the
#: browser laid it out, which is the only honest way to ask whether two of
#: them are printed on top of each other.
LABEL_BOXES = """Array.from(document.querySelectorAll('#when-chart .mlabel'))
  .map((el) => { const r = el.getBoundingClientRect();
                 return [r.left, r.top, r.right, r.bottom]; })"""


@needs_chrome
def test_a_crowded_timeline_names_what_it_can_and_says_how_many_it_could_not(
        browser, tmp_path):
    """Thirty-one incidents in four clusters is the live board, and the
    chart that labelled all of them printed one title over the next. It now
    labels only the marks with the room for a title worth reading, keeps
    every mark, and says under the axis how many it named."""
    page = browser
    page.goto(mock_page(tmp_path, "crowd", queue=CROWD_QUEUE,
                        queue_all=CROWD_QUEUE).as_uri())
    page.until("STATE.screen === 'queue'")
    many = len(CROWD_QUEUE["incidents"])
    assert page.eval("document.querySelectorAll('#when-chart .mark').length")\
        == many, "every incident is still a mark, labelled or not"

    boxes = page.eval(LABEL_BOXES)
    assert 0 < len(boxes) < many, \
        f"some marks are named and some are not, said {len(boxes)}"
    for one in range(len(boxes)):
        for two in range(one + 1, len(boxes)):
            a, b = boxes[one], boxes[two]
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] \
                or b[3] <= a[1], f"labels {a} and {b} are printed on top"
    for text in page.eval(TEXTS % "#when-chart .mlabel"):
        assert len(text) >= 24, f"a label this short says nothing: {text!r}"

    assert page.eval("document.getElementById('when-note').textContent") == \
        f"{many} incidents · {len(boxes)} named on the chart, " \
        "all in the list below"
    assert page.eval(
        "document.querySelectorAll('#queue-rows .qitem').length") == many, \
        "and the board below still carries every one of them"


@needs_chrome
def test_the_incident_screen_draws_the_case_before_it_offers_a_verb(browser,
                                                                    tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "case").as_uri())
    open_incident(page)

    tiles = page.eval(TEXTS % "#facts dt")
    assert tiles == ["database", "opened", "age", "last written", "wiki at"]
    assert page.eval(TEXTS % "#facts dd")[0] == "cdb1"
    assert page.eval(TEXTS % "#facts dd")[4] == REV[:7]

    codes = page.eval(
        "Array.from(document.querySelectorAll('#incident-codes a'))"
        ".map(a => [a.getAttribute('href'), a.textContent])")
    assert codes == [[f"#/page/errors/{code}.md", code] for code in CODES], \
        "an error code the wiki holds a page for is an address into it"

    said = strip_text(page, "window-strip")
    assert stamp_words(INCIDENT["window"]["start"]) in said \
        and stamp_words(INCIDENT["window"]["until"]) in said, \
        f"the window's two instants are text before they are a bar, {said!r}"
    assert "error absent ORA-00600" in said, "and the signal it waits on"
    assert "not met" in said, "and the verdict the last tick published"
    assert page.eval("document.querySelectorAll('#window-draw svg').length") \
        == 1
    assert page.eval("document.getElementById('window-none').hidden") is True

    assert page.eval("document.querySelectorAll('#acts li.act').length") \
        == len(INCIDENT["actions"])
    assert page.eval(TEXTS % "#acts .verb") == ["start-monitoring"], \
        "the kind is the word the CLI spells it with, never a paraphrase"
    assert page.eval(TEXTS % "#acts .disc") == ["◍"], \
        "the disc carries the kind's own mark"
    assert page.eval(TEXTS % "#acts .outcome .word") == ["pending"]
    assert page.eval(TEXTS % "#acts .outcome .mk") == ["◌"], \
        "an outcome carries its own mark, so the badge is never colour alone"
    assert page.eval("document.getElementById('acts-none').hidden") is True

    badge = strip_text(page, "incident-badge")
    assert "Monitoring" in badge and "◍" in badge, \
        f"the status is a word and a glyph beside the ribbon, said {badge!r}"


@needs_chrome
def test_an_instant_is_said_one_way_and_names_its_year_only_when_it_is_old(
        browser, tmp_path):
    """The two helpers every painter now goes through, read directly. A day
    inside this year is four characters shorter than one outside it, which is
    the whole reason the year is not always printed."""
    page = browser
    page.goto(mock_page(tmp_path, "vocabulary").as_uri())
    page.until("STATE.screen === 'queue'")
    year = dt.date.today().year
    assert page.eval(f"dayWords('{year}-09-04T15:00:00Z')") == "4 Sep"
    assert page.eval(f"stampWords('{year}-09-04T15:00:00Z')") == "4 Sep 15:00"
    assert page.eval("dayWords('2019-09-04')") == "4 Sep 2019", \
        "a day outside this year says which year it is"
    assert page.eval("stampWords('2019-09-04T15:00:00Z')") \
        == "4 Sep 2019 15:00"
    assert page.eval("stampWords('')") == "unrecorded"
    assert page.eval("dayWords('', 'undated')") == "undated", \
        "and a caller with its own word for the absence keeps it"


@needs_chrome
def test_the_interview_threads_each_answer_under_the_question_asked(browser,
                                                                    tmp_path):
    """The one advisory row the operator writes the prompt for. What is
    proved is the contract: the question rides with the tool and the click's
    own `at`, the answer is polled on the same ladder as every other row, and
    it lands under the question it answers rather than replacing it."""
    page = browser
    page.goto(mock_page(tmp_path, "interview", tools=ASK_TOOLS,
                        starts=ASK_STARTS, polls=ASK_POLLS).as_uri())
    open_incident(page)
    assert page.eval("document.getElementById('ask').hidden") is False
    assert page.eval(f"{MANIFEST}.length") == 0, \
        "offering the panel costs the server nothing: the manifest is packed " \
        "when it is opened and not before"
    assert "opening this asks" in strip_text(page, "ask-strip")

    open_ask(page)
    assert page.eval(TEXTS % "#tool-rows .name") == [
        row["label"] for row in TOOLS["tools"]], \
        "the asking row is the interview's; it is not a button on the panel"
    reads = page.eval("document.getElementById('ask-reads').textContent")
    assert "digest, neighbours" in reads and "9711 characters packed" in reads, \
        f"the panel says what a question would read before one is typed, " \
        f"said {reads!r}"
    assert page.eval("document.getElementById('ask-go').disabled") is False
    assert page.eval("document.getElementById('ask-empty').hidden") is False

    ask_question(page, QUESTION)
    sent = last_request(page)
    assert sent["url"] == f"/api/incidents/{SLUG}/advisory"
    assert set(sent["body"]) == {"tool", "question", "at"}, \
        f"a question rides with the row and the click's at, sent {sent['body']}"
    assert sent["body"]["tool"] == "ask-incident"
    assert sent["body"]["question"] == QUESTION
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                        sent["body"]["at"])
    assert page.eval("document.getElementById('ask-q').value") == "", \
        "the box is emptied for the next question, which is now in the thread"
    assert page.eval(TEXTS % "#ask-thread .q .txt") == [QUESTION]

    page.until("STATE.thread[0].run.status === 'succeeded'", timeout=20)
    turn = "#ask-thread li:nth-child(1)"
    assert ANSWERED["text"] in page.eval(
        f"document.querySelector('{turn} .says').textContent"), \
        "the answer is under the question it answers"
    assert page.eval(f"document.querySelectorAll('{turn} .links a').length") \
        == 1, "a cited digest the wiki holds is a link"
    assert page.eval(
        f"document.querySelector('{turn} .links .nolink').textContent") \
        == MISSING_DIGEST
    cost = page.eval(f"document.querySelector('{turn} .cost').textContent")
    assert "gemma-3" in cost and "$0.0031" in cost
    assert page.eval(f"document.querySelector('{turn} .badge .word')"
                     ".textContent") == "succeeded"
    assert page.eval(f"{POLLED}.length") == 2, \
        "the interview walks the same ladder every other row walks"

    time.sleep(POLL_QUIET)
    assert page.eval(f"{POLLED}.length") == 2, \
        "and stops when the run settles rather than polling forever"


@needs_chrome
def test_a_workbench_whose_manifest_asks_nothing_offers_no_interview(browser,
                                                                     tmp_path):
    """The panel is a claim about what this deployment can do, so it is drawn
    off the manifest and never off the page's own hopes."""
    page = browser
    page.goto(mock_page(tmp_path, "no-interview").as_uri())
    open_incident(page)
    assert page.eval("document.getElementById('ask').hidden") is False, \
        "nobody knows yet: the manifest has not been read"
    open_ask(page)
    page.until("document.getElementById('ask').hidden === true")
    assert page.eval("document.querySelectorAll('#ask-thread li').length") == 0



def test_the_page_assigns_innerhtml_exactly_once():
    """renderBody's single assignment is what confines markup insertion to the
    rendered-prose container; a second assignment anywhere would widen the
    injection surface the hashed-script CSP was tightened for."""
    script = "".join(ui._INLINE_SCRIPT_RE.findall(ui.page()))
    assert script.count("innerHTML") == 1


def test_a_measure_is_text_before_it_is_ever_a_bar():
    """Length is load-bearing nowhere, for the reason colour is not: the cell
    carries the number, and the bar is announced to nobody."""
    script = "".join(ui._INLINE_SCRIPT_RE.findall(ui.page()))
    drawn = re.search(r"function measureCell\(.*?\n\}", script, re.S)
    assert drawn, "the page no longer draws a measure cell"
    body = drawn.group(0)
    assert 'cell("unmeasured", "num")' in body, \
        "a measure with no stage behind it is a word, never a zero-length bar"
    assert 'bar.setAttribute("aria-hidden", "true")' in body
    assert "innerHTML" not in body and "textContent" not in body, \
        "the value goes through cell(), which is the one text writer"


def test_no_painter_cuts_a_date_out_of_an_iso_string_by_hand():
    """One vocabulary for instants, held by the absence of the two cuts every
    hand-built format on this page used to start with. A painter that wants a
    day says `dayWords`, one that wants an instant says `stampWords`, and the
    ISO itself goes in the element's title rather than into its text."""
    script = "".join(ui._INLINE_SCRIPT_RE.findall(ui.page()))
    for cut in (".slice(0, 10)", ".slice(11, 16)"):
        assert cut not in script, \
            f"a painter still cuts a date out of an ISO string with {cut}"
    for helper in ("function dayWords(", "function stampWords(",
                   "function stamped("):
        assert helper in script, f"the page no longer declares {helper}"


def test_the_new_regions_say_what_an_empty_answer_means():
    html = ui.page()
    for said in ("The logs hold no dated line.",
                 "No run in what the logs hold\n    recorded a categorised "
                 "failure.",
                 "No earlier run of this command is\n    still in the logs; "
                 "the runs screen says how far back they go."):
        assert said in html, f"the page no longer says {said!r}"
    for node in ("runs-no-trend", "runs-no-failures", "run-no-compare"):
        assert f'id="{node}" hidden>' in html, \
            f"{node} is hidden until a painter has an empty answer to explain"


HREFS = ("Array.from(document.querySelectorAll('%s'))"
         ".map(a => a.getAttribute('href'))")


@needs_chrome
def test_the_case_file_says_what_the_wiki_knows_about_each_code(browser,
                                                                tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "research").as_uri())
    open_incident(page)

    assert page.eval("document.getElementById('research-cap').hidden") is False
    assert page.eval(TEXTS % "#research .rcard .code") == CARDED, \
        "a card is for a code the wiki can answer about, and no other"
    assert page.eval(HREFS % "#research .rcard a.code") == \
        [f"#/page/errors/{code}.md" for code in CARDED], \
        "each card opens the page its research was read from"
    assert page.eval(TEXTS % "#research .rstate") == \
        [f"researched {day_words('2026-08-17')}", "unresearched"], \
        "the date is the claim, and its absence is the gap"
    assert page.eval("document.querySelectorAll('#research .thin').length") \
        == 1, "an unresearched card is drawn as an outline, not as a colour"
    assert page.eval(TEXTS % "#research-thin .code") == UNRESEARCHED, \
        "and a code the wiki cannot answer about at all is named on one line"
    assert page.eval(HREFS % "#research-thin a.code") == \
        [f"#/page/errors/{code}.md" for code in UNRESEARCHED], \
        "which still opens its own page"
    assert "not researched yet" in page.eval(
        "document.getElementById('research-thin').textContent")

    assert page.eval(TEXTS % "#research .rcard:nth-child(1) .cause") == [CAUSE]
    assert page.eval(TEXTS % "#research .rcard:nth-child(1) .action") \
        == [ACTION]
    assert page.eval(TEXTS % "#research .rcard:nth-child(1) .rsrc") \
        == [f"Source: oracle-docs (accessed {day_words('2026-08-17')})"], \
        "the citation is a source line under the card, not text in it"
    assert page.eval(HREFS % "#research .rcard:nth-child(1) .rsrc a") \
        == [CITATION["url"]]
    assert page.eval(
        "document.querySelector('#research .rcard:nth-child(2) .rsrc').hidden"
    ) is True, "an unresearched card has no source to name"
    assert page.eval("document.querySelector("
                     "'#research .rcard:nth-child(2) .rbody').hidden") is True
    assert page.eval(TEXTS % "#research .rcard:nth-child(2) .rfix .cap") \
        == ["Resolved before"]
    assert page.eval(TEXTS % "#research .rcard:nth-child(2) .rfix li") == [
        f"{day_words('2026-08-05')} on cdb1 raised the listener queue and "
        "bounced it",
        f"{day_words('2026-07-02')} on cdb2 reset the dead connection "
        "detection interval",
    ], "a code nobody researched still names what closed it before"
    assert page.eval(HREFS % "#research .rcard:nth-child(2) .rfix a") == [
        f"#/incident/{fix['incident']}" for fix in RESOLUTIONS], \
        "each line opens the case file it was resolved in"
    assert page.eval(TEXTS % "#research .rcard:nth-child(1) .rnotes .cap") \
        == ["Practitioner notes"]
    assert page.eval(TEXTS % "#research .rcard:nth-child(1) .rnotes li") == [
        NOTES[0]["text"] + NOTE_SOURCES[0],
        NOTES[1]["text"] + NOTE_SOURCES[1],
    ], "each note is followed by the page it was read out of, not the card's"
    assert page.eval(TEXTS % "#research .rcard:nth-child(1) .rnotes .nsrc") \
        == NOTE_SOURCES, \
        "the provenance is its own line, not the tail of the claim"
    assert page.eval(HREFS % "#research .rcard:nth-child(1) .rnotes a") \
        == [NOTES[0]["url"]], \
        "a source with no URL is named and not linked to nowhere"
    assert page.eval(
        "document.querySelector('#research .rcard:nth-child(2) .rnotes')"
        ".hidden") is True, \
        "an unresearched card draws no empty practitioner list"
    assert page.eval(
        "document.querySelector('#research .rcard:nth-child(1) .rfix').hidden"
    ) is True, "research is no reason to draw a history nobody wrote"
    assert page.eval(TEXTS % "#research .rcard:nth-child(2) .rpast .cap") \
        == ["Past fixes"]
    assert page.eval(TEXTS % "#research .rcard:nth-child(2) .rpast li") == [
        f"{day_words('2026-08-05')} on cdb1 raised the listener queue and "
        "bounced it (ticket: CHG-7) · succeeded · held (14d)",
        f"{day_words('2026-08-04')} on cdb1 restarted the listener · failed "
        "· n/a",
    ], "every recorded fix, the failed one included, with whether it held"
    assert page.eval(HREFS % "#research .rcard:nth-child(2) .rpast a") == [
        f"#/incident/{fix['incident']}" for fix in PAST_FIXES], \
        "each line opens the case file the fix was recorded on"
    assert page.eval(
        "document.querySelector('#research .rcard:nth-child(1) .rpast')"
        ".hidden") is True, "no table on the page, no list on the card"


@needs_chrome
def test_a_code_with_only_past_fixes_still_gets_a_card(browser, tmp_path):
    """Nothing researched and nothing resolved, but somebody tried something:
    that attempt is an answer, so the code is a card and not a name on the
    line."""
    page = browser
    tried = dict(INCIDENT, research=[
        dict(row, researched="", cause="", action="", citations=[], notes=[],
             resolutions=[], past_fixes=PAST_FIXES if i == 2 else [])
        for i, row in enumerate(RESEARCH)])
    page.goto(mock_page(tmp_path, "tried", incident=tried).as_uri())
    open_incident(page)
    assert page.eval(TEXTS % "#research .rcard .code") == [CODES[2]]
    assert page.eval(
        "document.querySelector('#research .rcard .rfix').hidden") is True
    assert len(page.eval(TEXTS % "#research .rcard .rpast li")) == 2


#: A cause with a tag in it, which no page under `wiki/errors` carries and
#: which one could carry tomorrow: research is fetched prose, and the day a
#: source's markup survives into it the card has to print it rather than run
#: it.
TAGGED_CAUSE = "a condition an <i>Oracle process</i> hit"


@needs_chrome
def test_a_cause_that_looks_like_markup_is_drawn_as_the_characters_it_is(
        browser, tmp_path):
    page = browser
    tagged = dict(INCIDENT, research=[dict(RESEARCH[0], cause=TAGGED_CAUSE)])
    page.goto(mock_page(tmp_path, "tagged", incident=tagged).as_uri())
    open_incident(page)
    assert page.eval(TEXTS % "#research .cause") == [TAGGED_CAUSE], \
        "every angle bracket the wire sent is on the screen as itself"
    assert page.eval(
        "document.querySelectorAll('#research .cause i').length") == 0, \
        "and none of them became an element"


@needs_chrome
def test_the_research_region_is_absent_on_an_incident_naming_no_code(browser,
                                                                    tmp_path):
    page = browser
    quiet = dict(INCIDENT, error_codes=[], research=[])
    page.goto(mock_page(tmp_path, "nocodes", incident=quiet).as_uri())
    open_incident(page)
    assert page.eval("document.getElementById('research-cap').hidden") is True
    assert page.eval("document.querySelectorAll('#research .rcard').length") \
        == 0
    assert page.eval("document.getElementById('research-thin').hidden") is True
    assert page.eval("document.getElementById('incident-codes').hidden") \
        is True


@needs_chrome
def test_an_incident_the_wiki_can_answer_nothing_about_draws_only_the_line(
        browser, tmp_path):
    """Nothing researched and nothing closed before: the region is the line
    naming the codes, and never four cards of the same sentence."""
    page = browser
    blank = dict(INCIDENT, research=[
        dict(row, researched="", cause="", action="", citations=[], notes=[],
             resolutions=[], past_fixes=[]) for row in RESEARCH])
    page.goto(mock_page(tmp_path, "unresearched", incident=blank).as_uri())
    open_incident(page)
    assert page.eval("document.querySelectorAll('#research .rcard').length") \
        == 0
    assert page.eval(TEXTS % "#research-thin .code") == CODES, \
        "every code is on the line, in the order the page names them"


@needs_chrome
def test_a_researched_code_whose_page_is_gone_is_named_and_not_linked(browser,
                                                                     tmp_path):
    page = browser
    gone = dict(INCIDENT, error_codes=[CODES[0]], research=[
        dict(RESEARCH[0], exists=False)])
    page.goto(mock_page(tmp_path, "gonepage", incident=gone).as_uri())
    open_incident(page)
    assert page.eval(HREFS % "#research .rcard a.code") == [], \
        "a card never offers an address the wiki cannot answer"
    assert page.eval(TEXTS % "#research .rcard .code.nolink") == [CODES[0]]
    assert page.eval(TEXTS % "#research .rcard .cause") == [CAUSE], \
        "the research survives the page that carried it going missing"


@needs_chrome
def test_a_source_url_that_is_not_http_is_drawn_as_its_name(browser,
                                                            tmp_path):
    """Issue 10: the citation and note URLs are researcher output, and
    `node.href = c.url` handed a `javascript:` one to a link the operator
    would click. Only `http(s):` becomes a link; anything else is the name."""
    page = browser
    evil = [{"source": "sources/evil", "url": "javascript:alert(1)",
             "accessed": "2026-08-17"},
            {"source": "sources/data", "url": "data:text/html,<b>x</b>",
             "accessed": "2026-08-17"}]
    notes = [dict(NOTES[0], url="JavaScript:alert(2)"), NOTES[1]]
    tainted = dict(INCIDENT, error_codes=[CODES[0]], research=[
        dict(RESEARCH[0], citations=[CITATION, *evil], notes=notes)])
    page.goto(mock_page(tmp_path, "badurl", incident=tainted).as_uri())
    open_incident(page)
    assert page.eval(HREFS % "#research a.src") == [CITATION["url"]], \
        "only the https citation is a link"
    assert "evil" in page.eval(TEXTS % "#research .rcard .rsrc")[0], \
        "the others are still named, as provenance"
    assert page.eval("document.querySelectorAll('#research [href^=\"java\" i],"
                     " #research [href^=\"data:\"]').length") == 0


def local_words(stamp):
    """What `stampWords` says for an instant read on the browser's own clock
    (`localWords`): day, month, the year only when it is not this one, and
    the clock."""
    at = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    year = "" if at.year == dt.date.today().year else f" {at.year}"
    return f"{at.day} {at:%b}{year} {at:%H:%M}"


@needs_chrome
def test_the_runs_list_reads_the_clock_its_day_headers_group_by(browser,
                                                                 tmp_path):
    """Issue 20: the runs fold grouped by the reader's day and labelled each
    row with the UTC characters, so in Prague a 23:30Z run sat under the
    next day's header reading the day before. Both are the reader's clock
    now, and the `at` column names the zone once."""
    late, early = "2026-08-30T23:30:00Z", "2026-08-30T00:30:00Z"
    runs = dict(RUNS, runs=[
        dict(RUNS["runs"][0], run_id="aaaaaaaaaaaa", started=late,
             finished=late),
        dict(RUNS["runs"][1], run_id="bbbbbbbbbbbb", started=early,
             finished=early)])
    page = browser
    page.goto(mock_page(tmp_path, "tzruns", runs=runs).as_uri() + "#/runs")
    page.until("STATE.screen === 'runs'")
    head = page.eval(TEXTS % "#runs-rows thead th")[0]
    assert re.fullmatch(r"at \(.+\)", head), \
        f"the at column names the reader's zone, said {head!r}"
    placed = page.eval(
        "Array.from(document.querySelectorAll('#runs-rows tbody tr'))"
        ".reduce((out, tr) => { if (tr.classList.contains('day')) "
        "out.day = tr.dataset.day; else if (tr.classList.contains('runrow')) "
        "out.rows.push([out.day, tr.querySelector('.tid').textContent,"
        " tr.querySelector('.tid').title]); return out; }, "
        "{day: '', rows: []}).rows")
    expected = []
    for stamp in (late, early):
        at = dt.datetime.fromisoformat(
            stamp.replace("Z", "+00:00")).astimezone()
        expected.append([at.date().isoformat(), local_words(stamp), stamp])
    assert placed == expected, \
        "each row sits under the day its own label names, ISO on hover"


@needs_chrome
def test_the_board_chips_and_the_database_table_address_the_error_pages(
        browser, tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "codelinks").as_uri())
    page.until("document.querySelectorAll('#queue-rows button').length === 2")
    assert page.eval(HREFS % "#queue-rows .qitem .codes a")[:len(CODES)] == \
        [f"#/page/errors/{code}.md" for code in CODES], \
        "every code on a board row is a resolved wikilink, so it is an address"

    page.goto(mock_page(tmp_path, "dberrors").as_uri() + "#/db/" + DB_NAME)
    page.until("STATE.screen === 'db'")
    assert page.eval(TEXTS % "#db-errors thead th") == \
        ["code", "occurrences", "", "last day", "researched", "resolved"], \
        "the unnamed column is the bar, which says nothing the count does not"
    assert page.eval(HREFS % "#db-errors tbody a") == \
        [f"#/page/errors/{row['code']}.md" for row in DB["errors"]]
    assert page.eval(TEXTS % "#db-errors tbody tr td:nth-child(5)") == \
        [day_words("2026-08-17"), "unresearched"], \
        "a code nobody has looked up says so where its count is read"
    assert page.eval(
        "document.querySelectorAll('#db-errors .b-unresearched').length") == 1
    assert page.eval(TEXTS % "#db-errors tbody tr td:nth-child(6)") == \
        ["—", "2"], \
        "a code nobody has closed yet reads as the table's absent value"


@needs_chrome
def test_the_agents_screen_draws_the_loop_and_the_pages_it_wrote(browser,
                                                                tmp_path):
    """The screen the operator asked for: which model did the work, when each
    tick ran it, and which incidents came out of it."""
    page = browser
    page.goto(mock_page(tmp_path, "agents").as_uri())
    page.until("STATE.screen === 'queue'")

    page.click("#nav-agents")
    page.until("STATE.screen === 'agents'")
    assert hash_of(page) == "#/agents"
    assert last_request(page)["url"] == "/api/agents?hours=48", \
        "the tab opens the window the screen defaults to"
    assert page.eval(
        "document.getElementById('nav-agents').getAttribute('aria-current')") \
        == "page", "the strip marks where the operator is"

    models = page.eval(TEXTS % "#agents-models tbody tr td.model .nm")
    assert models == ["NVIDIA-Nemotron-3.5-Lightning-30B", "gpt-5.6-luna"], \
        "the table is busiest first, and names a model and not a download"
    assert page.eval(TEXTS % "#agents-models tbody tr td.model .full") \
        == [NEMOTRON], \
        "the address it was fetched from stays under the name it goes by"
    row = page.eval(TEXTS % "#agents-models tbody tr:nth-child(1) td")
    assert "1 / 1" in row, \
        "the tier is a count per stage and never one label on the model"
    assert "pi" in row, "and the adapter it arrived by"
    assert page.eval(
        "document.querySelectorAll('#agents-models .bar').length") == 0, \
        "and no bar under a number two models have nothing to compare on"

    assert page.eval(TEXTS % "#agents-legend li .nm") == [
        "NVIDIA-Nemotron-3.5-Lightning-30B via pi", "gpt-5.6-luna via codex",
        "failed or rolled back"], \
        "one legend entry per model, and the mark that is not a model"
    assert page.eval(
        "document.querySelector('#agents-models tbody tr:nth-child(1)"
        " td.model .sw').className")\
        == page.eval("document.querySelector('#agents-legend li .sw')"
                     ".className"), \
        "a model's swatch in the table and in the legend is one class"


@needs_chrome
def test_the_seven_tiles_read_the_totals_the_server_sent(browser, tmp_path):
    """The head of the screen answers the questions before the chart does,
    and answers them off the payload's own totals rather than a second sum
    the page keeps."""
    page = browser
    page.goto(mock_page(tmp_path, "tiles").as_uri() + "#/agents")
    page.until("STATE.screen === 'agents'")

    keys = page.eval(TEXTS % "#agents-tiles .tile dt")
    assert keys == ["ticks", "stages", "failed", "rolled back", "wall time",
                    "tokens", "cost"]
    values = page.eval(TEXTS % "#agents-tiles .tile .v")
    assert values == ["3", "5", "1", "1", "0.3 h", "66.0k", "$0.00"], \
        f"the tiles are the window's own totals, said {values}"
    subs = page.eval(TEXTS % "#agents-tiles .tile .s")
    assert subs[0] == "every 15.8 h on average", \
        f"cadence is the span over the gaps between ticks, said {subs[0]}"
    assert subs[4] == "5 min per tick" and subs[6] == "4 stages unpriced"

    hot = page.eval(TEXTS % "#agents-tiles .tile.hot dt")
    assert hot == ["failed", "rolled back"], \
        "only a count nobody wants above zero takes the red tile"


@needs_chrome
def test_the_chart_draws_one_bar_per_tick_as_tall_as_the_tick_was_long(
        browser, tmp_path):
    """The lanes' whole job in one chart: when each tick ran, how long it
    took, which models spent it, and which one went wrong."""
    page = browser
    page.goto(mock_page(tmp_path, "chart").as_uri() + "#/agents")
    page.until("STATE.screen === 'agents'")

    bars = "#agents-chart g.tbar"
    assert page.eval(f"document.querySelectorAll('{bars}').length") == 3, \
        "one bar per tick, and never one lane per tick"
    xs = page.eval(f"Array.from(document.querySelectorAll('{bars}'))"
                   ".map(bar => parseFloat(bar.querySelector('rect')"
                   ".getAttribute('x')))")
    assert xs[2] < xs[1] < xs[0], \
        f"each bar sits at the clock time its tick started, said {xs}"

    tall = page.eval(
        "Array.from(document.querySelectorAll('%s')).map(bar => Array.from("
        "bar.querySelectorAll('rect.mbar')).reduce((sum, n) => sum + "
        "parseFloat(n.getAttribute('height')), 0))" % bars)
    assert tall[1] > tall[2] > tall[0], \
        f"the eleven-minute tick towers over the thirty-second lint, {tall}"
    assert abs(tall[1] / tall[2] - 716.6 / 201.0) < 0.05, \
        f"and height is the wall time, on one scale, said {tall}"

    assert page.eval(
        f"document.querySelectorAll('{bars}:nth-of-type(2) rect.mbar').length") \
        == 2, "the tick is stacked by the models that spent it"

    edges = f"{bars} rect.edge"
    assert page.eval(f"document.querySelectorAll('{edges}').length") == 1, \
        "one red edge, around the tick that failed and rolled back"
    assert page.eval(
        f"document.querySelector('{bars}:nth-of-type(2) rect.edge') !== null"), \
        "and it is around that tick and not another"

    said = page.eval(f"document.querySelector('{bars}:nth-of-type(2) title')"
                     ".textContent")
    assert said.startswith(RUN_ID[:7]) \
        and stamp_words("2026-08-31T08:15") in said, \
        f"the hover names the tick and the instant the log holds, {said!r}"
    assert "3 stages · 12 min" in said, f"and what it did, said {said!r}"
    assert page.eval(f"document.querySelector('{bars}:nth-of-type(2)')"
                     ".getAttribute('aria-label')") == said, \
        "a screen reader is handed the same string the hover shows"

    assert page.eval(TEXTS % f"{bars}:nth-of-type(3) text") == ["retry"], \
        "a tick that is not a run says which command it was"
    assert page.eval(TEXTS % f"{bars}:nth-of-type(1) text") == [], \
        "and gives the word up rather than printing it over `now`"


@needs_chrome
def test_a_bar_opens_the_run_the_tick_was(browser, tmp_path):
    """The lane's tid link, kept: the bar is the way from what the loop did
    to the run screen for that tick."""
    page = browser
    page.goto(mock_page(tmp_path, "bar-run").as_uri() + "#/agents")
    page.until("STATE.screen === 'agents'")
    page.click("#agents-chart g.tbar:nth-of-type(2)")
    page.until("STATE.screen === 'run'")
    assert hash_of(page) == "#/run/" + RUN_ID
    assert last_request(page)["url"] == f"/api/run?id={RUN_ID}"


#: The ledger draws the reader's own clock, so what it should say is
#: computed here rather than written out in whatever zone the suite runs in.
def local_at(stamp):
    at = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    return f"{at.day} {at:%b} {at:%H:%M}"


def day_head(stamp):
    at = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    return f"{at:%A} {at.day} {at:%B}"


@needs_chrome
def test_one_command_run_three_times_over_is_one_word_and_a_count(browser,
                                                                  tmp_path):
    """The word already yields to the neighbour on its left. Three of the
    same word inside the same stretch of axis are not three neighbours, they
    are one word with a count after it."""
    page = browser
    page.goto(mock_page(tmp_path, "crowded-chart",
                        agents=CROWDED_AGENTS).as_uri() + "#/agents")
    page.until("STATE.screen === 'agents'")
    words = page.eval(TEXTS % "#agents-chart .tbar .mu")
    assert "research \u00d73" in words, \
        f"three researches in a row are one word and a count, said {words}"
    assert len([one for one in words if one.startswith("research")]) == 1, \
        f"and the word is never printed twice over, said {words}"


@needs_chrome
def test_the_ledger_groups_the_ticks_by_day_and_says_how_each_went(browser,
                                                                  tmp_path):
    """The lane's label column and chip line, as a table: what ran, how long
    it took, what it wrote and whether anything went wrong."""
    page = browser
    page.goto(mock_page(tmp_path, "ledger").as_uri() + "#/agents")
    page.until("STATE.screen === 'agents'")

    days = page.eval(TEXTS % "#agents-ledger tr.day .nm, "
                             "#agents-ledger tr.day td:not(:has(.dayx))")
    assert days == [day_head(AGENT_TICKS[0]["started"]),
                    day_head(AGENT_TICKS[2]["started"])], \
        f"newest day first, one header each, said {days}"
    rows = "#agents-ledger tbody tr:not(.day)"
    assert page.eval(f"document.querySelectorAll('{rows}').length") == 2, \
        "the older day was all ok, so it is folded to its own header"

    said = page.eval(TEXTS % f"{rows}:nth-child(3) td")
    assert said[:6] == [local_at(RUN_STARTED), RUN_ID[:7],
                        "2 ingest \u00b7 report", "3", "12 min", "33.0k"], \
        f"the tick, what it was asked to do and what it took, said {said}"
    assert page.eval(TEXTS % f"{rows}:nth-child(3) td:last-child") \
        == ["1 failed \u00b7 1 rolled back"], \
        "and the two ways it went wrong, told apart"
    assert page.eval(TEXTS % f"{rows}:nth-child(2) td:last-child") == ["ok"], \
        "a tick that did what it was asked says so in a word"
    assert page.eval(TEXTS % f"{rows}:nth-child(2) td.wrote") == ["\u2014"], \
        "and a tick that wrote nothing says that too"


#: A window whose older day is three ticks that all went fine, which is what
#: most days of the loop are and what the ledger folds. The default fixture's
#: older day holds one tick, which proves the fold but not the counts on it.
QUIET_TICKS = [
    {"run_id": f"quiet{n}00000", "command": "run",
     "started": f"2026-08-30T0{n}:00:00Z",
     "finished": f"2026-08-30T0{n}:05:00Z", "stages": OLD_STAGES,
     "incidents": [{"slug": SLUG, "title": "Oracle internal errors on cdb1",
                    "status": "monitoring", "db": "cdb1", "commits": ["a" * 40]}],
     "totals": agent_totals(OLD_STAGES)}
    for n in (1, 2, 3)
]

QUIET_DAY = "2026-08-30"

FOLDING_AGENTS = {**AGENTS, "ticks": [AGENT_TICKS[1]] + QUIET_TICKS}

FOLDED = f'#agents-ledger tr.day[data-day="{QUIET_DAY}"]'


@needs_chrome
def test_a_day_the_loop_got_right_folds_to_one_row_the_reader_can_open(
        browser, tmp_path):
    """A week of the loop is ninety rows, and on most days every tick did what
    it was asked. Such a day says so in one row and keeps its ticks behind a
    disclosure; the newest day and any day something went wrong on are never
    folded, because those are the two the screen was opened for."""
    page = browser
    page.goto(mock_page(tmp_path, "folded", agents=FOLDING_AGENTS).as_uri()
              + "#/agents")
    page.until("STATE.screen === 'agents'")

    rows = "#agents-ledger tbody tr:not(.day)"
    assert page.eval(f"document.querySelectorAll('{rows}').length") == 1, \
        "the newest day's one tick, and nothing of the folded day"
    said = page.eval(f"document.querySelector('{FOLDED}').textContent")
    for part in (day_head(QUIET_TICKS[0]["started"]), "3 ticks", "all ok",
                 "10 min wall", "49.5k tokens", "wrote 3 pages"):
        assert part in said, f"the folded row says {part!r}, said {said!r}"
    assert page.eval(f"document.querySelector('{FOLDED} .dayx')"
                     ".getAttribute('aria-expanded')") == "false"

    page.click(f"{FOLDED} .dayx")
    page.until(f"document.querySelectorAll('{rows}').length === 4")
    assert page.eval(f"document.querySelector('{FOLDED} .dayx')"
                     ".getAttribute('aria-expanded')") == "true"
    assert "3 ticks" not in page.eval(
        f"document.querySelector('{FOLDED}').textContent"), \
        "an open day is its own header again, not a summary of itself"

    page.click("#agents-hours button:nth-child(3)")
    page.until("STATE.agents.window_hours === 168")
    assert page.eval(f"document.querySelectorAll('{rows}').length") == 4, \
        "a day the reader opened stays open across a window toggle"


@needs_chrome
def test_the_newest_day_and_a_day_that_went_wrong_are_never_folded(browser,
                                                                   tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "unfolded").as_uri() + "#/agents")
    page.until("STATE.screen === 'agents'")
    newest = '#agents-ledger tr.day[data-day="2026-08-31"]'
    assert page.eval(f"document.querySelector('{newest} .dayx') === null"), \
        "the newest day holds a failed tick and a rolled-back one, and is " \
        "the day the screen was opened for besides: it has no disclosure"
    assert page.eval(TEXTS % f"{newest} td") \
        == [day_head(AGENT_TICKS[0]["started"])], \
        "so its header says the day and nothing else"


@needs_chrome
def test_a_wrote_chip_in_the_ledger_opens_the_case_it_names(browser,
                                                            tmp_path):
    """The join, made clickable: the operator reading what the loop did last
    night is one click from the page it wrote. The chip says the database
    because the same three cases are written every two hours."""
    page = browser
    page.goto(mock_page(tmp_path, "chips").as_uri() + "#/agents")
    page.until("STATE.screen === 'agents'")
    chips = "#agents-ledger td.wrote .ichip"
    assert page.eval(TEXTS % chips) == ["cdb1", "cdb2"]
    assert page.eval(f"document.querySelector('{chips}').className") \
        == "ichip db s-monitoring", "the chip carries the status colour"
    assert page.eval(f"document.querySelector('{chips}').title") \
        == "Oracle internal errors on cdb1 \u00b7 monitoring \u00b7 2 commits", \
        "and the title the row it opens carries"

    page.click(chips)
    page.until("STATE.screen === 'incident'")
    assert hash_of(page) == "#/incident/" + SLUG
    assert last_request(page)["url"] == f"/api/incidents/{SLUG}"


@needs_chrome
def test_the_window_toggle_asks_the_server_and_redraws(browser, tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "window").as_uri() + "#/agents")
    page.until("STATE.screen === 'agents'")
    assert page.eval(
        "document.querySelectorAll('#agents-chart g.tbar').length") == 3

    page.click("#agents-hours button:nth-child(1)")
    page.until("STATE.agents.window_hours === 24")
    assert last_request(page)["url"] == "/api/agents?hours=24"
    assert page.eval(
        "document.querySelectorAll('#agents-chart g.tbar').length") == 2, \
        "24 hours of this fixture drops the tick from the night before"
    assert page.eval(
        "document.querySelector('#agents-hours button:nth-child(1)')"
        ".getAttribute('aria-pressed')") == "true"


@needs_chrome
def test_the_incident_screen_names_the_ticks_that_wrote_its_page(browser,
                                                                 tmp_path):
    """The incident half of the same join. The stage on this page's own
    database is marked; the rest of the tick stays legible around it, because
    nothing records which stage of a tick wrote which file."""
    page = browser
    page.goto(mock_page(tmp_path, "touches").as_uri() + "#/incident/" + SLUG)
    page.until("STATE.screen === 'incident'")

    rows = page.eval(TEXTS % "#incident-touches .tid")
    assert rows == [RUN_ID[:7], "0011223"], "newest tick first, as sent"
    said = page.eval(TEXTS % "#incident-touches tbody tr.stage td.tstage")
    assert len(said) == 3, "the whole tick, not a guess at one stage"
    assert said[0] == "ingest", "the stage is its own column now"
    first = page.eval(TEXTS % "#incident-touches tbody tr.stage:"
                              "nth-child(1) td")
    assert first[3] == "cdb1" and first[5] == "363", \
        f"the database and the seconds are each a cell, said {first}"
    assert page.eval(
        "document.querySelectorAll('#incident-touches tbody tr.stage.same')"
        ".length") == 1, \
        "one stage names this page's database, and it is the marked one"
    assert page.eval(TEXTS % "#incident-touches tbody td.outcome") \
        == ["ok", "validation failed", "rolled back"], \
        "the outcome is a word in its own column, never a colour alone"
    assert page.eval("document.querySelectorAll("
                     "'#incident-touches tbody tr.stage td.trace a').length") \
        == 3, "every stage of the tick links its own trace"
    widths = page.eval(
        "Array.from(document.querySelectorAll('#incident-touches .bar'))"
        ".map(n => parseFloat(n.style.width))")
    assert widths[0] == 120 and widths[0] > widths[1] > widths[2] >= 4, \
        f"one scale across the section, the longest full width, {widths}"

    gone = page.eval(TEXTS % "#incident-touches tbody tr.gone td.said")
    assert gone == ["the logs no longer hold this tick's stages"], \
        "the commit is proof the tick wrote the page, stages or not"


@needs_chrome
def test_an_incident_no_tick_wrote_says_it_was_written_by_hand(browser,
                                                              tmp_path):
    page = browser
    hand = {**INCIDENT, "touched_by": []}
    page.goto(mock_page(tmp_path, "byhand", incident=hand).as_uri()
              + "#/incident/" + SLUG)
    page.until("STATE.screen === 'incident'")
    assert page.eval("document.getElementById('touches-none').hidden") is False
    assert page.eval(
        "document.querySelectorAll('#incident-touches li').length") == 0


TAGS = "Array.from(document.querySelectorAll('#links-board .ltag'))" \
       ".map(n => n.textContent)"


@needs_chrome
def test_the_links_screen_draws_the_board_the_operator_wrote(browser,
                                                            tmp_path):
    page = browser
    page.goto(mock_page(tmp_path, "links").as_uri())
    page.until("STATE.screen === 'queue'")
    page.click("#nav-links")
    page.until("STATE.screen === 'links'")

    assert hash_of(page) == "#/links", "the screen has an address of its own"
    assert page.eval(
        "document.getElementById('nav-links').getAttribute('aria-current')") \
        == "page", "the tab the operator is on says so"
    assert page.eval("document.getElementById('title').textContent") \
        == LINKS["title"], "the board's own title heads the screen"
    assert page.eval(TEXTS % "#links-board .cap") \
        == ["Dashboards", "Repositories"], \
        "every section of the board is drawn, in the order of the file"
    assert page.eval(
        "document.querySelectorAll('#links-board .lnote').length") == 1, \
        "only the section that was given a note draws one"

    first = "#links-board li.lrow:nth-child(1) .lurl"
    assert page.eval("document.querySelector('%s').getAttribute('href')"
                     % first) == LINKS["sections"][0]["links"][0]["url"], \
        "a row's anchor points at the url the board gave"
    assert page.eval("document.querySelector('%s').getAttribute('target')"
                     % first) == "_blank", \
        "a link off the workbench opens beside it, not over it"
    assert page.eval(TAGS) == ["tailscale", "artifact", "github"], \
        "each row carries its tag as the word the operator wrote"

    codes = page.eval(TEXTS % "#links-board .lwhat code")
    assert codes == ["oracle_error"], \
        "a backticked word in an explanation becomes a code element"
    assert page.eval(TEXTS % "#links-intro code") == ["tailscale"], \
        "and so does one in the intro"
    assert page.eval("document.getElementById('links-empty').hidden"), \
        "a board with sections says nothing about being empty"


@needs_chrome
def test_a_board_with_no_sections_says_so_and_draws_nothing(browser, tmp_path):
    page = browser
    empty = {"title": "Links", "intro": "", "sections": []}
    page.goto(mock_page(tmp_path, "links-empty", links=empty).as_uri())
    page.until("STATE.screen === 'queue'")
    page.click("#nav-links")
    page.until("STATE.screen === 'links'")

    assert not page.eval("document.getElementById('links-empty').hidden"), \
        "a checkout with no config/links.yaml is told where to write one"
    assert "add config/links.yaml" in page.eval(
        "document.getElementById('links-empty').textContent"), \
        "and the line names the file"
    assert page.eval("document.querySelectorAll('#links-board *').length") \
        == 0, "an empty board draws no section and no row"
    assert page.eval("document.getElementById('links-intro').hidden"), \
        "and no intro paragraph either"


#: The window `portal_shots` photographs at, and the one the layout is drawn
#: for. Anything wider than this is a table or a chart nobody can read
#: without dragging the page sideways.
WIDE = "[document.documentElement.scrollWidth, window.innerWidth]"


EXPANDED = ("Array.from(document.querySelectorAll('%s'))"
            ".map(n => n.getAttribute('aria-expanded'))")

DB_HASH = "#/db/" + DB_NAME

LIVE_ROWS = "document.querySelectorAll('%s tbody tr:not(.day)').length"


def big_db(browser, tmp_path, name, **over):
    """The database screen on the live-shaped fixture, painted and settled."""
    page = browser
    page.goto(mock_page(tmp_path, name, db=BIG_DB, heat=BIG_HEAT,
                        **over).as_uri() + DB_HASH)
    page.until("STATE.screen === 'db'")
    page.until("STATE.heat30.data !== null || STATE.heat30.refused")
    return page


def band_sum(name):
    return sum(count or 0 for count in big_band(name))


@needs_chrome
def test_a_database_opens_with_five_numbers_and_the_host_it_runs_on(browser,
                                                                    tmp_path):
    """What the screen is for. Before any of it is read, the operator is told
    how much is open, how much has been seen, how loud the month was, and how
    fresh the two things a person writes are."""
    page = big_db(browser, tmp_path, "dbtiles")
    assert page.eval("document.getElementById('db-host').textContent") \
        == BIG_HOST, "the host comes off the heat row for this database"
    assert page.eval(TEXTS % "#db-tiles dt") == [
        "open incidents", "error codes", "errors · 30 d", "last journal",
        "page written"]
    assert page.eval(TEXTS % "#db-tiles .v") == [
        str(BIG_OPEN), str(BIG_CODES_HELD), str(band_sum("error")),
        day_words(BIG_JOURNAL[0]["day"]), day_words(OWNER_COMMIT["at"])]
    assert page.eval(TEXTS % "#db-tiles .s") == [
        f"{BIG_DONE} resolved", f"{BIG_UNRESEARCHED} unresearched",
        f"{band_sum('warning')} warnings · {band_sum('unmatched')} unmatched",
        f"{BIG_JOURNAL_DAYS} days written",
        f"{OWNER_COMMIT['short']} · hand"]
    assert page.eval(
        "document.querySelectorAll('#db-tiles .tile.hot').length") == 1, \
        "the one count nobody wants above zero is the one drawn hot"


@needs_chrome
def test_the_standing_page_is_a_lead_and_the_rest_behind_disclosures(browser,
                                                                     tmp_path):
    """Four screens of settings, of which the reader wants one paragraph and
    knows which section holds the rest."""
    page = big_db(browser, tmp_path, "dbabout")
    assert page.eval("document.querySelectorAll('#db-about h1').length") == 0, \
        "the screen above already says the database's name"
    assert "Oracle Database 19c" in page.eval(
        "document.querySelector('#db-about .lead').textContent"), \
        "the lead is what the reader came for, and it takes no click"
    assert page.eval(TEXTS % "#db-about .sectx .nm") == \
        [title for title, _, _ in BIG_SECTIONS]
    assert page.eval(TEXTS % "#db-about .sectx .n") == \
        [f"{points} point" + ("" if points == 1 else "s")
         for _, _, points in BIG_SECTIONS]
    assert page.eval(EXPANDED % "#db-about .sectx") == ["false"] * 3
    assert page.eval("document.querySelectorAll('#db-about li').length") == 0

    page.click("#db-about .sectx")
    page.until("STATE.dbSections.size === 1")
    held = BIG_SECTIONS[0][2]
    assert page.eval("document.querySelectorAll('#db-about li').length") \
        == held, "the section the reader asked for is the section it holds"
    page.eval("render()")
    assert page.eval("document.querySelectorAll('#db-about li').length") \
        == held, "and it is still open after a redraw"
    assert page.eval(
        "document.querySelector('#db-about .foot .path').textContent") \
        == BIG_PAGE["path"]


@needs_chrome
def test_the_incidents_open_the_live_ones_and_fold_the_resolved(browser,
                                                                tmp_path):
    page = big_db(browser, tmp_path, "dbincidents")
    rows = LIVE_ROWS % "#db-rows"
    assert page.eval(rows) == BIG_OPEN, \
        "fifteen rows, and no card repeating the name of the database"
    assert page.eval(
        "document.getElementById('db-incidents-said').textContent") \
        == f"{BIG_OPEN} open · {BIG_DONE} resolved"
    said = page.eval(
        "document.querySelector('#db-rows tr.day .dayx').textContent")
    assert f"{BIG_DONE} resolved" in said \
        and day_words(BIG_RESOLVED_LAST) in said, \
        f"the fold says what is under it, said {said!r}"
    page.click("#db-rows tr.day .dayx")
    page.until(f"{rows} === {BIG_OPEN + BIG_DONE}")

    page.click("#db-rows tbody td.what a")
    page.until("STATE.screen === 'incident'")
    assert last_request(page)["url"] == f"/api/incidents/{SLUG}", \
        "the row opens the incident by the slug the row carried"


@needs_chrome
def test_the_error_codes_open_twelve_rows_and_compare_them(browser, tmp_path):
    page = big_db(browser, tmp_path, "dbcodes")
    rows = LIVE_ROWS % "#db-errors"
    assert page.eval(rows) == 12
    assert page.eval("document.getElementById('db-errors-said').textContent") \
        == f"{BIG_CODES_HELD} codes · newest first"
    said = page.eval(
        "document.querySelector('#db-errors tr.day .dayx').textContent")
    assert f"{BIG_CODES_HELD - 12} more codes" in said \
        and day_words(BIG_CODE_ROWS[12]["last_day"]) in said, \
        f"the fold says what is under it, said {said!r}"
    wide = page.eval("Array.from(document.querySelectorAll("
                     "'#db-errors td.cbar i')).map(n => n.offsetWidth)")
    assert wide == sorted(wide, reverse=True) and wide[0] > wide[-1], \
        "the bar is the count and nothing else, so it falls as the count does"
    page.click("#db-errors tr.day .dayx")
    page.until(f"{rows} === {BIG_CODES_HELD}")


@needs_chrome
def test_the_journal_opens_eight_days_and_reads_as_headlines(browser,
                                                             tmp_path):
    page = big_db(browser, tmp_path, "dbjournal")
    rows = LIVE_ROWS % "#db-journal"
    assert page.eval(rows) == 8
    said = page.eval(
        "document.querySelector('#db-journal tr.day .dayx').textContent")
    assert f"{BIG_JOURNAL_DAYS - 8} earlier days" in said \
        and day_words(BIG_JOURNAL[-1]["day"]) in said, \
        f"the fold says how far back it goes, said {said!r}"
    assert page.eval("document.querySelector("
                     "'#db-journal tbody td.clip a').getAttribute('title')") \
        == BIG_JOURNAL[0]["headline"], \
        "the headline is clipped to one line, and the whole of it is a title"

    page.click("#db-journal tbody td.clip a")
    page.until("window.__requests.some(r => r.url.indexOf('/api/page?') === 0)")
    assert last_request(page)["url"] == "/api/page?" + urllib.parse.urlencode(
        {"path": BIG_JOURNAL[0]["path"]}), \
        "the headline opens the journal page it was written on"


@needs_chrome
def test_the_month_of_log_lines_draws_three_bands_over_thirty_days(browser,
                                                                   tmp_path):
    page = big_db(browser, tmp_path, "dbbands")
    assert page.eval(
        "document.querySelectorAll('#db-bands .hband-svg').length") == 3
    assert page.eval("document.querySelectorAll('#db-bands rect').length") \
        == 3 * 30
    assert page.eval(
        "document.querySelectorAll('#db-bands rect.blank').length") \
        == 3 * len(BIG_SILENT), \
        "a day the wiki holds no digest for is hatched in every band"
    assert page.eval(TEXTS % "#db-bands .hname") == \
        ["errors", "warnings", "unmatched lines"]
    most = max(count or 0 for name in heat.CLASSES for count in big_band(name))
    assert page.eval("document.getElementById('db-heat-note').textContent") \
        == f"darkest cell = {most} lines · hatched = no digest", \
        "the caption says what the darkest end of the ramp is worth"


@needs_chrome
def test_a_refused_month_leaves_the_database_screen_standing(browser,
                                                             tmp_path):
    """The counts are a second question about the same wiki. A database the
    operator cannot read the month of is still a database with fifteen open
    incidents on it."""
    page = big_db(browser, tmp_path, "dbnoheat", refuse=["/api/heat"])
    assert page.eval(TEXTS % "#db-tiles .v")[2] == "—"
    assert page.eval(TEXTS % "#db-tiles .s")[2] == "no 30-day counts"
    assert page.eval("document.getElementById('db-heat-note').textContent") \
        == "no 30-day counts"
    assert page.eval("document.querySelectorAll('#db-bands rect').length") == 0
    assert page.eval(LIVE_ROWS % "#db-rows") == BIG_OPEN, \
        "the tables are read off the envelope and never waited for the counts"


@needs_chrome
def test_a_database_says_which_other_pages_name_it(browser, tmp_path):
    page = big_db(browser, tmp_path, "dblinked")
    assert page.eval(TEXTS % "#db-linked .dbchip") == [BIG_STANDBY], \
        "the other database is an address; the sixteen reports are a count"
    assert f"{len(BIG_REPORTS)} pages link here" in page.eval(
        "document.getElementById('db-linked').textContent")
    assert page.eval("document.querySelector('#db-linked .dbchip')"
                     ".getAttribute('href')") == f"#/db/{BIG_STANDBY}"
    page.click("#db-linked .dbchip")
    page.until("window.__requests.some("
               f"r => r.url === '/api/db?name={BIG_STANDBY}')")


@needs_chrome
def test_a_live_database_screen_is_a_few_screens_tall_at_1280(browser,
                                                              tmp_path):
    """Six and a half thousand pixels was the whole complaint. Everything
    past the first screen is now behind a disclosure the reader opens."""
    page = big_db(browser, tmp_path, "dbtall")
    tall = page.eval("Math.ceil(document.documentElement.scrollHeight)")
    assert tall < 3600, f"the database screen is {tall}px tall at 1280"


#: Every read screen and the address that opens it. The queue is where the
#: page boots, so it has none.
READ_SCREENS = [
    ("queue", None),
    ("agents", "#/agents"),
    ("fleet", "#/fleet"),
    ("inbox", "#/inbox"),
    ("review", "#/review/" + BIG_REVIEW_ID),
    ("runs", "#/runs"),
    ("run", "#/run/" + RUN_ID),
    ("db", "#/db/" + DB_NAME),
    ("wikipage", "#/page/" + PAGE_PATH),
    ("search", "#/search/" + SEARCH["query"]),
    ("links", "#/links"),
]


@needs_chrome
def test_no_read_screen_scrolls_sideways_at_1280(browser, tmp_path):
    """A horizontal scrollbar on a console is a column the operator will not
    find. Every read screen is checked, because the wide things are tables
    and every screen but two carries one."""
    page = browser
    page.goto(mock_page(tmp_path, "wide", fleet=BIG_FLEET, runs=BIG_RUNS,
                        db=BIG_DB, heat=BIG_HEAT, week=BIG_REVIEW,
                        inbox=BIG_INBOX).as_uri())
    page.until("STATE.screen === 'queue'")
    page.until("STATE.heat.data !== null")
    for screen, where in READ_SCREENS:
        if where:
            page.eval("location.hash = " + json.dumps(where))
            page.until(f"STATE.screen === {json.dumps(screen)}")
        wide, window = page.eval(WIDE)
        assert wide <= window, \
            f"the {screen} screen is {wide}px wide in a {window}px window"

    page.click("#nav-queue")
    page.until("STATE.screen === 'queue'")
    open_incident(page)
    wide, window = page.eval(WIDE)
    assert wide <= window, \
        f"the incident screen is {wide}px wide in a {window}px window"
