"""The workbench API, driven over a real loopback socket.

Every test here binds an ephemeral port, runs `serve_forever` on a daemon
thread and speaks HTTP to it, because the things worth proving are the ones
that only exist over the wire: the Host and content-type baseline, the status
a refused transaction maps to, and the audit line a click leaves behind.
Nothing fakes git or lint; a stub would pin the portal's opinion of an outcome
rather than the outcome the wiki produces.

`http.client` rather than `urllib.request` because half of the baseline is
about headers a well-behaved client would never send, and it is the stdlib
layer that lets a test state every header exactly.
"""

import base64
import datetime as dt
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

import pytest

from dbwiki import (alerts, events, gitutil, incident_action, incidents,
                    lifecycle, monitoring)
from dbwiki import observability
from dbwiki import readmodel
from dbwiki import transaction
from dbwiki.config import load_config
from dbwiki.health import (AGENT_LOG, HEALTH_LOG, MAX_AGENT_EVENTS,
                           MAX_EVENTS, MAX_RUN_START_EVENTS, RUN_STARTS_LOG)
from dbwiki.evidence_ref import (Entity, EvidenceRef, Signature, Window,
                                 render_ref)
from dbwiki.incidents import (ErrorAbsent, MonitoringWindow, Status,
                              read_incident, set_status)
from dbwiki.lock import single_flight
from dbwiki.portal import server as portal
from dbwiki.portal import ui, wire
from dbwiki.portal.identity import (ROLE_OF, Principal, Role,
                                    TrustedOperator)
from fixtures.incident_pages import incident_page

EMAIL = "dba@example.com"
DB = "cdb1"
CODE = "TNS-12564"

OPEN_SLUG = "2026-08-05-cdb1-tns-12564"
MON_SLUG = "2026-08-10-cdb1-ora-600"
DONE_SLUG = "2026-07-01-cdb1-old-news"
#: A second incident closed against CODE, which the fleet wiki adds so that
#: the error page's resolution history has two rows and the order the views
#: put them in is observable.
OLD_FIX_SLUG = "2026-08-01-cdb1-tns-12564"

OPEN_PATH = f"incidents/{OPEN_SLUG}.md"
MON_PATH = f"incidents/{MON_SLUG}.md"

DIGEST = f"digests/{DB}/2026-08-30.md"
AT = "2026-08-30T14:22:10Z"
START = "2026-08-28T00:00:00Z"
UNTIL = "2026-09-02T00:00:00Z"

INTENT = "stop the standby losing its connection every night"
SUMMARY = "restarted the standby listener"

CONFIG = """\
elasticsearch: {url: "http://127.0.0.1:9200"}
sources: {}
wiki_repo: wiki
state_dir: .state
digest_dir: digests
report: {push: false, link_base: "https://example.invalid/blob/main"}
portal: {lock_wait_s: 1.0}
"""


KIBANA = "https://kibana.example.invalid"
LANGFUSE = "http://langfuse.example.invalid:3000"

LINKED_CONFIG = CONFIG.replace("portal: {lock_wait_s: 1.0}", f"""\
portal:
  lock_wait_s: 1.0
  links:
    kibana:
      base: "{KIBANA}"
      data_views: {{oracle-logs: dv-oracle, dbwiki: dv-dbwiki}}
    langfuse: {{base: "{LANGFUSE}", project: dbwiki}}
""")

EVIDENCE_BLOCK = render_ref(EvidenceRef(
    schema_version=1, kind="elastic_filter",
    template_id="oracle-error-context-v1", environment="production",
    data_view="oracle-logs", entity=Entity("database", DB),
    signature=Signature("oracle_error", CODE),
    window=Window("2026-08-30T10:00:00Z", "2026-08-30T10:20:00Z"),
    summary=f"{CODE} connect failures on {DB}"))

REPORT_PATH = "reports/2026-08-30-0615.md"

REPORT = f"""---
type: report
window_start: 2026-08-29T06:15:00Z
window_end: 2026-08-30T06:15:00Z
generated: 2026-08-30T06:15:41Z
---

# Fleet report {DB} 2026-08-30 06:15

One window over one fleet, and what it did overnight.

## Summary

| db | events | code |
|---|---|---|
| {DB} | 41 | {CODE} |

The open one is [[incidents/{OPEN_SLUG}]] and [[nothing/here]] is no page.
"""

INDEX = f"""---
type: index
---

# Logbook

## Open incidents

- [[incidents/{OPEN_SLUG}]] connect failures
- [[incidents/{MON_SLUG}]] internal errors

## Resolved incidents

- [[incidents/{DONE_SLUG}]] old news
"""


def git(repo, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _monitoring_page() -> str:
    window = MonitoringWindow(ErrorAbsent("ORA-00600"), START, UNTIL)
    return set_status(incident_page(DB, "ORA-600 internal errors"),
                      Status.MONITORING, updated=START, monitoring=window)


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A project root whose wiki holds one open incident, one under
    monitoring, one resolved, the error page the open one links, an index
    listing all three, a digest they may cite and one overnight report."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "dbwiki.yaml").write_text(CONFIG)
    wiki = tmp_path / "wiki"
    (wiki / "incidents").mkdir(parents=True)
    (wiki / "errors").mkdir()
    (wiki / "reports").mkdir()
    (wiki / "digests" / DB).mkdir(parents=True)
    (wiki / OPEN_PATH).write_text(
        incident_page(DB, f"{CODE} connect failures", body=EVIDENCE_BLOCK,
                      error_codes=(CODE,)))
    (wiki / MON_PATH).write_text(_monitoring_page())
    (wiki / f"incidents/{DONE_SLUG}.md").write_text(
        incident_page(DB, "old news", status="resolved"))
    (wiki / f"errors/{CODE}.md").write_text(
        f"---\ntype: error-class\n---\n\n# {CODE}\n\n"
        "## Resolution history\n\n"
        "| resolved | db | incident | remediation | evidence |\n"
        "|---|---|---|---|---|\n"
        f"| 2026-07-02 | {DB} | [[incidents/{DONE_SLUG}]] | "
        "restarted the listener | 2026-07-02T09:14:00Z |\n")
    (wiki / DIGEST).write_text(f"# {DB} 2026-08-30\n")
    (wiki / REPORT_PATH).write_text(REPORT)
    (wiki / "index.md").write_text(INDEX)
    (wiki / "log.md").write_text("---\ntype: log\n---\n\n# Log\n")
    git(wiki, "init", "-b", "main")
    git(wiki, "config", "user.email", EMAIL)
    git(wiki, "config", "user.name", "DBA")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "init")
    return tmp_path


class Client:
    """One request per connection, so no test inherits another's state."""

    def __init__(self, address):
        self.host, self.port = address[0], address[1]
        self.authority = f"{self.host}:{self.port}"

    def send(self, method, path, *, body=None, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
        head = {"Host": self.authority, **(headers or {})}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            head.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=payload, headers=head)
            r = conn.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            conn.close()

    def json(self, method, path, *, body=None, headers=None):
        status, raw, _ = self.send(method, path, body=body, headers=headers)
        return status, json.loads(raw or b"{}")

    def get(self, path, **kw):
        return self.json("GET", path, **kw)

    def post(self, path, body, **kw):
        return self.json("POST", path, body=body, **kw)


def serve(cfg, provider=None, now=None):
    srv = portal.make_server(cfg, bind=("127.0.0.1", 0), provider=provider,
                             now=now)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread


@pytest.fixture
def api(root):
    cfg = load_config(root)
    srv, thread = serve(cfg)
    try:
        yield Client(srv.server_address)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


@pytest.fixture
def linked(root):
    """The same server with `portal.links` naming a Kibana and a Langfuse this
    deployment can reach. The config file is rewritten before `load_config`
    rather than the loaded object patched, because what an operator edits is a
    block in `dbwiki.yaml`."""
    (root / "config" / "dbwiki.yaml").write_text(LINKED_CONFIG)
    cfg = load_config(root)
    srv, thread = serve(cfg)
    try:
        yield Client(srv.server_address)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)



def head(root) -> str:
    return transaction.head(root / "wiki")


def event(root) -> dict:
    return json.loads(
        (root / ".state" / HEALTH_LOG).read_text().splitlines()[-1])


def record_body(**over) -> dict:
    body = {"verb": "record-action",
            "fields": {"intent": INTENT, "summary": SUMMARY}}
    body.update(over)
    return body


PREVIEW = f"/api/incidents/{OPEN_SLUG}/preview"
COMMIT = f"/api/incidents/{OPEN_SLUG}/commit"


def test_the_queue_is_the_active_incidents_in_wiki_path_order(api, root):
    status, body = api.get("/api/incidents")
    assert status == 200
    assert body["revision"] == head(root)
    assert [r["slug"] for r in body["incidents"]] == [OPEN_SLUG, MON_SLUG]
    assert body["strays"] == []
    row = body["incidents"][0]
    assert row["status"] == "open" and row["label"] == "Open incident"
    assert row["db"] == DB and row["dirty"] is False
    assert row["error_codes"] == [CODE]
    assert body["incidents"][1]["error_codes"] == []
    assert row["allowed"] == ["record-action", "monitor", "resolve",
                              "merge"]


def test_the_resolved_incidents_arrive_only_when_they_are_asked_for(api):
    _, active = api.get("/api/incidents")
    _, everything = api.get("/api/incidents?all=1")
    assert DONE_SLUG not in [r["slug"] for r in active["incidents"]]
    assert [r["slug"] for r in everything["incidents"]] == [
        DONE_SLUG, OPEN_SLUG, MON_SLUG]
    done = everything["incidents"][0]
    assert done["allowed"] == ["record-action", "reopen"]


def test_a_page_only_the_working_tree_holds_is_listed_dirty_and_not_openable(
        api, root):
    """The ingest agent creates incident pages and no tick has committed them
    yet. An operator must be able to find the incident they just watched
    appear, so it is listed; and the workbench must not publish over bytes it
    cannot show, so opening it is a conflict rather than a miss."""
    slug = "2026-08-31-cdb1-brand-new"
    (root / "wiki" / f"incidents/{slug}.md").write_text(
        incident_page(DB, "brand new"))

    _, queue = api.get("/api/incidents")
    row = next(r for r in queue["incidents"] if r["slug"] == slug)
    assert row["dirty"] is True
    assert queue["strays"] == [f"incidents/{slug}.md"]
    assert [r["dirty"] for r in queue["incidents"] if r["slug"] != slug] \
        == [False, False]

    status, body = api.get(f"/api/incidents/{slug}")
    assert status == 409 and body["error"] == "uncommitted_page"
    assert body["paths"] == [f"incidents/{slug}.md"]
    assert "git" in body["cli"] and slug in body["cli"]


def test_a_slug_the_wiki_never_held_is_a_miss(api):
    status, body = api.get("/api/incidents/2020-01-01-nope-nope")
    assert status == 404 and body["error"] == "no_such_incident"


def test_a_slug_that_tries_to_leave_the_incident_directory_is_a_miss(api):
    status, body = api.get("/api/incidents/..%2F..%2Fetc%2Fpasswd")
    assert status == 404 and body["error"] == "no_such_incident"


def test_an_incident_is_the_committed_page_with_its_history_and_references(
        api, root):
    status, body = api.get(f"/api/incidents/{OPEN_SLUG}")
    assert status == 200
    assert body["revision"] == head(root) and body["head"] == body["revision"]
    assert body["slug"] == OPEN_SLUG and body["path"] == OPEN_PATH
    assert body["status"] == "open" and body["label"] == "Open incident"
    assert body["unknown_status"] is None and body["db"] == DB
    assert body["error_codes"] == [CODE]
    assert body["window"] is None and body["closure"] is None
    assert body["actions"] == [] and body["problems"] == []
    assert body["dirty"] is False and body["strays"] == []
    assert body["body"] == (root / "wiki" / OPEN_PATH).read_text()

    assert [c["subject"] for c in body["history"]] == ["init"]
    assert body["history"][0]["actor"] == ""

    refs = {r["path"]: r for r in body["references"]}
    assert refs[OPEN_PATH]["kind"] == "page" and refs[OPEN_PATH]["exists"]
    assert refs[f"errors/{CODE}.md"]["kind"] == "error"
    assert refs[OPEN_PATH]["url"] == \
        f"https://example.invalid/blob/main/{OPEN_PATH}"


