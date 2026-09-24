"""The three inbox endpoints, driven over a real loopback socket.

`tests/test_review.py` proves what a week selects and what `acks.json` means;
this file proves what reaches the browser and what a click may write, so it
binds an ephemeral port and speaks HTTP, borrowing `tests/test_portal_api.py`'s
client and its server harness rather than restating them, which is what
`tests/test_portal_advisory.py` already does.

Reviews are written here as files rather than produced by `review.run`. The
published file *is* the contract between the cron stage and the portal, and a
test that ran the stage would prove the stage instead of the edge.

The claim this file exists to protect is the single-writer split: an
acknowledge or a suppress writes `acks.json` and nothing else, so `state.json`
and the review file are compared byte for byte after every POST.
"""

import json
import urllib.parse

import pytest

from dbwiki import review, transaction
from dbwiki.config import load_config
from dbwiki.portal.identity import Principal, Role, TrustedOperator
from test_portal_api import Client, git, serve

DB = "cdb1"
SLUG = "2026-08-05-cdb1-ora-00600"
PATH = f"incidents/{SLUG}.md"
CODE = "ORA-00600"

THIS_WEEK = "2026-W36"
LAST_WEEK = "2026-W35"
GENERATED = "2026-09-07T10:00:00Z"
NOW = "2026-09-08T09:30:00Z"
REVISION = "3f2a1b7c9d4e5f60718293a4b5c6d7e8f9012345"

#: One string only a review file's prompt-shaped fields carry. Every fixture
#: below plants it wherever a future `review.py` field could, so "the wire
#: carries nothing the table does not name" is one scan per response. Not
#: inside a reason's `evidence`, which is the detectors' own key namespace and
#: is allowlisted by shape rather than by name; `test_portal_wire.py` is where
#: that boundary is drawn.
SENTINEL = "PACKED-BODY-9f3a1c"

STALE = review.fingerprint("stale_open", SLUG, DB, "stale_open")
CROSS = review.fingerprint("cross_db", CODE, "-", "multi_db")
GONE = review.fingerprint("worsening", "ORA-01555", DB, "occurrences_up")

CONFIG = """\
elasticsearch: {url: "http://127.0.0.1:9200"}
sources: {}
wiki_repo: wiki
state_dir: .state
digest_dir: digests
report: {push: false, link_base: "https://example.invalid/blob/main"}
portal: {lock_wait_s: 1.0}
agents: {pi: {cheap: "gemma-3", strong: "qwen"}}
"""

KEEP_TWO_CONFIG = CONFIG + "review: {keep_reviews: 2}\n"


def finding(fingerprint, kind, code, slug, db, severity, movement, *,
            path="") -> dict:
    return {"fingerprint": fingerprint, "kind": kind, "code": code,
            "slug": slug, "db": db, "title": f"{code} on {db}", "path": path,
            "severity": severity,
            "reasons": [{"code": code, "prompt": SENTINEL,
                         "evidence": {"count": 4, "days": ["2026-09-01"]}}],
            "evidence_hash": "0123456789ab", "movement": movement,
            "explanation": f"{slug} on {db}: {kind} ({code}, {severity})."}


def review_file(review_id, findings, **overrides) -> dict:
    return {
        "schema_version": review.REVIEW_SCHEMA_VERSION,
        "review_id": review_id,
        "generated_at": GENERATED,
        "source_revision": REVISION,
        "window": {"from": "2026-08-10T10:00:00Z", "to": GENERATED,
                   "days": 28},
        "findings": findings,
        "changes": {name: [] for name in review.CHANGE_BUCKETS},
        "counts": {"selected": len(findings), "high": 1, "normal": 1,
                   "shadowed": 0,
                   **{name: 0 for name in review.CHANGE_BUCKETS}},
        "explanation": f"{review_id}: {len(findings)} selected.",
        "synthesis": {"summary": "one incident has gone untouched",
                      "themes": [{"title": "nobody has acted",
                                  "detail": "no record since 2026-08-06",
                                  "evidence_refs": [PATH]}],
                      "evidence_refs": [PATH], "model_tier": "cheap"},
        "synthesis_error": "",
        "pack_manifest": [{"kind": "summary", "heading": "the selection",
                           "path": "", "chars": 412, "truncated": False,
                           "text": SENTINEL}],
        "deliveries": [],
        "prompt": SENTINEL,
        **overrides}


THIS = review_file(THIS_WEEK, [
    finding(STALE, "stale_open", "stale_open", SLUG, DB, "high", "carried",
            path=PATH),
    finding(CROSS, "cross_db", "multi_db", CODE, "-", "normal", "new")])