def test_the_incident_view_carries_the_research_of_each_code_it_names(
        api, root):
    """One code researched, one code with no page at all: the row set the
    case file draws its cards from has to say which is which."""
    wiki = root / "wiki"
    (wiki / f"errors/{CODE}.md").write_text(
        f"---\ntype: error-class\nresearched: 2026-08-17\n---\n\n# {CODE}\n\n"
        "## Reference\n\n**Cause:** the listener refused the connection\n"
        "(source: sources/oracle-docs).\n\n"
        "**Action:** check the listener is up\n"
        "(source: sources/oracle-docs).\n\n"
        "**Practitioner note:** the listener queue fills long before it "
        "refuses\n"
        "(source: sources/jonathan-lewis; url: https://example.invalid/q; "
        "accessed: 2026-08-17).\n")
    (wiki / OPEN_PATH).write_text(incident_page(
        DB, "x", error_codes=(CODE, "ORA-00600")))
    git(wiki, "commit", "-am", "research the connect failure")
    _, body = api.get(f"/api/incidents/{OPEN_SLUG}")
    rows = {row["code"]: row for row in body["research"]}
    assert [row["code"] for row in body["research"]] == body["error_codes"]
    assert rows[CODE]["researched"] == "2026-08-17"
    assert rows[CODE]["exists"] is True
    assert rows[CODE]["cause"] == "the listener refused the connection"
    assert rows[CODE]["action"] == "check the listener is up"
    assert rows[CODE]["citations"] == [
        {"source": "sources/oracle-docs", "url": "", "accessed": ""}]
    assert rows[CODE]["notes"] == [
        {"source": "sources/jonathan-lewis",
         "url": "https://example.invalid/q", "accessed": "2026-08-17",
         "text": "the listener queue fills long before it refuses"}], \
        "what a practitioner adds to Oracle's own text reaches the card"
    assert rows["ORA-00600"] == {
        "code": "ORA-00600", "path": "errors/ORA-00600.md", "exists": False,
        "researched": "", "cause": "", "action": "", "citations": [],
        "notes": [], "resolutions": []}


def test_the_incident_view_carries_what_worked_the_last_time(api):
    """The fixture's error page records one fix, so the card beside the chip
    names the case file it came out of as well as the remedy."""
    _, body = api.get(f"/api/incidents/{OPEN_SLUG}")
    rows = {row["code"]: row for row in body["research"]}
    assert rows[CODE]["resolutions"] == [
        {"day": "2026-07-02", "db": DB, "incident": DONE_SLUG,
         "path": f"incidents/{DONE_SLUG}.md",
         "remediation": "restarted the listener",
         "evidence": "2026-07-02T09:14:00Z"}]


def test_a_reference_the_revision_does_not_hold_is_reported_absent(api, root):
    (root / "wiki" / OPEN_PATH).write_text(incident_page(
        DB, "x", error_codes=(CODE, "ORA-00600")))
    git(root / "wiki", "commit", "-am", "link a second error class")
    _, body = api.get(f"/api/incidents/{OPEN_SLUG}")
    refs = {r["path"]: r for r in body["references"]}
    assert refs[f"errors/{CODE}.md"]["exists"] is True
    assert refs["errors/ORA-00600.md"]["exists"] is False


def test_every_command_the_state_machine_knows_has_a_role():
    """`Workbench` indexes `ROLE_OF` by command type with no default, so a
    command `lifecycle` gained and this table did not would `KeyError` out of
    every incident GET rather than fail closed. Adding `Merge` did exactly
    that until its row landed."""
    assert set(ROLE_OF) == set(lifecycle.KINDS)


def test_every_allowed_verb_carries_its_role_and_the_form_that_submits_it(api):
    _, body = api.get(f"/api/incidents/{OPEN_SLUG}")
    rows = {r["verb"]: r for r in body["allowed"]}
    assert list(rows) == ["record-action", "monitor", "resolve", "merge"]
    assert rows["resolve"]["role"] == "closer"
    assert rows["merge"]["role"] == "closer"
    assert rows["record-action"]["role"] == "operator"
    assert all(r["permitted"] is True for r in rows.values())

    fields = rows["record-action"]["fields"]
    assert [f["name"] for f in fields] == [
        s.name for s in incident_action.command_fields(lifecycle.RecordAction)]
    intent = fields[0]
    assert intent["required"] is True and intent["default"] is None
    assert intent["widget"] == "line" and intent["type"] == "string"
    assert intent["cli_flag"] == "--intent"
    assert intent["help"] == incident_action.PRESENTATION[
        ("record-action", "intent")].help
    outcome = next(f for f in fields if f["name"] == "outcome")
    assert outcome["choices"] == ["pending", "succeeded", "failed", "rejected"]
    assert outcome["default"] == "pending"
    evidence = next(f for f in fields if f["name"] == "evidence")
    assert evidence["type"] == "list" and evidence["default"] == []

    pages = next(f for f in rows["resolve"]["fields"]
                 if f["name"] == "update_error_pages")
    assert pages["type"] == "boolean" and pages["default"] is True
    assert pages["cli_flag"] == "--no-error-pages"


def test_an_off_vocabulary_status_offers_no_verbs_at_all(api, root):
    """`Incident` reads an unknown status as open for display, but
    `lifecycle.build` refuses the page, so a button for it would be a lie."""
    text = (root / "wiki" / OPEN_PATH).read_text()
    (root / "wiki" / OPEN_PATH).write_text(
        text.replace("status: open", "status: wedged"))
    git(root / "wiki", "commit", "-am", "a status nobody defined")
    _, body = api.get(f"/api/incidents/{OPEN_SLUG}")
    assert body["unknown_status"] == "wedged"
    assert body["allowed"] == []
    _, queue = api.get("/api/incidents")
    row = next(r for r in queue["incidents"] if r["slug"] == OPEN_SLUG)
    assert row["allowed"] == []


def _write_facts(root, *, source_revision):
    inc = read_incident((root / "wiki" / MON_PATH).read_text(), MON_PATH)
    facts = monitoring.evaluate(inc, [], now="2026-09-03T00:00:00Z",
                                source_revision=source_revision).to_dict()
    d = root / ".state" / monitoring.MONITORING_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{MON_SLUG}.json").write_text(json.dumps(facts))
    return facts


def test_a_monitoring_incident_carries_the_closure_case_and_its_staleness(
        api, root):
    facts = _write_facts(root, source_revision="0" * 40)
    _, body = api.get(f"/api/incidents/{MON_SLUG}")
    closure = body["closure"]
    assert closure["verdict"] == facts["verdict"]
    assert closure["evaluated_at"] == facts["evaluated_at"]
    assert closure["stale"] is True
    assert closure["against"]
    assert closure["facts"] == facts
    assert body["window"]["kind"] == "error_absent"
    assert body["window"]["code"] == "ORA-00600"
    assert body["window"]["until"] == UNTIL

    _write_facts(root, source_revision=head(root))
    _, fresh = api.get(f"/api/incidents/{MON_SLUG}")
    assert fresh["closure"]["stale"] is False


def test_a_monitoring_incident_no_tick_has_evaluated_carries_no_closure(api):
    _, body = api.get(f"/api/incidents/{MON_SLUG}")
    assert body["closure"] is None


def test_a_preview_echoes_the_normalised_fields_and_writes_nothing(api, root):
    base = head(root)
    status, body = api.post(PREVIEW, record_body(fields={
        "intent": INTENT, "summary": SUMMARY, "outcome": "pending",
        "evidence": [DIGEST]}))
    assert status == 200
    assert body["verb"] == "record-action"
    assert body["fields"] == {"intent": INTENT, "summary": SUMMARY,
                              "evidence": [DIGEST]}
    assert body["base"] == base and body["head"] == base
    assert incidents.ISO_Z_RE.match(body["at"])
    assert body["status_after"] == "open"
    assert body["blocked"] is False and body["strays"] == []
    assert body["nothing_to_do"] is False
    assert sorted(body["paths"]) == sorted([OPEN_PATH, "log.md"])
    assert f"+## Action {body['at']}" in body["diff"]
    assert body["actor"]["email"] == EMAIL
    assert head(root) == base
    assert transaction.stray_paths(root / "wiki") == ()
    assert not (root / ".state" / HEALTH_LOG).exists()


def test_the_preview_hands_back_the_cli_line_that_publishes_it(
        api, root, monkeypatch, capsys):
    """"The CLI is the fallback" is a copy-paste, not a promise."""
    import shlex

    from dbwiki import cli

    _, body = api.post(PREVIEW, record_body())
    line = body["cli"]
    assert line.startswith("dbwiki incident record-action ")
    assert f"--at {body['at']}" in line and "--commit" in line
    monkeypatch.chdir(root)
    assert cli.main(shlex.split(line)[1:]) == 0
    capsys.readouterr()
    page = (root / "wiki" / OPEN_PATH).read_text()
    assert f"## Action {body['at']}" in page


def test_a_preview_that_lint_would_block_is_still_a_200(api, root):
    """`blocked` is a field, not a status: the operator gets the diff and the
    finding, and the edit that fixes it is theirs to make."""
    _, body = api.post(PREVIEW, record_body(fields={
        "intent": INTENT, "summary": SUMMARY,
        "evidence": ["digests/cdb1/2026-01-01.md"]}))
    assert body["blocked"] is True
    assert [f["rule"] for f in body["findings"]] == ["digest-missing"]
    assert body["diff"]
    assert head(root) == body["base"]


def test_a_verb_the_incident_cannot_take_names_the_transition(api):
    status, body = api.post(f"/api/incidents/{OPEN_SLUG}/preview",
                            {"verb": "extend",
                             "fields": {"until": UNTIL, "intent": INTENT}})
    assert status == 422 and body["error"] == "transition_refused"
    assert "ExtendMonitoring is not valid from status" in body["message"]


def test_a_field_the_verb_has_no_home_for_names_the_field(api):
    status, body = api.post(PREVIEW, record_body(
        fields={"intent": INTENT, "summary": SUMMARY, "sumary": "typo"}))
    assert status == 422 and body["error"] == "field"
    assert body["field"] == "sumary"


def test_an_unknown_verb_is_refused_before_anything_is_built(api):
    status, body = api.post(PREVIEW, {"verb": "nuke", "fields": {}})
    assert status == 422 and body["error"] == "bad_verb"


def test_a_commit_without_the_previews_base_and_at_is_refused(api, root):
    status, body = api.post(COMMIT, record_body())
    assert status == 400 and body["error"] == "preview_first"
    assert not (root / ".state" / HEALTH_LOG).exists()


def test_a_commit_publishes_the_previewed_bytes_and_reports_the_push(
        root, monkeypatch):
    monkeypatch.setattr(gitutil, "push_best_effort", lambda repo: True)
    cfg = load_config(root)
    cfg.portal["push"] = True
    srv, thread = serve(cfg)
    try:
        client = Client(srv.server_address)
        base = head(root)
        _, pv = client.post(PREVIEW, record_body())
        status, body = client.post(
            COMMIT, record_body(base=pv["base"], at=pv["at"]))
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)
    assert status == 200
    assert body["pushed"] is True
    assert body["base"] == base and body["at"] == pv["at"]
    assert sorted(body["paths"]) == sorted(pv["paths"])
    assert body["sha"] == git(root / "wiki", "rev-parse",
                              "--short", "HEAD").strip()
    assert f"## Action {pv['at']}" in (root / "wiki" / OPEN_PATH).read_text()
    assert transaction.stray_paths(root / "wiki") == ()


def test_the_audit_line_for_a_portal_commit_names_the_surface(api, root):
    base = head(root)
    _, pv = api.post(PREVIEW, record_body())
    _, ok = api.post(COMMIT, record_body(base=pv["base"], at=pv["at"]))
    ev = event(root)
    assert ev["command"] == "incident"
    assert ev["outcome"] == "ok" and ev["error_category"] is None
    assert ev["facts"] == {
        "incident": OPEN_SLUG, "command": "record-action", "actor": EMAIL,
        "base": base, "at": pv["at"], "validation": "ok", "pushed": False,
        "surface": "portal",
        "commit": git(root / "wiki", "rev-parse", "--short", "HEAD").strip()}