LAST = review_file(LAST_WEEK, [
    finding(GONE, "worsening", "occurrences_up", "ORA-01555", DB, "normal",
            "new")])


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A project whose wiki holds one incident page and whose `.state` holds
    two published reviews, this week's and last week's."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "dbwiki.yaml").write_text(CONFIG)
    wiki = tmp_path / "wiki"
    (wiki / "incidents").mkdir(parents=True)
    (wiki / PATH).write_text(
        f"---\ntype: incident\ndb: {DB}\nstatus: open\n---\n\n# {CODE}\n")
    (wiki / "index.md").write_text("---\ntype: index\n---\n\n# Logbook\n")
    git(wiki, "init", "-b", "main")
    git(wiki, "config", "user.email", "dba@example.com")
    git(wiki, "config", "user.name", "DBA")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "init")
    reviews = tmp_path / ".state" / review.REVIEW_DIR / review.REVIEWS_DIR
    reviews.mkdir(parents=True)
    for document in (THIS, LAST):
        (reviews / f"{document['review_id']}.json").write_text(
            json.dumps(document, indent=1, sort_keys=True))
    (tmp_path / ".state" / review.REVIEW_DIR / review.STATE_FILE).write_text(
        json.dumps({"schema_version": review.REVIEW_SCHEMA_VERSION,
                    "last_review_id": THIS_WEEK, "history": {}}))
    return tmp_path


class _Serving:
    def __init__(self, cfg, provider):
        self.cfg = cfg
        self.provider = provider

    def __enter__(self) -> Client:
        self.srv, self.thread = serve(self.cfg, provider=self.provider,
                                      now=lambda: NOW)
        return Client(self.srv.server_address)

    def __exit__(self, *exc) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=10)


def client(root, config=CONFIG, provider=None):
    """A server over `root` with `config` written to its `dbwiki.yaml`. The
    file is rewritten before `load_config` rather than the loaded object
    patched, because what an operator edits is a block in `dbwiki.yaml`."""
    (root / "config" / "dbwiki.yaml").write_text(config)
    return _Serving(load_config(root), provider)


@pytest.fixture
def api(root):
    with client(root) as made:
        yield made


def viewer(root):
    return TrustedOperator(Principal(
        transaction.resolve_actor(root / "wiki"), frozenset({Role.VIEWER})))


#: The two files the cron stage owns. `acks.json` is the portal's, and the
#: audit log is append-only through `health._append_capped`, which is why both
#: writers may reach it: neither rewrites the other's line.
CRON_FILES = (review.STATE_FILE, "reviews")


def state_files(root) -> dict:
    """Every cron-owned `.state/review/` file, as bytes, so what a POST left
    alone can be compared rather than asserted about."""
    home = root / ".state" / review.REVIEW_DIR
    return {str(path.relative_to(home)): path.read_bytes()
            for path in sorted(home.rglob("*"))
            if path.is_file()
            and path.relative_to(home).parts[0] in CRON_FILES}


def audit_rows(root) -> list:
    log = root / ".state" / review.AUDIT_LOG
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line]


def test_the_inbox_lists_every_week_newest_first_and_no_findings(api):
    status, body = api.get("/api/inbox")
    assert status == 200
    assert body["generated_at"] == NOW
    assert [row["review_id"] for row in body["reviews"]] \
        == [THIS_WEEK, LAST_WEEK]
    assert all("findings" not in row for row in body["reviews"]), \
        "the inbox is a list of weeks; the findings are the review's own"
    assert body["reviews"][0]["counts"]["selected"] == 2
    assert body["reviews"][0]["synthesized"] is True


def test_neither_inbox_envelope_claims_a_wiki_revision(api):
    """`.state`-backed, the `runs` rule: there is no revision to name, and the
    page must not be able to draw a stale badge against one."""
    for path in ("/api/inbox", f"/api/review?id={THIS_WEEK}"):
        _, body = api.get(path)
        assert "revision" not in body and "head" not in body
        assert "evidence_revision" not in body
    _, body = api.get(f"/api/review?id={THIS_WEEK}")
    assert body["source_revision"] == REVISION


def test_a_review_carries_its_findings_with_the_ack_state_merged_in(api):
    status, body = api.get(f"/api/review?id={THIS_WEEK}")
    assert status == 200
    assert body["review_id"] == THIS_WEEK
    assert [row["fingerprint"] for row in body["findings"]] == [STALE, CROSS]
    assert body["findings"][0]["ack"] == {
        "acknowledged_at": None, "suppressed_until": None, "actor": ""}
    assert body["synthesis"]["themes"][0]["evidence_refs"] == [PATH]

    assert api.post("/api/inbox", {"fingerprint": STALE,
                                   "action": "acknowledge"})[0] == 200
    _, after = api.get(f"/api/review?id={THIS_WEEK}")
    assert after["findings"][0]["ack"]["acknowledged_at"] == NOW
    assert after["findings"][0]["ack"]["actor"] == "dba@example.com"
    assert after["findings"][1]["ack"]["acknowledged_at"] is None


def test_no_response_carries_a_field_the_review_table_does_not_name(api):
    api.post("/api/inbox", {"fingerprint": STALE, "action": "acknowledge"})
    for method, path in (("GET", "/api/inbox"),
                         ("GET", f"/api/review?id={THIS_WEEK}"),
                         ("GET", f"/api/review?id={LAST_WEEK}")):
        _, raw, _ = api.send(method, path)
        assert SENTINEL not in raw.decode(), \
            f"{path} carried a field only `_REVIEW_KEYS` could have stopped"


def test_a_review_id_the_state_directory_never_held_names_the_retention(api):
    status, body = api.get("/api/review?id=2019-W01")
    assert status == 404 and body["error"] == "unknown_review"
    assert "kept" in body["message"] and "pruned" in body["message"]
    assert str(review.Rules().keep_reviews) in body["message"]


@pytest.mark.parametrize("shape", ["absolute", "parent", "unreadable",
                                   "no_such_file", "nul"])
def test_a_review_id_that_is_not_a_week_is_a_miss_and_reads_nothing(
        api, root, shape):
    """Issue 09: `?id=` became `reviews/{id}.json` unchecked, so an absolute
    id read any review-shaped `*.json` on disk and 500 against 404 probed for
    files. Only `YYYY-Www` names a week; anything else is the same 404 a
    pruned week is, whatever sits at the path it spells."""
    outside = root / "outside"
    outside.mkdir()
    (outside / "leak.json").write_text(json.dumps(
        {**THIS, "explanation": "LEAKED-7c1e"}))
    (outside / "garbage.json").write_text("not json at all")
    review_id = {"absolute": str(outside / "leak"),
                 "parent": "../../../outside/leak",
                 "unreadable": str(outside / "garbage"),
                 "no_such_file": str(outside / "nothing"),
                 "nul": "2026-W36\x00"}[shape]
    status, raw, _ = api.send(
        "GET", "/api/review?" + urllib.parse.urlencode({"id": review_id}))
    body = json.loads(raw)
    assert (status, body["error"]) == (404, "unknown_review")
    assert "LEAKED-7c1e" not in raw.decode()


def test_the_retention_the_404_names_is_the_one_this_deployment_configured(
        root):
    with client(root, KEEP_TWO_CONFIG) as made:
        _, body = made.get("/api/review?id=2019-W01")
        assert "the newest 2 are kept" in body["message"]


def test_one_unreadable_week_costs_itself_and_not_the_inbox(root):
    """`review._check_version` exists for a newer cron stage publishing under
    a portal that has rolled back. That week must cost itself: the list stays
    honest about the rest, and an operator can still act on a finding in a
    week this code does understand."""
    reviews = root / ".state" / review.REVIEW_DIR / review.REVIEWS_DIR
    (reviews / "2026-W40.json").write_text(json.dumps(
        {"schema_version": review.REVIEW_SCHEMA_VERSION + 1}))
    (reviews / "2026-W39.json").write_text("{not json")
    with client(root) as made:
        status, body = made.get("/api/inbox")
        assert status == 200
        assert [row["review_id"] for row in body["reviews"]] \
            == [THIS_WEEK, LAST_WEEK]
        assert made.post("/api/inbox", {"fingerprint": STALE,
                                        "action": "acknowledge"})[0] == 200
        named, answer = made.get("/api/review?id=2026-W40")
        assert named == 500, \
            "asked for by name, the file this code cannot read is the answer"
        assert answer["error"] == "internal"


def test_an_empty_inbox_is_an_empty_list_and_never_an_outage(root):
    for path in sorted((root / ".state" / review.REVIEW_DIR
                        / review.REVIEWS_DIR).glob("*.json")):
        path.unlink()
    with client(root) as made:
        status, body = made.get("/api/inbox")
        assert status == 200 and body["reviews"] == []


def test_an_acknowledge_is_idempotent_audited_and_writes_only_acks(api, root):
    before = state_files(root)
    status, body = api.post("/api/inbox", {"fingerprint": STALE,
                                           "action": "acknowledge"})
    assert status == 200
    assert body == {"fingerprint": STALE,
                    "ack": {"acknowledged_at": NOW, "suppressed_until": None,
                            "actor": "dba@example.com"}}
    assert state_files(root) == before, \
        "state.json and the review files are the cron stage's, not the portal's"
    acks = (root / ".state" / review.REVIEW_DIR / review.ACKS_FILE)
    written = acks.read_bytes()

    again = api.post("/api/inbox", {"fingerprint": STALE,
                                    "action": "acknowledge"})
    assert again == (200, body), "a second identical POST answers the same"
    assert acks.read_bytes() == written, "and writes no new byte"
    assert state_files(root) == before
    assert [row["event"] for row in audit_rows(root)] == ["acknowledged"], \
        "one act, one audit row, however many times it is posted"