def test_replaying_the_same_action_against_the_new_head_writes_nothing(
        api, root):
    """The page's own convergence path: a 409 sends it back for the new base,
    it re-previews with the same `at`, and the record it already wrote is
    recognised rather than written twice."""
    _, pv = api.post(PREVIEW, record_body())
    api.post(COMMIT, record_body(base=pv["base"], at=pv["at"]))
    landed = head(root)

    _, again = api.post(PREVIEW, record_body(base=landed, at=pv["at"]))
    assert again["nothing_to_do"] is True and again["paths"] == []
    status, body = api.post(COMMIT, record_body(base=landed, at=pv["at"]))
    assert status == 200 and body["nothing_to_do"] is True
    assert body["base"] == landed and body["at"] == pv["at"]
    assert head(root) == landed


def test_a_commit_after_someone_else_publishes_is_a_conflict_and_writes_nothing(
        api, root):
    _, pv = api.post(PREVIEW, record_body())
    git(root / "wiki", "commit", "--allow-empty", "-m", "a tick landed")
    moved = head(root)
    status, body = api.post(COMMIT, record_body(base=pv["base"], at=pv["at"]))
    assert status == 409 and body["error"] == "base_moved"
    assert body["expected"] == pv["base"] and body["actual"] == moved
    assert head(root) == moved
    assert f"## Action {pv['at']}" not in \
        (root / "wiki" / OPEN_PATH).read_text()
    assert transaction.stray_paths(root / "wiki") == ()
    ev = event(root)
    assert ev["outcome"] == "ok" and ev["facts"]["validation"] == "base_moved"
    assert ev["facts"]["commit"] is None


def test_an_uncommitted_edit_the_workbench_did_not_author_is_a_conflict(
        api, root):
    _, pv = api.post(PREVIEW, record_body())
    (root / "wiki" / "log.md").write_text("---\ntype: log\n---\n\n# Log\n\nx\n")
    status, body = api.post(COMMIT, record_body(base=pv["base"], at=pv["at"]))
    assert status == 409 and body["error"] == "tree_dirty"
    assert body["paths"] == ["log.md"]
    assert event(root)["facts"]["validation"] == "tree_dirty"


def test_a_commit_lint_blocks_leaves_the_tree_untouched(api, root):
    fields = {"intent": INTENT, "summary": SUMMARY,
              "evidence": ["digests/cdb1/2026-01-01.md"]}
    _, pv = api.post(PREVIEW, record_body(fields=fields))
    status, body = api.post(COMMIT, record_body(
        fields=fields, base=pv["base"], at=pv["at"]))
    assert status == 422 and body["error"] == "lint_blocked"
    assert [f["rule"] for f in body["findings"]] == ["digest-missing"]
    assert head(root) == pv["base"]
    assert transaction.stray_paths(root / "wiki") == ()
    assert event(root)["facts"]["validation"] == "lint_blocked"


def test_a_resolve_without_a_residual_risk_is_refused_before_the_wiki_moves(
        api, root):
    """A confirmation a client can skip by not drawing it is not a
    confirmation, so the server insists rather than the page."""
    body = {"verb": "resolve", "fields": {"summary": "listener stable for 72h"}}
    _, pv = api.post(f"/api/incidents/{OPEN_SLUG}/preview", body)
    assert pv["requires"] == ["residual_risk"]

    status, refused = api.post(f"/api/incidents/{OPEN_SLUG}/commit",
                               {**body, "base": pv["base"], "at": pv["at"]})
    assert status == 422 and refused["error"] == "confirmation_required"
    assert refused["field"] == "residual_risk"
    assert head(root) == pv["base"]
    assert not (root / ".state" / HEALTH_LOG).exists()


def test_a_resolve_that_names_the_residual_risk_lands_it_in_the_record(
        api, root):
    body = {"verb": "resolve",
            "fields": {"summary": "listener stable for 72h",
                       "residual_risk": "none identified"}}
    _, pv = api.post(f"/api/incidents/{OPEN_SLUG}/preview", body)
    assert pv["requires"] == []
    status, ok = api.post(f"/api/incidents/{OPEN_SLUG}/commit",
                          {**body, "base": pv["base"], "at": pv["at"]})
    assert status == 200 and ok["sha"]
    page = (root / "wiki" / OPEN_PATH).read_text()
    assert "Residual risk: none identified" in page
    _, view = api.get(f"/api/incidents/{OPEN_SLUG}")
    assert view["status"] == "resolved" and view["label"] == "Resolved"


def _fake_tick(state_dir):
    """A holder of `orchestrator.lock` in another process, so the 423 carries
    the holder sentence a real tick leaves rather than this process's own."""
    code = (
        "import fcntl, json, os, sys, time\n"
        f"fh = open({str(state_dir / 'orchestrator.lock')!r}, 'a+')\n"
        "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
        "fh.seek(0); fh.truncate()\n"
        "fh.write(json.dumps({'pid': os.getpid(), 'command': 'run',\n"
        "                     'since': '2026-08-30T14:00:00Z'})); fh.flush()\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_a_click_that_meets_a_tick_is_a_423_inside_the_configured_wait(
        api, root):
    _, pv = api.post(PREVIEW, record_body())
    (root / ".state").mkdir(exist_ok=True)
    tick = _fake_tick(root / ".state")
    try:
        started = time.monotonic()
        status, body = api.post(
            COMMIT, record_body(base=pv["base"], at=pv["at"]))
        waited = time.monotonic() - started
    finally:
        tick.terminate()
        tick.wait(timeout=10)
    assert status == 423 and body["error"] == "lock_busy"
    assert body["retry_after_s"] == 1.0
    assert 1.0 <= waited < 8.0
    assert "another dbwiki command holds the lock" in body["message"]
    assert f"pid {tick.pid}" in body["message"]
    assert head(root) == pv["base"]
    assert transaction.stray_paths(root / "wiki") == ()
    ev = event(root)
    assert ev["outcome"] == "ok" and ev["error_category"] is None
    assert ev["facts"]["validation"] == "lock_busy"
    assert ev["facts"]["surface"] == "portal"
    assert ev["facts"]["commit"] is None and ev["facts"]["pushed"] is False


def test_the_portal_blocking_itself_says_so_rather_than_naming_its_own_pid(
        api, root):
    """`lock._holder` formats `pid <self>` for a portal-versus-portal
    collision, and shipping that verbatim reads as the portal blocking
    itself."""
    _, pv = api.post(PREVIEW, record_body())
    with single_flight(root / ".state", "portal:resolve:other"):
        status, body = api.post(
            COMMIT, record_body(base=pv["base"], at=pv["at"]))
    assert status == 423
    assert body["message"] == "another workbench request is publishing"
    assert str(os.getpid()) not in body["message"]


def test_a_host_the_server_does_not_answer_to_is_refused(api):
    status, body = api.json("GET", "/api/incidents",
                            headers={"Host": "wiki.example.com"})
    assert status == 403 and body["error"] == "bad_host"


def test_the_three_loopback_spellings_of_the_bound_port_are_answered(api):
    for spelling in ("127.0.0.1", "localhost", "[::1]"):
        status, body = api.json("GET", "/api/health",
                                headers={"Host": f"{spelling}:{api.port}"})
        assert status == 200, spelling
        assert body["ok"] is True


def test_an_origin_from_another_site_is_refused(api):
    status, body = api.json("GET", "/api/incidents",
                            headers={"Origin": "https://evil.example"})
    assert status == 403 and body["error"] == "bad_origin"
    status, _ = api.json("GET", "/api/incidents",
                         headers={"Origin": f"http://{api.authority}"})
    assert status == 200


def test_a_post_that_is_not_json_is_refused(api):
    status, body = api.json(
        "POST", PREVIEW, body=record_body(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415 and body["error"] == "json_only"


def test_a_body_that_asserts_an_actor_is_refused(api):
    status, body = api.post(PREVIEW, record_body(actor="root@example.com"))
    assert status == 400 and body["error"] == "identity_is_server_side"


def test_a_body_that_is_not_a_json_object_is_refused(api):
    status, body = api.post(PREVIEW, ["verb", "record-action"])
    assert status == 400 and body["error"] == "not_object"


def test_the_router_separates_a_missing_route_from_a_wrong_method(api):
    """The write routes are in the list because a GET route onto one would be
    reachable from a bare link, which carries no `Origin` and no JSON content
    type, and the thing on the other side writes the wiki."""
    assert api.get("/api/nope")[0] == 404
    assert api.json("POST", "/api/incidents", body={})[0] == 405
    assert api.json("GET", PREVIEW)[0] == 405
    assert api.json("GET", COMMIT)[0] == 405


def test_every_response_carries_the_no_store_baseline(api):
    status, _, headers = api.send("GET", "/api/health")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "connect-src 'self'" in headers["Content-Security-Policy"]
    assert headers["Content-Type"].startswith("application/json")


def test_the_page_is_served_as_html_under_the_same_baseline(api):
    status, raw, headers = api.send("GET", "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert headers["Cache-Control"] == "no-store"
    assert raw.lstrip().startswith(b"<!")


def _policy(header: str) -> dict[str, frozenset[str]]:
    """`"default-src 'none'; connect-src 'self'"` ->
    `{"default-src": {"'none'"}, "connect-src": {"'self'"}}`."""
    fields = [directive.split() for directive in header.split(";")]
    return {field[0]: frozenset(field[1:]) for field in fields if field}


def _inline(html: str, tag: str) -> list[str]:
    """The bodies of every `<tag>` the page carries with no `src=`, which is
    what a browser refuses to run under a policy that does not permit
    inline."""
    return re.findall(rf"<{tag}(?![^>]*\bsrc=)[^>]*>(.*?)</{tag}>", html, re.S)


#: A `data:` URI where a browser would fetch it: `url(data:` in the
#: stylesheet, or a `src=`/`href=` attribute. A bare `data:` anywhere else is
#: script punctuation (`{data: rows}`) and nothing the page loads.
_DATA_URI_RE = re.compile(
    r"""url\(\s*['"]?data:|(?:src|href)\s*=\s*['"]?data:""")

#: The directives that do not fall back to `default-src`. Dropping one of
#: these grants everything it covers, so their absence is a weakening the
#: derivation below cannot see: nothing in the page asks for them, and that
#: is exactly why they must be there saying `'none'`.
_NO_FALLBACK = ("base-uri", "form-action")


def _permitting(blocks: list[str]) -> set[frozenset[str]]:
    """The source sets that permit exactly `blocks` and nothing else: the
    blanket `'unsafe-inline'`, or a `sha256-` hash of every block. Both
    spellings stay because either is a correct policy for this page: the
    hardening below pins `script-src` to the digest, and `style-src` is still
    the blanket one."""
    digests = frozenset(
        "'sha256-{}'".format(
            base64.b64encode(hashlib.sha256(block.encode()).digest()).decode())
        for block in blocks)
    return {frozenset({"'unsafe-inline'"}), digests}


def test_the_policy_grants_exactly_what_the_page_needs(api):
    """The CSP against the page it protects. `tests/test_portal_ui.py` walks
    the page in a browser off a `file://` URL, where no header reaches it, so
    until this row the policy and the page were two documents nobody compared.

    What the page needs is read off the bytes the server just sent, never
    restated here. Drop the inline `<script>` and the row fails on a grant
    nothing asks for any more; weaken `script-src` and it fails because what
    the policy grants is no longer a set that would run the block the page
    still carries."""
    _, raw, headers = api.send("GET", "/")
    html = raw.decode()
    policy = _policy(headers["Content-Security-Policy"])

    needed: dict[str, set[frozenset[str]]] = {
        "default-src": {frozenset({"'none'"})}}
    for tag, directive in (("script", "script-src"), ("style", "style-src")):
        blocks = _inline(html, tag)
        if blocks:
            needed[directive] = _permitting(blocks)
    if ui.endpoints_called(html):
        needed["connect-src"] = {frozenset({"'self'"})}
    if _DATA_URI_RE.search(html):
        needed["img-src"] = {frozenset({"data:"})}

    for directive, permitting in needed.items():
        assert directive in policy, \
            f"the page needs {directive} and the policy has no such directive"
        assert policy[directive] in permitting, (
            f"{directive} grants {sorted(policy[directive])}; the page needs "
            f"one of {sorted(sorted(s) for s in permitting)}")
    for directive, granted in policy.items():
        if directive not in needed:
            assert granted == {"'none'"}, (
                f"{directive} grants {sorted(granted)} and nothing in the "
                f"page asks for it")
    for directive in _NO_FALLBACK:
        assert directive in policy, (
            f"{directive} does not fall back to default-src, so a policy "
            f"that omits it grants every {directive} the page could carry")


def test_script_src_is_the_hash_of_the_served_block_and_never_unsafe_inline(api):
    """The claim the browser walk cannot make: it runs off `file://`, where no
    header reaches the page, so nothing there is under a CSP at all.

    Beside the derived row above, which proves the policy is no wider than the
    page needs and would accept either spelling: this one proves `script-src`
    is the narrow spelling, the digest of the block in the same response, and
    that it stays narrow. An origin that can commit to the wiki does not get
    to run script an attacker injected into the page."""
    _, raw, headers = api.send("GET", "/")
    blocks = _inline(raw.decode(), "script")
    assert len(blocks) == 1, \
        f"the page carries one inline script block; got {len(blocks)}"
    digest = base64.b64encode(
        hashlib.sha256(blocks[0].encode()).digest()).decode()

    granted = _policy(headers["Content-Security-Policy"])["script-src"]
    assert granted == {f"'sha256-{digest}'"}, (
        f"script-src grants {sorted(granted)}; the served page's one block "
        f"hashes to sha256-{digest}")
    assert "'unsafe-inline'" not in granted


def test_the_host_aliases_are_the_loopback_spellings_of_the_bound_port():
    assert portal.allowed_hosts("127.0.0.1:8765") == frozenset(
        {"127.0.0.1:8765", "localhost:8765", "[::1]:8765"})
    assert portal.loopback("127.0.0.1") and portal.loopback("127.1.2.3")
    assert portal.loopback("localhost") and portal.loopback("::1")
    assert portal.loopback("[::1]")
    assert not portal.loopback("10.0.0.1")
    assert not portal.loopback("wiki.example.com")


def test_a_verb_the_principal_may_not_run_is_listed_with_the_role_it_needs(
        root):
    cfg = load_config(root)
    actor = transaction.resolve_actor(root / "wiki")
    provider = TrustedOperator(
        Principal(actor, frozenset({Role.VIEWER, Role.OPERATOR})))
    srv, thread = serve(cfg, provider=provider)
    try:
        client = Client(srv.server_address)
        _, body = client.get(f"/api/incidents/{OPEN_SLUG}")
        rows = {r["verb"]: r for r in body["allowed"]}
        assert rows["resolve"]["permitted"] is False
        assert rows["resolve"]["role"] == "closer"
        assert rows["resolve"]["fields"], "the form is still described"
        assert rows["record-action"]["permitted"] is True

        status, refused = client.post(
            f"/api/incidents/{OPEN_SLUG}/preview",
            {"verb": "resolve", "fields": {"summary": "x"}})
        assert status == 403 and refused["error"] == "forbidden"
        assert refused["required"] == "closer"
        assert refused["held"] == ["operator", "viewer"]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


class _NoOne:
    def identify(self, ctx):
        from dbwiki.portal.identity import Unidentified
        raise Unidentified("nobody is at this browser")


def test_health_answers_without_an_identity_and_the_rest_does_not(root):
    cfg = load_config(root)
    srv, thread = serve(cfg, provider=_NoOne())
    try:
        client = Client(srv.server_address)
        status, body = client.get("/api/health")
        assert status == 200 and body["ok"] is True
        assert body["revision"] == head(root)
        assert body["push"] is False
        assert body["bind"] == client.authority
        assert client.get("/api/incidents")[0] == 401
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


def test_health_names_the_actor_the_commits_will_carry(api, root):
    _, body = api.get("/api/health")
    assert body["actor"] == EMAIL


def test_make_server_still_refuses_non_loopback_by_default(root):
    """Every caller that does not pass `allow_remote` — every test above
    this one included — keeps the loopback-only guarantee."""
    cfg = load_config(root)
    with pytest.raises(ValueError, match="loopback"):
        portal.make_server(cfg, bind=("0.0.0.0", 0))


def test_make_server_binds_non_loopback_when_allowed(root):
    cfg = load_config(root)
    srv = portal.make_server(cfg, bind=("0.0.0.0", 0), allow_remote=True)
    try:
        assert not portal.loopback(srv.server_address[0])
    finally:
        srv.server_close()


def test_serve_warns_but_does_not_refuse_a_bind_the_whole_network_could_reach(
        root, capsys, monkeypatch):
    """--bind is the operator's own choice; serve() honours it and warns on
    stderr instead of refusing (exit 2), since every write on it still
    carries the one TrustedOperator identity. `serve_forever` is stubbed out
    so the call returns instead of blocking, the way SIGTERM normally ends
    it."""
    monkeypatch.setattr(portal.WorkbenchServer, "serve_forever",
                        lambda self: None)
    cfg = load_config(root)
    assert portal.serve(cfg, bind="0.0.0.0:0") == 0
    err = capsys.readouterr().err
    assert "0.0.0.0" in err and EMAIL in err


def test_serve_refuses_to_start_with_no_attributable_actor(root, capsys):
    """Better than serving a read-only page that fails on the first commit."""
    git(root / "wiki", "config", "--unset", "user.email")
    cfg = load_config(root)
    assert portal.serve(cfg, bind="127.0.0.1:0") == 2
    assert "no actor" in capsys.readouterr().err


def test_the_portal_is_never_in_the_commands_that_hold_the_wiki_lock():
    from dbwiki import cli
    assert "portal" not in cli.LOCKED


def test_serve_refuses_a_wiki_with_no_commit_to_build_against(root, capsys):
    """A fresh wiki resolves an actor and then has no HEAD for the banner,
    the queue or a base. The unit's `RestartPreventExitStatus=2` covers this
    alongside the no-actor case; an unhandled traceback would exit 1 and
    restart-loop every five seconds instead."""
    fresh = root / "fresh"
    (fresh / "config").mkdir(parents=True)
    (fresh / "config" / "dbwiki.yaml").write_text(CONFIG)
    (fresh / "wiki").mkdir()
    git(fresh / "wiki", "init", "-b", "main")
    git(fresh / "wiki", "config", "user.email", EMAIL)
    git(fresh / "wiki", "config", "user.name", "DBA")

    assert portal.serve(load_config(fresh), bind="127.0.0.1:0") == 2
    assert "no HEAD" in capsys.readouterr().err


FLEET_NOW = "2026-08-31T09:00:00Z"

HEAT_HOST = "lab-dg1.localdomain"
LOUD_DAY = "2026-08-29"
QUIET_DAY = "2026-08-30"
SILENT_DAY = "2026-08-28"


def _digest_sidecar(day, sources):
    """A digest JSON in the shape the compactor writes, cut down to the keys
    the heat model reads. `sources.<source>.by_class` is the only part `heat`
    parses, and the rest is here so the fixture stays recognisable as the file
    the live wiki holds rather than as the subset one reader wants."""
    return json.dumps({
        "db": DB,
        "window": {"from": f"{day}T00:00:00Z", "to": f"{day}T22:15:00Z",
                   "day": day},
        "generated_by": "dbwiki-compactor/0.1.0",
        "pattern_versions": {"alert": 1, "listener": 1},
        "sources": {name: {"total_events": sum(by_class.values()),
                           "by_class": by_class, "notable": []}
                    for name, by_class in sources.items()}})


def _fleet_wiki(root):
    """The fixture wiki plus the pages the fleet view reads: a database page
    (which is what puts a database in the view at all), a journal month, and
    two occurrence rows straddling the 30-day window.

    The database page names a host and two days of the window carry a digest
    sidecar, which is what the heat maps read: a loud day whose sources have
    to be summed, a day whose digest never mentions `error` at all, and the
    days between them that hold no digest."""
    wiki = root / "wiki"
    (wiki / f"incidents/{OLD_FIX_SLUG}.md").write_text(incident_page(
        DB, f"{CODE} on the busy listener", status="resolved",
        error_codes=(CODE,)))
    (wiki / "databases" / DB / "journal").mkdir(parents=True)
    (wiki / "databases" / f"{DB}.md").write_text(
        f"---\ntype: database\ndb: {DB}\n---\n\n# {DB}\n\n"
        f"Host `{HEAT_HOST}` (192.0.2.121).\n")
    (wiki / "digests" / DB / f"{LOUD_DAY}.json").write_text(_digest_sidecar(
        LOUD_DAY, {"alert": {"routine": 73, "unmatched": 84, "error": 3,
                             "warning": 2},
                   "listener": {"routine": 12, "error": 1}}))
    (wiki / "digests" / DB / f"{QUIET_DAY}.json").write_text(_digest_sidecar(
        QUIET_DAY, {"alert": {"routine": 40}}))
    (wiki / "databases" / DB / "journal" / "2026-08.md").write_text(
        f"---\ntype: journal\ndb: {DB}\n---\n\n# {DB} journal 2026-08\n\n"
        "## 2026-08-14 — listener rebuilt\n\nEarlier work.\n\n"
        "## 2026-08-29 — standby lag cleared\n\nThe lag is gone.\n")
    (wiki / f"errors/{CODE}.md").write_text(
        f"---\ntype: error-class\nresearched: 2026-08-17\n---\n\n# {CODE}\n\n"
        "## Occurrences\n\n"
        "| day | db | note | evidence |\n|---|---|---|---|\n"
        f"| 2026-07-01 | {DB} | before the window | {DIGEST} |\n"
        f"| 2026-08-28 | {DB} | inside the window | {DIGEST} |\n\n"
        "## Reference\n\n**Cause:** the listener refused the connection\n"
        "(source: sources/oracle-docs).\n\n"
        "**Action:** check the listener is up\n"
        "(source: sources/oracle-docs).\n\n"
        "## Resolution history\n\n"
        "| resolved | db | incident | remediation | evidence |\n"
        "|---|---|---|---|---|\n"
        f"| 2026-07-02 | {DB} | [[incidents/{DONE_SLUG}]] | "
        "restarted the listener | 2026-07-02T09:14:00Z |\n"
        f"| 2026-08-02 | {DB} | [[incidents/{OLD_FIX_SLUG}]] | "
        f"raised the listener queue size | {DIGEST} |\n")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "ingest: fleet fixtures")


def test_the_fleet_view_carries_the_provenance_triple_and_one_row_per_db(root):
    """The triple is flat at the top level, and `revision == head` on a wiki
    nothing is moving under: the page derives its stale badge by comparing
    those two, so a quiet wiki must never show one."""
    _fleet_wiki(root)
    srv, thread = serve(load_config(root), now=lambda: FLEET_NOW)
    try:
        status, body = Client(srv.server_address).get("/api/fleet")
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)

    assert status == 200
    assert body["revision"] == head(root)
    assert body["head"] == head(root)
    assert incidents.ISO_Z_RE.match(body["built_at"])
    assert set(body) == {"revision", "built_at", "head", "dbs"}

    assert len(body["dbs"]) == 1
    row = body["dbs"][0]
    assert set(row) == {"db", "open", "monitoring", "journal_day",
                        "journal_headline", "errors_30d", "page"}
    assert row["db"] == DB
    assert row["open"] == 1 and row["monitoring"] == 1
    assert row["journal_day"] == "2026-08-29"
    assert row["journal_headline"] == "standby lag cleared"
    assert row["errors_30d"] == 1
    assert row["page"] == f"databases/{DB}.md"


@pytest.fixture
def reading(root):
    """The fleet fixture's wiki behind a server with a pinned clock: what the
    snapshot-backed read views answer from."""
    _fleet_wiki(root)
    srv, thread = serve(load_config(root), now=lambda: FLEET_NOW)
    try:
        yield Client(srv.server_address)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


def test_the_heat_maps_draw_the_default_month_when_the_page_names_no_span(
        reading, root):
    """The provenance triple is flat at the top level the way every other
    revision-backed envelope carries it, so the board derives one stale badge
    for the maps and the fleet table alike."""
    status, body = reading.get("/api/heat")
    assert status == 200
    assert set(body) == {"revision", "built_at", "head", "days", "classes",
                         "rows"}
    assert body["revision"] == head(root)
    assert body["head"] == head(root)
    assert incidents.ISO_Z_RE.match(body["built_at"])

    assert len(body["days"]) == 30
    assert body["days"][-1] == FLEET_NOW[:10]
    assert body["classes"] == ["error", "warning", "unmatched"]
    for row in body["rows"]:
        for name in body["classes"]:
            assert len(row["counts"][name]) == len(body["days"])


def test_the_heat_maps_draw_the_whole_window_when_the_toggle_asks_for_it(
        reading):
    """90 is the window `heat` builds, so the longest span the toggle offers
    is a slice of exactly everything and not a second build."""
    status, body = reading.get("/api/heat?days=90")
    assert status == 200
    assert len(body["days"]) == 90
    assert body["days"][-1] == FLEET_NOW[:10]
    assert len(body["rows"][0]["counts"]["error"]) == 90


@pytest.mark.parametrize("days", ["0", "91", "x", "-1", "30.0"])
def test_a_span_outside_the_window_is_refused_rather_than_clamped(reading,
                                                                  days):
    """A page asking for 400 days has a bug, and silently drawing 90 would
    hide it."""
    status, body = reading.get(f"/api/heat?days={days}")
    assert status == 400
    assert body["error"] == "bad_days"


def test_an_empty_span_is_the_default_and_not_a_refusal(reading):
    """`?days=` with nothing after it is what a page sends when its toggle
    has never been touched, and it means the default rather than a bad
    request."""
    status, body = reading.get("/api/heat?days=")
    assert status == 200
    assert len(body["days"]) == 30


def test_a_heat_row_sums_its_sources_on_the_day_its_digest_covers(reading):
    """The alert log counted three errors that day and the listener one, and
    the operator scanning the row is asking how loud the day was rather than
    which log was loud."""
    _, body = reading.get("/api/heat")
    assert [row["db"] for row in body["rows"]] == [DB]
    row = body["rows"][0]
    assert set(row) == {"db", "host", "counts"}
    assert row["host"] == HEAT_HOST, "the band the maps group this row under"

    loud = body["days"].index(LOUD_DAY)
    assert row["counts"]["error"][loud] == 4
    assert row["counts"]["warning"][loud] == 2
    assert row["counts"]["unmatched"][loud] == 84


def test_a_day_with_no_digest_reads_null_beside_a_silent_digest_reading_zero(
        reading):
    """The two facts the maps draw differently. The quiet day's sidecar counts
    only routine lines, so its error cell is a nothing that was observed; the
    silent day has no sidecar at all, so nobody can say."""
    _, body = reading.get("/api/heat")
    counts = body["rows"][0]["counts"]["error"]
    assert counts[body["days"].index(QUIET_DAY)] == 0
    assert counts[body["days"].index(SILENT_DAY)] is None


def test_a_database_the_snapshot_does_not_hold_names_the_revision_it_read(
        reading, root):
    """The message carries the snapshot's revision rather than HEAD: that is
    the revision the answer was looked up in, and the two differ exactly when
    this caller lost a rebuild race."""
    status, body = reading.get("/api/db?name=nosuchdb")
    assert status == 404
    assert body["error"] == "unknown_db"
    assert head(root)[:12] in body["message"]


def test_a_database_incident_row_is_the_committed_view_with_its_ownership(
        reading):
    status, body = reading.get(f"/api/db?name={DB}")
    assert status == 200
    assert [row["slug"] for row in body["incidents"]] == [
        DONE_SLUG, OLD_FIX_SLUG, OPEN_SLUG, MON_SLUG]
    row = body["incidents"][0]
    assert set(row) == {"slug", "path", "db", "title", "status", "label",
                        "unknown_status", "opened", "updated", "origin",
                        "commit"}
    for absent in ("verdict", "dirty", "allowed"):
        assert absent not in row, \
            f"{absent} is a working-tree fact and this row reads a revision"
    assert row["origin"] in {str(origin) for origin in readmodel.Origin}
    assert set(row["commit"]) == {"sha", "short", "at", "author", "subject",
                                  "actor"}


def test_a_database_error_row_counts_every_occurrence_and_not_a_window(
        reading):
    """`fleet` counts the last 30 days; this view counts the page, so the row
    holds the July occurrence as well as the August one."""
    _, body = reading.get(f"/api/db?name={DB}")
    assert body["errors"] == [
        {"code": CODE, "count": 2, "last_day": "2026-08-28",
         "researched": "2026-08-17", "resolved": 2,
         "page": f"errors/{CODE}.md"}]


def test_a_database_error_row_counts_the_fixes_on_the_error_page(reading):
    """Two rows in the page's resolution history, so the table can say the
    code has been closed before without carrying the rows themselves."""
    _, body = reading.get(f"/api/db?name={DB}")
    assert body["errors"][0]["resolved"] == 2


def test_the_resolution_history_reaches_the_case_file_newest_day_first(
        reading):
    """The page appends, so the newest fix is the last row written and the
    first row the operator should read."""
    _, body = reading.get(f"/api/incidents/{OPEN_SLUG}")
    rows = {row["code"]: row for row in body["research"]}
    assert rows[CODE]["resolutions"] == [
        {"day": "2026-08-02", "db": DB, "incident": OLD_FIX_SLUG,
         "path": f"incidents/{OLD_FIX_SLUG}.md",
         "remediation": "raised the listener queue size",
         "evidence": DIGEST},
        {"day": "2026-07-02", "db": DB, "incident": DONE_SLUG,
         "path": f"incidents/{DONE_SLUG}.md",
         "remediation": "restarted the listener",
         "evidence": "2026-07-02T09:14:00Z"}]


def test_a_database_journal_is_newest_day_first(reading):
    _, body = reading.get(f"/api/db?name={DB}")
    assert [entry["day"] for entry in body["journal"]] == ["2026-08-29",
                                                           "2026-08-14"]
    assert body["journal"][0] == {
        "day": "2026-08-29", "headline": "standby lag cleared",
        "path": f"databases/{DB}/journal/2026-08.md"}


def test_the_database_view_carries_the_provenance_triple_flat(reading, root):
    _, body = reading.get(f"/api/db?name={DB}")
    assert body["revision"] == head(root)
    assert body["head"] == head(root)
    assert incidents.ISO_Z_RE.match(body["built_at"])
    assert set(body) == {"revision", "built_at", "head", "db", "page",
                         "incidents", "errors", "journal"}
    assert body["page"]["path"] == f"databases/{DB}.md"


@pytest.mark.parametrize("q", ["", "%20%20"])
def test_a_search_for_nothing_is_refused_rather_than_answered_empty(reading, q):
    """Every page holds the empty string, so a 200 with no hits would be a
    false statement about the wiki."""
    status, body = reading.get(f"/api/search?q={q}")
    assert status == 400
    assert body["error"] == "empty_query"


def test_a_search_ranks_a_field_hit_ahead_of_a_body_hit(reading):
    """`cdb1` is the database page's path and every incident's `db`, and it
    appears in the index only inside link text, which is body."""
    status, body = reading.get(f"/api/search?q={DB}")
    assert status == 200
    assert body["query"] == DB
    paths = [hit["path"] for hit in body["hits"]]
    assert paths[0] == f"databases/{DB}.md"
    assert paths.index("index.md") > paths.index(OPEN_PATH)


def test_a_search_hit_carries_the_five_keys_the_page_draws(reading):
    _, body = reading.get(f"/api/search?q={CODE}")
    assert body["hits"], "the fixture wiki holds pages naming the error code"
    for hit in body["hits"]:
        assert set(hit) == {"path", "type", "title", "db", "snippet"}


def page_at(client, path):
    return client.get("/api/page?path="
                      + urllib.parse.quote(path, safe=""))


def test_a_report_arrives_rendered_with_its_frontmatter_swallowed(reading,
                                                                  root):
    """The renderer's two verified bugs, proved at the endpoint rather than in
    a unit: frontmatter is consumed before any block sees a line, so its keys
    are not the page's lede and its closing `---` is not a rule."""
    status, body = page_at(reading, REPORT_PATH)
    assert status == 200
    assert body["revision"] == head(root) and body["head"] == head(root)
    assert incidents.ISO_Z_RE.match(body["built_at"])
    assert body["type"] == "report"
    assert body["title"] == f"Fleet report {DB} 2026-08-30 06:15"

    html = body["html"]
    assert "<table>" in html and "<th>" in html
    assert "window_start" not in html, \
        "frontmatter is consumed, so no key of it reads as prose"
    assert not html.startswith("<hr>"), \
        "the frontmatter's closing --- is not a horizontal rule"


def test_a_rendered_wikilink_is_a_route_and_an_unresolved_one_is_struck(
        reading):
    """`lint.resolve_link` decides both arms, so a link lint calls broken can
    never render as one an operator can follow."""
    _, body = page_at(reading, REPORT_PATH)
    assert f'href="#/page/incidents/{OPEN_SLUG}.md"' in body["html"]
    assert 'class="nolink">nothing/here<' in body["html"]


def test_a_rendered_page_names_its_headings_and_who_wrote_it(reading):
    _, body = page_at(reading, REPORT_PATH)
    assert {"level": 2, "text": "Summary", "slug": "wikipage--summary"} \
        in body["headings"], \
        "a heading arrives with the prefixed slug its id already carries"
    assert body["origin"] == "hand", \
        "the fixture's one commit carries no Actor trailer and no subject " \
        "prefix, and reports/ is not a machine directory"
    assert set(body["commit"]) == {"sha", "short", "at", "author", "subject",
                                   "actor"}


def test_the_pages_that_link_a_page_are_its_backlinks(reading):
    _, body = page_at(reading, OPEN_PATH)
    assert "index.md" in body["backlinks"], \
        "the index lists the open incident, so it links to it"
    assert REPORT_PATH in body["backlinks"]


OUTSIDE = ["../../etc/passwd", "/etc/passwd",
           "incidents/../../../etc/passwd", "", "wiki/index.md", "index"]


@pytest.mark.parametrize("path", OUTSIDE)
def test_a_path_outside_the_inventory_is_not_a_page(reading, path):
    status, body = page_at(reading, path)
    assert status == 404 and body["error"] == "no_such_page"


def test_a_refused_path_never_reaches_the_wiki_at_all(reading, monkeypatch):
    """The traversal boundary is the inventory, not a sanitised string: the
    endpoint decides before it reads, so with every read wired to raise, each
    refusal still answers 404 rather than an error the read produced."""
    assert page_at(reading, REPORT_PATH)[0] == 200, \
        "the snapshot is built, so nothing below is a build reaching git"

    def refuse(*args, **kwargs):
        raise AssertionError("a refused path must not reach the wiki")

    monkeypatch.setattr(transaction.Tree, "batch_at", refuse)
    for path in OUTSIDE:
        status, body = page_at(reading, path)
        assert status == 404 and body["error"] == "no_such_page", \
            f"{path!r} is refused by the inventory, not by a read"


def test_a_digest_is_read_lazily_at_the_revision_the_snapshot_pinned(reading,
                                                                     root):
    """`readmodel.build` leaves the digests closed, which is what keeps 787
    files of 982 unread. The endpoint reads this one on demand, so the proof
    is that the body arrived and the snapshot still does not hold it."""
    status, body = page_at(reading, DIGEST)
    assert status == 200
    assert body["html"], "the digest rendered"
    assert body["origin"] == "machine" and body["commit"] is None
    assert body["title"] == f"{DB} 2026-08-30"

    snap = readmodel.build(root / "wiki", head(root), now=lambda: FLEET_NOW)
    assert DIGEST not in snap.text, \
        "the lazy path is what answered, not a cached body"


def test_the_database_view_carries_the_rendered_standing_page(reading):
    """`db_page_json` is gone: one `page_body_json` shape is drawn by one
    painter on the database screen and the page screen alike."""
    _, body = reading.get(f"/api/db?name={DB}")
    page = body["page"]
    assert set(page) == {"path", "type", "title", "html", "headings",
                         "origin", "commit", "backlinks"}
    assert page["html"].startswith("<h1"), \
        "the standing page arrives rendered, not as structure to re-draw"
    assert not hasattr(wire, "db_page_json"), \
        "the legacy headings-only encoder went with its last caller"


RUNS_NOW = "2026-08-31T09:00:00Z"
RUN_DAY = "2026-08-30"

OLD_RUN = "1111aaaa2222"
RUN = "9f2c1a0b7de4"
LINT_RUN = "3333bbbb4444"
PENDING_RUN = "5555cccc6666"

OLD_AT = "2026-08-29T05:00:00Z"
RUN_AT = "2026-08-30T05:00:00Z"
LINT_AT = "2026-08-31T05:00:00Z"
PENDING_AT = "2026-08-31T08:30:00Z"

DB2 = "cdb2"
DB3 = "cdb3"


def _db_entry(db, **over):
    """One `dbs[]` entry as `cmd_run` writes it: `rec.add_db`'s facts plus
    `cli._digest_facts`. Every key is a writer's; a fixture that invents one
    proves the loader handles fiction."""
    entry = {
        "db": db,
        "watermark_before": "2026-08-29T05:00:00Z",
        "watermark_after": RUN_AT,
        "decision": "wake",
        "decision_reasons": ["first_ever_code"],
        "model_tier": "strong",
        "window": {"from": "2026-08-29T05:00:00Z", "to": RUN_AT,
                   "day": RUN_DAY},
        "events": 1204,
        "notable_events": 12,
        "notable_groups": 2,
        "notable": True,
        "deltas": ["first_ever_code"],
        "digest": f"wiki/digests/{db}/{RUN_DAY}.json",
        "content_hash": "3f9a1c2b",
        "validation": "ok",
        "outcome": "ingested",
        "commit": "ab12cd3",
    }
    entry.update(over)
    return entry


def _run_line(run_id, *, command="run", started=RUN_AT, outcome="ok",
              error_category=None, dbs=(), facts=None):
    """One `run_health.jsonl` line as `RunRecord.to_event` writes it."""
    return {"schema_version": 1,
            "run_id": run_id,
            "command": command,
            "started": started,
            "finished": started,
            "duration_s": 61.4,
            "outcome": outcome,
            "error_category": error_category,
            "dbs": list(dbs),
            "facts": {"consolidation": False, "adapter": "pi",
                      "dbs": len(dbs), **(facts or {})}}


def _agent_line(event_id, run_id, *, db=DB, at=RUN_AT, **over):
    """One `agent_runs.jsonl` line as `record_agent_run` writes it."""
    line = {"event_id": event_id,
            "run_id": run_id,
            "at": at,
            "task": "ingest",
            "db": db,
            "adapter": "pi",
            "model": "qwen3-30b",
            "model_tier": "strong",
            "mode": "structured",
            "duration_s": 41.2,
            "timed_out": False,
            "attempts": 1,
            "validation_ok": True,
            "rolled_back": False,
            "pages_touched": 3,
            "lint_findings": 0,
            "digest_bytes": 18422,
            "input_tokens": 12000,
            "output_tokens": 900,
            "cost_usd": 0.42,
            "usage_known": True}
    line.update(over)
    return line


def _start_line(run_id, *, command="run", started=PENDING_AT):
    """One `elk/run_starts.jsonl` line as `_record_run_start` writes it."""
    return {"event_id": f"{run_id}-start", "run_id": run_id,
            "command": command, "started": started, "pid": 4242,
            "run_host": "dbhost", "schema_version": 1}


def _ledger_entry(**over):
    entry = {"last_decision": {"outcome": "wake", "at": RUN_AT,
                               "reasons": [{"code": "first_ever_code",
                                            "evidence": {"code": CODE,
                                                         "count": 3}}]}}
    entry.update(over)
    return entry


#: The fingerprint `alerts.evaluate` would have minted for the failure the
#: fixture's newest tick recorded on DB3, read through the function that mints
#: it rather than typed out.
DB3_FINGERPRINT = alerts.fingerprint("harness_error", DB3, "-")


def _alert_entry(*, db, category="harness_error", **over):
    """One `.state/alerts.json` fingerprint entry as `alerts.evaluate` writes
    it. `count` is assessments that saw the finding, which is why it is larger
    than the failures the run logs hold."""
    entry = {"first_seen": OLD_AT, "last_seen": RUN_AT, "count": 43,
             "alerted_at": OLD_AT, "category": category, "db": db,
             "source": "-"}
    entry.update(over)
    return entry


def _state(root):
    """A `.state/` the pipeline could have written: two `run` ticks over the
    same digests, a weekly lint, one agent line that measured its usage and
    one that did not, a start no finish ever answered, a ledger holding one
    ingested entry, one deliberate skip and one real debt, and an alert still
    open about the database the newest tick failed on."""
    state = root / ".state"
    (state / "elk").mkdir(parents=True, exist_ok=True)
    lines = (
        _run_line(OLD_RUN, started=OLD_AT, dbs=[_db_entry(DB)]),
        _run_line(RUN, started=RUN_AT, outcome="failed",
                  error_category="harness_error",
                  facts={"report": REPORT_PATH, "html": "9f2c1a0"},
                  dbs=[_db_entry(DB),
                       _db_entry(DB2, decision="skip",
                                 decision_reasons=["unchanged_window"],
                                 outcome="skipped", commit=""),
                       _db_entry(DB3, outcome="failed", validation="failed",
                                 commit="", error_category="harness_error",
                                 error="ORA-12514 the listener refused it")]),
        _run_line(LINT_RUN, command="lint", started=LINT_AT,
                  facts={"findings": 0}),
    )
    (state / HEALTH_LOG).write_text(
        "".join(json.dumps(line) + "\n" for line in lines))
    legacy = _agent_line("ev-ingest-cdb3", RUN, db=DB3)
    for key in ("input_tokens", "output_tokens", "cost_usd", "usage_known"):
        del legacy[key]
    (state / AGENT_LOG).write_text("".join(
        json.dumps(line) + "\n"
        for line in (_agent_line("ev-ingest-cdb1", RUN), legacy)))
    (state / RUN_STARTS_LOG).write_text(
        json.dumps(_start_line(PENDING_RUN)) + "\n")
    (state / "ingest_ledger.json").write_text(json.dumps({
        "schema_version": 1,
        "entries": {
            f"digests/{DB}/{RUN_DAY}.json": _ledger_entry(status="ingested",
                                                          at=RUN_AT),
            f"digests/{DB2}/{RUN_DAY}.json": _ledger_entry(
                last_decision={"outcome": "skip", "at": RUN_AT,
                               "reasons": [{"code": "unchanged_window",
                                            "evidence": {"delta": 0}}]}),
            f"digests/{DB3}/{RUN_DAY}.json": _ledger_entry(),
        }}))
    (state / alerts.ALERT_STATE).write_text(json.dumps(
        {"schema_version": alerts.ALERT_SCHEMA_VERSION,
         "fingerprints": {DB3_FINGERPRINT: _alert_entry(db=DB3)},
         "recovered": []}))


@pytest.fixture
def automation(root):
    """The fixture project over that `.state/`, behind a server whose clock is
    pinned: freshness is an age against now, so an unpinned clock would make
    every staleness assertion a fact about the day the suite ran."""
    _state(root)
    srv, thread = serve(load_config(root), now=lambda: RUNS_NOW)
    try:
        yield Client(srv.server_address)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


def _row(body, run_id):
    return next(row for row in body["runs"] if row["run_id"] == run_id)


def test_the_run_list_is_newest_first_and_carries_no_revision(automation):
    """`/api/runs` reads `.state/` and not the wiki, so there is no revision
    to name; `coverage` is what bounds the answer instead."""
    status, body = automation.get("/api/runs")
    assert status == 200
    assert set(body) == {"generated_at", "coverage", "freshness", "runs",
                         "backlog", "pending", "trend", "failures"}
    assert "revision" not in body
    assert body["generated_at"] == RUNS_NOW
    assert [row["run_id"] for row in body["runs"]] == [LINT_RUN, RUN, OLD_RUN]

    row = _row(body, RUN)
    assert row["command"] == "run" and row["outcome"] == "failed"
    assert row["error_category"] == "harness_error"
    assert row["dbs_ingested"] == 1 and row["dbs_skipped"] == 1
    assert [stage["name"] for stage in row["stages"]] \
        == list(events.STAGE_NAMES)
    assert {stage["status"] for stage in row["stages"]} <= set(events.STATUSES)


def test_a_log_that_was_never_written_is_a_coverage_row_and_not_a_gap(
        automation, root):
    """"never written" and "empty" are one answer to a view, and a missing row
    would read as a bug. The logs are read per request, so deleting one
    between requests is exactly the fresh-install shape."""
    (root / ".state" / AGENT_LOG).unlink()
    _, body = automation.get("/api/runs")
    rows = {row["name"]: row for row in body["coverage"]}
    assert set(rows) == {"run_health", "agent_runs", "run_starts"}
    assert rows["run_health"]["lines"] == 3
    assert rows["run_health"]["cap"] == MAX_EVENTS
    assert rows["run_health"]["oldest"] == OLD_AT
    assert rows["run_health"]["newest"] == LINT_AT
    assert rows["run_health"]["truncated"] is False
    assert rows["run_starts"]["cap"] == MAX_RUN_START_EVENTS
    assert rows["agent_runs"] == {"name": "agent_runs", "lines": 0,
                                  "cap": MAX_AGENT_EVENTS, "oldest": "",
                                  "newest": "", "truncated": False}


def test_the_backlog_is_debt_and_never_a_deliberate_skip(automation):
    """`health.deliberate_skip` owns that rule, so the workbench and
    `dbwiki health` cannot disagree about one ledger entry."""
    _, body = automation.get("/api/runs")
    assert body["backlog"] == [{"digest": f"digests/{DB3}/{RUN_DAY}.json",
                                "db": DB3, "decision": "wake"}]


def test_freshness_is_the_pinned_now_against_the_configured_budget(automation):
    """One stale task, one inside its weekly budget, and one nothing proves
    ran at all — which is never stale, because "never ran" is not a
    regression (`health._stage_staleness`)."""
    _, body = automation.get("/api/runs")
    rows = {row["task"]: row for row in body["freshness"]}
    assert set(rows) == {"run", "report", "lint", "research"}

    assert rows["run"] == {"task": "run", "at": RUN_AT, "age_h": 28.0,
                           "threshold_h": 26.0, "stale": True}
    assert rows["report"]["at"] == RUN_AT and rows["report"]["stale"] is True
    assert rows["lint"] == {"task": "lint", "at": LINT_AT, "age_h": 4.0,
                            "threshold_h": 192.0, "stale": False}
    assert rows["research"] == {"task": "research", "at": "", "age_h": None,
                                "threshold_h": 192.0, "stale": False}


def test_a_start_no_finish_answered_is_pending_and_is_still_a_run(automation):
    """`run_starts.jsonl` exists because run_health records only finishes, so
    this difference is the only evidence a dead tick leaves. Opening one is a
    real answer: seven pending stages, not a 404."""
    _, body = automation.get("/api/runs")
    assert body["pending"] == [{"run_id": PENDING_RUN, "command": "run",
                                "started": PENDING_AT}]

    status, run = automation.get(f"/api/run?id={PENDING_RUN}")
    assert status == 200
    assert run["run"]["run_id"] == PENDING_RUN and run["dbs"] == []
    assert [stage["name"] for stage in run["stages"]] \
        == list(events.STAGE_NAMES)
    assert {stage["status"] for stage in run["stages"]} == {"pending"}


def test_a_run_is_seven_stages_and_one_row_per_database(automation):
    status, body = automation.get(f"/api/run?id={RUN}")
    assert status == 200
    assert set(body) == {"run", "stages", "dbs", "agents", "links", "totals",
                         "compare"}

    assert [stage["name"] for stage in body["stages"]] \
        == list(events.STAGE_NAMES)
    ingest = next(s for s in body["stages"] if s["name"] == "ingest")
    assert ingest["status"] == "warning" and ingest["agentic"] is True
    assert ingest["detail"]["inferred"] is True

    rows = {row["db"]: row for row in body["dbs"]}
    assert [row["db"] for row in body["dbs"]] == [DB, DB2, DB3]
    assert rows[DB]["decision"] == "wake" and rows[DB]["outcome"] == "ingested"
    assert rows[DB]["digest"] == f"wiki/digests/{DB}/{RUN_DAY}.json"
    assert rows[DB]["digest_page"] == f"digests/{DB}/{RUN_DAY}.md"
    assert rows[DB]["reasons"] == [{"code": "first_ever_code",
                                    "evidence": {"code": CODE, "count": 3}}]
    assert rows[DB]["usage"] == {"input_tokens": 12000, "output_tokens": 900,
                                 "cost_usd": 0.42, "known": True,
                                 "cost_known": True}
    assert rows[DB2]["decision"] == "skip" and rows[DB2]["usage"] is None
    assert rows[DB3]["error"].startswith("ORA-12514")
    assert rows[DB3]["usage"] is None, \
        "a stage whose adapter reported nothing costs None, never a zero"


def test_an_older_run_is_never_decorated_with_a_later_ticks_reasoning(
        automation):
    """The ledger holds one `last_decision` per digest and every tick
    overwrites it, so the stored evidence describes the newest run that
    touched the digest and no other. The older run still carries its own
    recorded codes."""
    _, newer = automation.get(f"/api/run?id={RUN}")
    _, older = automation.get(f"/api/run?id={OLD_RUN}")
    assert newer["dbs"][0]["reasons"][0]["evidence"] == {"code": CODE,
                                                         "count": 3}
    assert older["dbs"][0]["reasons"] == [{"code": "first_ever_code",
                                           "evidence": {}}]


def test_a_run_id_nothing_recorded_names_the_caps_rather_than_the_id(
        automation):
    status, body = automation.get("/api/run?id=deadbeefcafe")
    assert status == 404 and body["error"] == "unknown_run"
    assert "deadbeefcafe" in body["message"] and "cap" in body["message"]


def _append(root, log, line):
    """One more line in a log the fixture already wrote. The logs are read per
    request, so the next call sees it."""
    with (root / ".state" / log).open("a") as fh:
        fh.write(json.dumps(line) + "\n")


def test_the_trend_holds_the_days_the_loop_did_not_run(automation, root):
    """A gap is a fact about the loop, and a table that skipped it would draw
    the span as adjacent rows and read as a denser loop than there was."""
    _append(root, HEALTH_LOG,
            _run_line("7777dddd8888", started="2026-08-27T05:00:00Z"))
    _, body = automation.get("/api/runs")

    rows = {row["day"]: row for row in body["trend"]}
    assert [row["day"] for row in body["trend"]] == [
        "2026-08-27", "2026-08-28", "2026-08-29", "2026-08-30", "2026-08-31"]
    gap = rows["2026-08-28"]
    assert (gap["runs"], gap["failed"]) == (0, 0)
    assert all(value == 0 for value in gap["totals"].values())
    assert (rows[RUN_DAY]["runs"], rows[RUN_DAY]["failed"]) == (1, 1)
    assert rows["2026-08-31"]["runs"] == 1, "the lint is loop work too"


def test_a_day_never_counts_a_price_nobody_reported_as_a_measured_zero(
        automation, root):
    silent = _agent_line("ev-report-cdb1", RUN)
    del silent["cost_usd"]
    _append(root, AGENT_LOG, silent)
    _, body = automation.get("/api/runs")

    measured = next(row for row in body["trend"]
                    if row["day"] == RUN_DAY)["totals"]
    assert measured["stages"] == 3
    assert measured["cost_usd"] == 0.42 and measured["priced_stages"] == 1
    assert measured["unpriced_stages"] == 1
    assert measured["token_stages"] == 2, \
        "the line reported its tokens and only its price is missing"


def test_a_failure_group_is_decorated_only_where_an_alert_is_open(automation):
    """The group is minted in `alerts.fingerprint`'s own namespace, so the
    entry `alerts.json` holds joins to it exactly. A group with no entry says
    nothing: `evaluate` drops a fingerprint when the finding clears, so an
    absence is "not open now" and never "this recovered"."""
    _, body = automation.get("/api/runs")

    rows = {(row["category"], row["db"]): row for row in body["failures"]}
    assert set(rows) == {("harness_error", DB3), ("harness_error", "-")}
    on_db = rows[("harness_error", DB3)]
    assert on_db["fingerprint"] == DB3_FINGERPRINT
    assert (on_db["count"], on_db["days"]) == (1, 1)
    assert on_db["commands"] == ["run"]
    assert on_db["sample_error"].startswith("ORA-12514")
    assert on_db["alert"] == {"first_seen": OLD_AT, "last_seen": RUN_AT,
                              "count": 43}
    assert rows[("harness_error", "-")]["alert"] is None


def test_a_run_is_drawn_beside_the_last_run_of_its_own_command(automation):
    _, body = automation.get(f"/api/run?id={RUN}")

    assert body["totals"]["stages"] == 2
    before = body["compare"]
    assert before["run_id"] == OLD_RUN and before["command"] == "run"
    assert before["dbs_ingested"] == 1
    assert set(before["totals"]) == set(body["totals"])
    assert before["totals"]["stages"] == 0, "no agent line names that run"


def test_a_command_the_logs_hold_no_earlier_run_of_is_drawn_alone(automation):
    for run_id in (OLD_RUN, LINT_RUN):
        _, body = automation.get(f"/api/run?id={run_id}")
        assert body["compare"] is None
        assert body["totals"]["stages"] == 0


def test_a_fold_in_replaying_a_run_id_is_never_the_run_before(automation, root):
    """`queue.fold_results` re-records under the enqueuing tick's `run_id`
    days later, so a candidate sharing this run's id is this run recorded
    twice, and comparing against it would draw a fold-in as a regression."""
    _append(root, HEALTH_LOG, _run_line(LINT_RUN, command="lint",
                                        started=RUN_AT))
    _append(root, HEALTH_LOG, _run_line(RUN, started="2026-08-29T06:00:00Z"))

    _, lint = automation.get(f"/api/run?id={LINT_RUN}")
    assert lint["compare"] is None, "the only earlier lint is this one again"
    _, run = automation.get(f"/api/run?id={RUN}")
    assert run["compare"]["run_id"] == OLD_RUN


def test_neither_run_view_opens_a_network_connection(automation, monkeypatch):
    """The thread is the discriminator: this test's own client connects from
    the main thread, and the handler runs on one of `ThreadingHTTPServer`'s.
    Recording only the connections a handler thread makes is what separates
    "the endpoint called out" from "the test spoke HTTP to it" — and calling
    out is exactly what reusing `health.assess` here would do."""
    opened = []
    original = {"connect": socket.socket.connect,
                "connect_ex": socket.socket.connect_ex}

    def spy(name):
        def call(self, address):
            if threading.current_thread() is not threading.main_thread():
                opened.append(address)
            return original[name](self, address)
        return call

    monkeypatch.setattr(socket.socket, "connect", spy("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", spy("connect_ex"))

    assert automation.get("/api/runs")[0] == 200
    assert automation.get(f"/api/run?id={RUN}")[0] == 200
    assert opened == []


@pytest.fixture
def linked_automation(root):
    """The `.state/` fixture behind a server that maps the link vocabulary."""
    _state(root)
    (root / "config" / "dbwiki.yaml").write_text(LINKED_CONFIG)
    srv, thread = serve(load_config(root), now=lambda: RUNS_NOW)
    try:
        yield Client(srv.server_address)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


def test_an_incident_carries_a_link_per_reference_it_holds(linked):
    status, body = linked.get(f"/api/incidents/{OPEN_SLUG}")
    assert status == 200
    assert [link["state"] for link in body["links"]] == ["available",
                                                         "missing"]
    logs, document = body["links"]
    assert logs["label"] == "Open exact logs"
    assert logs["url"].startswith(f"{KIBANA}/app/discover")
    assert CODE in logs["description"] and DB in logs["description"]
    assert document["url"] is None
    assert "no representative document" in document["note"]


def test_every_agent_stage_links_the_trace_its_event_id_seeds(linked_agentic):
    """The exporter seeds the trace id from the ledger line's `event_id`
    (`observability.trace_id`), so the run screen and the incident screen
    name the trace without asking Langfuse."""
    _, body = linked_agentic.get(f"/api/run?id={RUN}")
    assert [s["task"] for s in body["agents"]] == ["ingest", "ingest"]
    stage = body["agents"][0]
    assert stage["trace"]["state"] == "available"
    assert stage["trace"]["url"] == (
        f"{LANGFUSE}/project/dbwiki/traces/"
        f"{observability.trace_id(stage['event_id'])}")
    assert stage["event_id"] in stage["trace"]["description"]
    _, page = linked_agentic.get(f"/api/incidents/{OPEN_SLUG}")
    assert [s["trace"]["url"] for s in page["touched_by"][0]["stages"]] \
        == [s["trace"]["url"] for s in body["agents"]]


def test_a_stage_trace_is_unavailable_where_no_langfuse_is_configured(agentic):
    _, body = agentic.get(f"/api/run?id={RUN}")
    assert {s["trace"]["state"] for s in body["agents"]} == {"unavailable"}
    assert body["agents"][0]["trace"]["url"] is None


def test_an_unconfigured_deployment_answers_the_reason_and_no_url(api):
    _, body = api.get(f"/api/incidents/{OPEN_SLUG}")
    assert [link["state"] for link in body["links"]] == ["unavailable",
                                                         "missing"]
    assert all(link["url"] is None for link in body["links"])
    assert body["links"][0]["note"] == "no links.kibana.base is configured"


def test_a_wiki_page_carries_the_links_its_own_references_resolve_to(linked):
    status, body = linked.get(f"/api/page?path={OPEN_PATH}")
    assert status == 200
    assert [link["state"] for link in body["links"]] == ["available",
                                                         "missing"]


def test_a_page_holding_no_reference_answers_an_empty_link_list(linked):
    _, body = linked.get(f"/api/page?path={REPORT_PATH}")
    assert body["links"] == []


def test_a_run_carries_its_trace_and_its_elk_history(linked_automation):
    status, body = linked_automation.get(f"/api/run?id={RUN}")
    assert status == 200
    trace, history = body["links"]
    assert trace["state"] == "available"
    assert trace["url"] == f"{LANGFUSE}/project/dbwiki/sessions/{RUN}"
    assert history["state"] == "available"
    assert "dv-dbwiki" in history["url"]
    assert history["label"] == "Open the run's ELK history"


def test_a_run_on_an_unconfigured_deployment_says_which_key_is_missing(
        automation):
    _, body = automation.get(f"/api/run?id={RUN}")
    assert [link["state"] for link in body["links"]] == ["unavailable",
                                                         "unavailable"]
    assert body["links"][0]["note"] == "no links.langfuse.base is configured"


def _touched_wiki(root):
    """Two more commits on the open incident page: one by the tick the agent
    ledger holds stages for, and one hand edit with no `Run-ID:` trailer."""
    wiki = root / "wiki"
    page = wiki / OPEN_PATH
    page.write_text(page.read_text() + "\n<!-- the tick wrote this -->\n")
    git(wiki, "commit", "-am",
        f"ingest: {DB} {CODE}\n\nRun-ID: {RUN}\nActor: agent")
    page.write_text(page.read_text() + "\n<!-- and a human tidied it -->\n")
    git(wiki, "commit", "-am", "tidy the timeline wording")


@pytest.fixture
def agentic(root):
    """The `.state/` fixture over a wiki whose history the same tick wrote,
    behind a server whose clock is pinned: the window is an age against now,
    so an unpinned clock would make every span assertion a fact about the day
    the suite ran."""
    _state(root)
    _touched_wiki(root)
    with (root / ".state" / AGENT_LOG).open("a") as log:
        log.write(json.dumps(_agent_line(
            "ev-lint", LINT_RUN, db="", at=LINT_AT, task="lint",
            adapter="codex", model="gpt-5.6-luna", model_tier="cheap",
            mode="agentic", duration_s=29.9)) + "\n")
    srv, thread = serve(load_config(root), now=lambda: RUNS_NOW)
    try:
        yield Client(srv.server_address)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


@pytest.fixture
def linked_agentic(root):
    """`agentic` behind `linked`'s config: the ledger lines of a tick and a
    Langfuse the deployment names, which is what a stage's trace link needs
    both of."""
    (root / "config" / "dbwiki.yaml").write_text(LINKED_CONFIG)
    _state(root)
    _touched_wiki(root)
    srv, thread = serve(load_config(root), now=lambda: RUNS_NOW)
    try:
        yield Client(srv.server_address)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


QUIET_CODE = "ORA-00060"
RECENT_CODE = "ORA-01555"
QUIET_SEEN = "2026-07-22"
RECENT_SEEN = "2026-08-30"
QUIET_SLUG = "2026-07-20-cdb1-ora-00060"
RECENT_SLUG = "2026-08-28-cdb1-ora-01555"


def _quiet_wiki(root):
    """Two more incidents on the fleet wiki, one whose code stopped happening
    long before the pinned clock and one whose code happened the day before
    it, each with the error page carrying the occurrence row that says so."""
    wiki = root / "wiki"
    for code, day in ((QUIET_CODE, QUIET_SEEN), (RECENT_CODE, RECENT_SEEN)):
        (wiki / f"errors/{code}.md").write_text(
            f"---\ntype: error-class\n---\n\n# {code}\n\n"
            "## Occurrences\n\n"
            "| day | db | note | evidence |\n|---|---|---|---|\n"
            f"| {day} | {DB} | seen on the standby | {DIGEST} |\n")
    (wiki / f"incidents/{QUIET_SLUG}.md").write_text(incident_page(
        DB, f"{QUIET_CODE} deadlocks on the batch load",
        error_codes=(QUIET_CODE,)))
    (wiki / f"incidents/{RECENT_SLUG}.md").write_text(incident_page(
        DB, f"{RECENT_CODE} on the nightly export",
        error_codes=(RECENT_CODE,)))
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "ingest: quiet and noisy incidents")


@pytest.fixture
def quiet(root):
    """The fleet fixture's wiki, two more incidents, and the same pinned
    clock the other snapshot-backed read views answer against."""
    _fleet_wiki(root)
    _quiet_wiki(root)
    srv, thread = serve(load_config(root), now=lambda: FLEET_NOW)
    try:
        yield Client(srv.server_address)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


def test_the_agents_view_joins_the_ledger_to_the_pages_the_tick_wrote(agentic):
    """The whole point of the endpoint: `.state/` says which model ran which
    stage, the wiki's `Run-ID:` trailer says which incident page the same tick
    committed, and nothing but that trailer joins them."""
    status, body = agentic.get("/api/agents?hours=48")
    assert status == 200
    tick = next(t for t in body["ticks"] if t["run_id"] == RUN)
    assert tick["command"] == "run"
    assert [inc["slug"] for inc in tick["incidents"]] == [OPEN_SLUG]
    assert tick["incidents"][0]["status"] == "open"
    assert tick["incidents"][0]["db"] == DB
    assert [s["task"] for s in tick["stages"]] == ["ingest", "ingest"]
    assert {s["model"] for s in tick["stages"]} == {"qwen3-30b"}


def test_a_tick_that_wrote_nothing_carries_an_empty_incident_list(agentic):
    """The weekly lint ran a model and committed no incident page. It is a
    row of the loop's work all the same, and dropping it would draw the night
    as quieter than it was."""
    _, body = agentic.get("/api/agents?hours=48")
    lint = next(t for t in body["ticks"] if t["run_id"] == LINT_RUN)
    assert lint["incidents"] == []
    assert lint["command"] == "lint"
    assert [s["task"] for s in lint["stages"]] == ["lint"]


def test_the_ticks_are_newest_first(agentic):
    _, body = agentic.get("/api/agents?hours=48")
    assert [t["run_id"] for t in body["ticks"]] == [LINT_RUN, RUN]
    assert body["ticks"][0]["started"] > body["ticks"][1]["started"]


def test_the_hand_edit_is_attributed_to_no_tick(agentic):
    """Two commits landed on the page and only one carries a trailer, so the
    incident may appear exactly once across every tick."""
    _, body = agentic.get("/api/agents?hours=48")
    named = [inc["slug"] for tick in body["ticks"] for inc in tick["incidents"]]
    assert named.count(OPEN_SLUG) == 1


def test_the_agents_view_carries_both_provenances(agentic):
    """It reads `.state/`, which the caps bound, and the wiki at a revision.
    A reader has to be able to say which half an absence came from."""
    _, body = agentic.get("/api/agents?hours=48")
    assert body["revision"] == body["head"]
    assert len(body["revision"]) == 40
    assert {row["name"] for row in body["coverage"]} == {
        "run_health", "agent_runs", "run_starts"}
    assert body["generated_at"] == RUNS_NOW


def test_the_model_table_measures_every_stage_in_the_window(agentic):
    _, body = agentic.get("/api/agents?hours=48")
    rolled = {row["model"]: row for row in body["models"]}
    assert rolled["qwen3-30b"]["totals"]["stages"] == 2
    assert rolled["qwen3-30b"]["tiers"] == {"strong": 2}
    assert rolled["qwen3-30b"]["ok"] == 2
    assert sum(row["totals"]["stages"] for row in body["models"]) \
        == body["totals"]["stages"]


def test_an_unpriced_stage_is_never_summed_as_a_measured_zero(agentic):
    """One of the three ledger lines predates usage recording and carries no
    usage block at all. The window total has to say so rather than presenting
    two measured prices as three."""
    _, body = agentic.get("/api/agents?hours=48")
    assert body["totals"]["stages"] == 3
    assert body["totals"]["priced_stages"] == 2
    assert body["totals"]["token_stages"] == 2
    assert next(s for tick in body["ticks"] for s in tick["stages"]
                if s["event_id"] == "ev-ingest-cdb3")["usage"] is None


def test_a_narrow_window_drops_the_older_ticks(agentic):
    """`RUNS_NOW` is 2026-08-31T09:00Z. The lint stage is four hours behind
    it and the ingest stages twenty-eight, so six hours reaches one tick and
    one hour reaches none."""
    _, wide = agentic.get("/api/agents?hours=48")
    _, six = agentic.get("/api/agents?hours=6")
    _, narrow = agentic.get("/api/agents?hours=1")
    assert [t["run_id"] for t in wide["ticks"]] == [LINT_RUN, RUN]
    assert [t["run_id"] for t in six["ticks"]] == [LINT_RUN]
    assert narrow["window_hours"] == 1 and narrow["ticks"] == []
    assert narrow["models"] == [] and narrow["totals"]["stages"] == 0


def test_the_window_defaults_to_the_span_the_screen_opens_on(agentic):
    _, body = agentic.get("/api/agents")
    assert body["window_hours"] == 48


@pytest.mark.parametrize("query", ["?hours=0", "?hours=721", "?hours=all",
                                   "?hours=-3"])
def test_a_window_outside_the_touch_join_is_refused_rather_than_clamped(
        agentic, query):
    """A page asking for a year has a bug, and silently drawing thirty days
    would hide it. The ceiling is the touch join's own window: past it the
    stages keep arriving and the incidents under them stop."""
    status, body = agentic.get(f"/api/agents{query}")
    assert status == 400
    assert body["error"] == "bad_hours"


def test_the_incident_screen_lists_the_ticks_that_wrote_its_page(agentic):
    status, body = agentic.get(f"/api/incidents/{OPEN_SLUG}")
    assert status == 200
    assert [row["run_id"] for row in body["touched_by"]] == [RUN]
    row = body["touched_by"][0]
    assert len(row["commits"]) == 1
    assert [s["task"] for s in row["stages"]] == ["ingest", "ingest"]
    assert row["stages"][0]["duration_s"] == 41.2


def test_an_incident_no_tick_wrote_carries_an_empty_touch_list(agentic):
    _, body = agentic.get(f"/api/incidents/{MON_SLUG}")
    assert body["touched_by"] == []
def _span(day):
    return (dt.date.fromisoformat(FLEET_NOW[:10])
            - dt.date.fromisoformat(day)).days


def test_the_queue_carries_the_day_each_incidents_codes_were_last_seen(quiet):
    _, body = quiet.get("/api/incidents")
    days = {row["slug"]: row["last_seen"] for row in body["incidents"]}

    assert days[QUIET_SLUG] == QUIET_SEEN
    assert _span(days[QUIET_SLUG]) == 40, \
        "the reader counts 40 days of quiet from the day alone"
    assert days[RECENT_SLUG] == RECENT_SEEN
    assert _span(days[RECENT_SLUG]) == 1
    assert days[OPEN_SLUG] == "2026-08-28"
    assert days[MON_SLUG] is None, \
        "a page that links no error code joins no occurrence row"


def test_the_queue_sends_the_day_and_never_a_quiet_verdict(quiet):
    _, body = quiet.get("/api/incidents")
    row = next(r for r in body["incidents"] if r["slug"] == QUIET_SLUG)
    assert not [key for key in row if "quiet" in key], \
        "the span is the page's to derive against its own today"


def test_the_incident_view_carries_the_day_its_codes_were_last_seen(quiet):
    status, body = quiet.get(f"/api/incidents/{QUIET_SLUG}")
    assert status == 200
    assert body["last_seen"] == QUIET_SEEN

    _, recent = quiet.get(f"/api/incidents/{RECENT_SLUG}")
    assert recent["last_seen"] == RECENT_SEEN

    _, silent = quiet.get(f"/api/incidents/{MON_SLUG}")
    assert silent["last_seen"] is None


LINKS_YAML = """\
title: Fixture Link Board
intro: Everything reachable over Tailscale, with `dbwiki stats` on the box.
sections:
  - title: Dashboards
    note: Generated by `build_dashboards.py`, not edited in the UI.
    links:
      - name: Fleet triage
        what: "Where on call starts: `oracle_error` by database."
        url: http://box.example.invalid:5601/app/dashboards
        tag: tailscale
  - title: Repositories
    links:
      - name: logbook
        url: https://github.com/example/logbook
        tag: github
"""


@pytest.fixture
def boarded(root):
    """The same server over a project that has a link board. The file is
    written before `load_config` because the server reads it once, at
    construction, exactly as it reads the rest of `config/`."""
    (root / "config" / "links.yaml").write_text(LINKS_YAML)
    srv, thread = serve(load_config(root))
    try:
        yield Client(srv.server_address)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


def test_the_link_board_arrives_as_the_operator_wrote_it(boarded):
    status, body = boarded.get("/api/links")
    assert status == 200
    assert body["title"] == "Fixture Link Board", "the board names itself"
    assert body["intro"].startswith("Everything reachable"), \
        "the intro crosses whole, backticks included"
    assert [s["title"] for s in body["sections"]] == ["Dashboards",
                                                      "Repositories"], \
        "sections keep the order of the file"


def test_a_link_carries_the_four_keys_the_page_draws(boarded):
    _, body = boarded.get("/api/links")
    for section in body["sections"]:
        for link in section["links"]:
            assert set(link) == {"name", "what", "url", "tag"}


def test_an_absent_what_or_note_crosses_as_an_empty_string(boarded):
    _, body = boarded.get("/api/links")
    dashboards, repositories = body["sections"]
    assert dashboards["note"], "a section that was given a note keeps it"
    assert repositories["note"] == "", \
        "a section with no note sends an empty one"
    assert repositories["links"][0]["what"] == "", \
        "a link with no explanation sends an empty one"
    assert dashboards["links"][0]["tag"] == "tailscale", "the tag is its word"


def test_a_project_with_no_board_answers_an_empty_one(api):
    status, body = api.get("/api/links")
    assert status == 200
    assert body["sections"] == [], "no file means no sections, not a 404"
    assert body["title"] == "Links", "the empty board still has a heading"


def test_a_url_the_board_could_not_hold_stops_the_server(root):
    """The refusal belongs at startup: an operator restarting after an edit
    finds out immediately, rather than on somebody's later click."""
    (root / "config" / "links.yaml").write_text(
        "sections: [{title: X, links: "
        "[{name: N, url: 'ftp://box/', tag: github}]}]")
    with pytest.raises(ValueError, match="not http or https"):
        portal.make_server(load_config(root), bind=("127.0.0.1", 0))