def test_a_suppress_is_idempotent_audited_and_only_ever_extends(api, root):
    before = state_files(root)
    status, body = api.post("/api/inbox", {"fingerprint": CROSS,
                                           "action": "suppress", "days": 7})
    assert status == 200
    assert body["ack"]["suppressed_until"] == "2026-09-15T09:30:00Z"
    assert body["ack"]["acknowledged_at"] is None
    assert state_files(root) == before

    acks = (root / ".state" / review.REVIEW_DIR / review.ACKS_FILE)
    written = acks.read_bytes()
    assert api.post("/api/inbox", {"fingerprint": CROSS, "action": "suppress",
                                   "days": 7}) == (200, body)
    assert acks.read_bytes() == written
    assert api.post("/api/inbox", {"fingerprint": CROSS, "action": "suppress",
                                   "days": 3}) == (200, body), \
        "a shorter re-suppression is a no-op; suppression only extends forward"
    assert acks.read_bytes() == written
    assert [row["event"] for row in audit_rows(root)] == ["suppressed"]

    longer = api.post("/api/inbox", {"fingerprint": CROSS,
                                     "action": "suppress", "days": 30})[1]
    assert longer["ack"]["suppressed_until"] == "2026-10-08T09:30:00Z"
    assert [row["event"] for row in audit_rows(root)] \
        == ["suppressed", "suppressed"]


def test_a_finding_only_an_older_review_holds_is_still_actionable(api):
    """An operator can be reading last week's page, so the fingerprint is
    looked up across every review the deployment still holds."""
    status, body = api.post("/api/inbox", {"fingerprint": GONE,
                                           "action": "acknowledge"})
    assert status == 200 and body["ack"]["acknowledged_at"] == NOW


def test_a_fingerprint_no_published_review_holds_is_a_miss(api, root):
    status, body = api.post("/api/inbox", {"fingerprint": "0" * 12,
                                           "action": "acknowledge"})
    assert status == 404 and body["error"] == "unknown_item"
    assert not (root / ".state" / review.REVIEW_DIR
                / review.ACKS_FILE).exists(), \
        "a refused act writes nothing at all"
    assert audit_rows(root) == []


def test_the_role_gate_runs_before_the_fingerprint_is_looked_up(root):
    """A caller who may not write learns "forbidden" and not which
    fingerprints this workbench holds, the `advisory_start` order."""
    with client(root, provider=viewer(root)) as made:
        status, body = made.post("/api/inbox", {"fingerprint": "0" * 12,
                                                "action": "acknowledge"})
        assert status == 403 and body["error"] == "forbidden"
        assert body["required"] == "operator" and body["held"] == ["viewer"]
    assert audit_rows(root) == []


def test_a_viewer_may_still_read_the_inbox_and_a_review(root):
    with client(root, provider=viewer(root)) as made:
        assert made.get("/api/inbox")[0] == 200
        assert made.get(f"/api/review?id={THIS_WEEK}")[0] == 200


def test_an_inbox_body_is_allowlisted_key_by_key(api):
    cases = [
        ({"fingerprint": STALE, "action": "acknowledge",
          "actor": "root@example.com"}, "identity_is_server_side"),
        ({"fingerprint": STALE, "action": "acknowledge", "base": "x"},
         "unknown_key"),
        ({"fingerprint": "", "action": "acknowledge"}, "bad_fingerprint"),
        ({"fingerprint": STALE, "action": "delete"}, "bad_action"),
        ({"fingerprint": STALE, "action": "acknowledge", "days": 7},
         "bad_days"),
        ({"fingerprint": STALE, "action": "suppress"}, "bad_days"),
        ({"fingerprint": STALE, "action": "suppress", "days": 7,
          "at": "tomorrow"}, "bad_at"),
    ]
    for body, word in cases:
        status, answer = api.post("/api/inbox", body)
        assert (status, answer["error"]) == (400, word), body


def test_the_caller_may_pin_the_instant_the_act_is_recorded_at(api):
    _, body = api.post("/api/inbox", {"fingerprint": STALE,
                                      "action": "acknowledge",
                                      "at": "2026-09-09T12:00:00Z"})
    assert body["ack"]["acknowledged_at"] == "2026-09-09T12:00:00Z"


def test_the_router_separates_a_missing_route_from_a_wrong_method(api):
    assert api.json("POST", f"/api/review?id={THIS_WEEK}", body={})[0] == 405
    assert api.json("GET", "/api/nonsense")[0] == 404
    status, body = api.json("GET", "/api/nonsense")
    assert body["error"] == "no_route"
