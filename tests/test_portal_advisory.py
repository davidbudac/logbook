"""The advisory endpoints, driven over a real loopback socket.

`tests/test_advisory.py` proves what a tool may read and what a click may
spend over `Tree.of` fixtures; this file proves what reaches the browser, so
it binds an ephemeral port and speaks HTTP, borrowing
`tests/test_portal_api.py`'s client and its server harness rather than
restating them. The only fake is `harness.run_text`, patched on the module the
worker looks it up on: what is under test is the surface, and a real model
would make it a test of how long pi takes.

The claim this file exists to protect is that the material never crosses the
wire. `SENTINEL` is planted in everything a pack can reach, one run is driven
to `succeeded`, and every response of all three routes is scanned for it.
"""

import datetime as dt
import json
import time
import urllib.parse

import pytest

from dbwiki import advisory, harness, health, monitoring, transaction
from dbwiki.advisory import TOOLS
from dbwiki.config import load_config
from dbwiki.incidents import ErrorAbsent, MonitoringWindow, Status, set_status
from dbwiki.portal.identity import Principal, Role, TrustedOperator
from fixtures.incident_pages import incident_page
from test_portal_api import Client, git, serve

DB = "cdb1"
CODE = "ORA-00600"
SLUG = "2026-08-05-cdb1-ora-00600"
PATH = f"incidents/{SLUG}.md"
DAY = "2026-08-30"
DIGEST = f"digests/{DB}/{DAY}.md"
START = "2026-08-28T00:00:00Z"
UNTIL = "2026-09-02T00:00:00Z"
AT = "2026-08-30T14:22:10Z"
QUESTION = "did the standby ever catch up on the gap?"

#: One string only packed material may carry. Every fixture below plants it in
#: whatever it contributes to a prompt, so "no encoding of a run carries the
#: material" is one scan over every response.
SENTINEL = "PACKED-BODY-9f3a1c"

ANSWER = f"the standby stopped applying redo, per {PATH}"
USAGE = {"input_tokens": 4200, "output_tokens": 31, "cost_usd": 0.002}

TERMINAL = frozenset(str(status) for status in advisory.TERMINAL)

RUN_KEYS = {"run_id", "tool", "target", "at", "question", "status", "started",
            "finished", "duration_s", "evidence_revision", "context", "answer",
            "error"}
TOOL_KEYS = {"id", "label", "role", "tier", "enabled", "target_field", "asks",
             "max_answer_chars", "sources", "context", "context_chars",
             "ceiling"}
CONTEXT_KEYS = {"kind", "path", "heading", "chars", "truncated"}
CEILING_KEYS = {"window_h", "max_runs", "max_cost_usd", "runs",
                "measured_runs", "unmeasured_runs", "cost_usd", "reason"}
USAGE_KEYS = {"input_tokens", "output_tokens", "cost_usd", "known"}

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

OFF_CONFIG = CONFIG + "advisory: {enabled: false}\n"
ONE_ROW_OFF_CONFIG = CONFIG + (
    "advisory: {tools: {next-checks: {enabled: false}}}\n")
TIGHT_CONFIG = CONFIG + "advisory: {max_runs: 1}\n"

TOOLS_PATH = f"/api/incidents/{SLUG}/tools"
START_PATH = f"/api/incidents/{SLUG}/advisory"


def _page() -> str:
    window = MonitoringWindow(ErrorAbsent(CODE), START, UNTIL)
    page = incident_page(DB, f"{CODE} internal errors", error_codes=(CODE,),
                         body=f"The standby stopped applying redo. {SENTINEL}")
    return set_status(page, Status.MONITORING, updated=START,
                      monitoring=window)


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A project whose wiki holds one incident under monitoring, the error
    page it links and the digest its facts cite, each carrying `SENTINEL`, and
    a `.state` a tick left behind so the closure and digest readers have
    something to pack."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "dbwiki.yaml").write_text(CONFIG)
    wiki = tmp_path / "wiki"
    (wiki / "incidents").mkdir(parents=True)
    (wiki / "errors").mkdir()
    (wiki / "digests" / DB).mkdir(parents=True)
    (wiki / PATH).write_text(_page())
    (wiki / f"errors/{CODE}.md").write_text(
        f"---\ntype: error-class\n---\n\n# {CODE}\n\n## Meaning\n\n"
        f"{CODE} is reported. {SENTINEL}\n")
    (wiki / DIGEST).write_text(
        f"# {DB} - {DAY}\n\n## Deltas (never seen before / anomalies)\n\n"
        f"- {CODE} first seen on {DAY} {SENTINEL}\n")
    (wiki / "index.md").write_text(
        f"---\ntype: index\n---\n\n# Logbook\n\n- [[{PATH[:-3]}]]\n")
    git(wiki, "init", "-b", "main")
    git(wiki, "config", "user.email", "dba@example.com")
    git(wiki, "config", "user.name", "DBA")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "init")
    facts = tmp_path / ".state" / monitoring.MONITORING_DIR
    facts.mkdir(parents=True)
    (facts / f"{SLUG}.json").write_text(json.dumps({
        "verdict": "not_met",
        "signal": {"kind": "error_absent", "code": CODE},
        "window": {"start": START, "until": UNTIL},
        "observed": [{"day": DAY, "code": CODE, "count": 0,
                      "digest": f"digests/{DB}/{DAY}.json"}],
        "contradictions": [f"the listener still logs {SENTINEL}"],
        "evaluated_at": UNTIL,
        "source_revision": transaction.head(wiki)}))
    return tmp_path


def client(root, config=CONFIG, provider=None):
    """A server over `root` with `config` written to its `dbwiki.yaml`, as a
    context manager. The file is rewritten before `load_config` rather than
    the loaded object patched, because what an operator edits is a block in
    `dbwiki.yaml`."""
    (root / "config" / "dbwiki.yaml").write_text(config)
    return _Serving(load_config(root), provider)


class _Serving:
    def __init__(self, cfg, provider):
        self.cfg = cfg
        self.provider = provider

    def __enter__(self) -> Client:
        self.srv, self.thread = serve(self.cfg, provider=self.provider)
        return Client(self.srv.server_address)

    def __exit__(self, *exc) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=10)


@pytest.fixture
def api(root):
    with client(root) as made:
        yield made


def fake_run_text(monkeypatch, *, answer=ANSWER, usage=USAGE, raises=None):
    """Stand in for `harness.run_text`, patched on the module because
    `advisory.Runner._work` looks it up there at call time, which is what lets
    a patch reach a worker thread. Returns the prompts it was asked, in
    order."""
    seen: list[str] = []

    def run_text(prompt, model, timeout, provider=None, cwd=None,
                 telemetry=None):
        seen.append(prompt)
        if telemetry is not None:
            telemetry.update(adapter="pi", model=model or "gemma-3")
            if usage is not None:
                telemetry["usage"] = usage
        if raises is not None:
            raise raises
        return answer

    monkeypatch.setattr(harness, "run_text", run_text)
    return seen


def poll(made: Client, run_id: str) -> dict:
    """The run record once it stops moving. Bounded and polled rather than
    slept on: the worker is a thread, and a fixed wait is either a slow test
    or a flaky one."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status, body = made.get(f"/api/advisory?run={run_id}")
        assert status == 200
        if body["status"] in TERMINAL:
            return body
        time.sleep(0.005)
    raise AssertionError(f"{run_id} never reached a terminal status")


def started(made: Client, tool: str, at: str = AT) -> dict:
    status, body = made.post(START_PATH, {"tool": tool, "at": at})
    assert status == 202, body
    return body


def head(root) -> str:
    return transaction.head(root / "wiki")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_the_manifest_prices_every_row_and_reaches_no_model(api, root,
                                                            monkeypatch):
    seen = fake_run_text(monkeypatch)
    status, body = api.get(TOOLS_PATH)
    assert status == 200
    assert set(body) == {"slug", "evidence_revision", "tools"}
    assert body["slug"] == SLUG and body["evidence_revision"] == head(root)
    assert [row["id"] for row in body["tools"]] == list(TOOLS)
    for row in body["tools"]:
        assert set(row) == TOOL_KEYS
        spec = TOOLS[row["id"]]
        assert row["sources"] == [str(kind) for kind in spec.sources]
        assert row["role"] == "operator" and row["enabled"] is True
        assert row["max_answer_chars"] == spec.max_answer_chars
        assert set(row["ceiling"]) == CEILING_KEYS
        assert row["ceiling"]["reason"] == "" and row["ceiling"]["runs"] == 0
        assert all(set(entry) == CONTEXT_KEYS for entry in row["context"])
    explain = next(r for r in body["tools"] if r["id"] == "explain-incident")
    assert [e["path"] for e in explain["context"]][:2] == \
        [PATH, f"errors/{CODE}.md"]
    assert explain["context_chars"] == sum(
        e["chars"] for e in explain["context"])
    assert seen == []


def test_the_manifest_says_which_row_takes_the_operators_question(api):
    _, body = api.get(TOOLS_PATH)
    rows = {row["id"]: row for row in body["tools"]}
    assert rows["ask-incident"]["asks"] is True
    assert rows["ask-incident"]["label"] == "Interview this incident"
    assert [row["id"] for row in body["tools"] if row["asks"]] == \
        ["ask-incident"]


def test_a_question_reaches_the_run_it_produced(api, root, monkeypatch):
    seen = fake_run_text(monkeypatch)
    status, body = api.post(START_PATH, {"tool": "ask-incident", "at": AT,
                                         "question": QUESTION})
    assert status == 202, body
    assert set(body) == RUN_KEYS and body["question"] == QUESTION
    run = poll(api, body["run_id"])
    assert run["status"] == "succeeded" and run["question"] == QUESTION
    assert QUESTION in seen[0]


def test_a_malformed_ask_is_refused_before_any_evidence_is_read(api,
                                                                monkeypatch):
    """Both arms of one rule: the question and the row must agree. Neither
    refusal reads a page or reaches a model, which is why it sits beside the
    role gates rather than after the target is built."""
    seen = fake_run_text(monkeypatch)
    status, body = api.post(START_PATH, {"tool": "explain-incident", "at": AT,
                                         "question": QUESTION})
    assert status == 400 and body["error"] == "bad_question"

    status, body = api.post(START_PATH, {"tool": "ask-incident", "at": AT})
    assert status == 400 and body["error"] == "bad_question"
    assert seen == []


def test_the_row_that_fills_a_form_control_says_which_one(api):
    _, body = api.get(TOOLS_PATH)
    rows = {row["id"]: row for row in body["tools"]}
    assert rows["draft-note"]["target_field"] == "summary"
    assert rows["explain-incident"]["target_field"] == ""


def test_a_start_answers_202_with_the_queued_record_and_its_manifest(
        api, root, monkeypatch):
    fake_run_text(monkeypatch)
    body = started(api, "explain-incident")
    assert set(body) == RUN_KEYS
    assert body["status"] == "queued" and body["answer"] is None
    assert body["tool"] == "explain-incident" and body["target"] == SLUG
    assert body["at"] == AT and body["evidence_revision"] == head(root)
    assert body["finished"] == "" and body["error"] == ""
    assert [e["path"] for e in body["context"]][0] == PATH


def test_a_poll_answers_the_terminal_record_and_what_it_cost(api, monkeypatch):
    seen = fake_run_text(monkeypatch)
    run = poll(api, started(api, "explain-incident")["run_id"])
    assert set(run) == RUN_KEYS and run["status"] == "succeeded"
    assert set(run["answer"]) == {"text", "cites", "model", "usage"}
    assert run["answer"]["text"] == ANSWER
    assert run["answer"]["cites"] == [PATH]
    assert run["answer"]["model"] == "qwen"
    assert set(run["answer"]["usage"]) == USAGE_KEYS
    assert run["answer"]["usage"]["known"] is True
    assert run["answer"]["usage"]["cost_usd"] == 0.002
    assert run["duration_s"] >= 0 and run["finished"]
    assert len(seen) == 1


def test_a_cost_nobody_measured_is_never_rendered_as_nothing(api, monkeypatch):
    """The whole reason the advisory usage encoder is not `usage_json`: a
    per-click cost line may print a word, never $0.00 for a call nobody
    priced."""
    fake_run_text(monkeypatch, usage={"cost_usd": 0.01,
                                      "input_tokens": harness.UNKNOWN,
                                      "output_tokens": harness.UNKNOWN})
    run = poll(api, started(api, "explain-incident")["run_id"])
    usage = run["answer"]["usage"]
    assert usage == {"input_tokens": "unknown", "output_tokens": "unknown",
                     "cost_usd": 0.01, "known": False}


def test_a_harness_that_reported_nothing_says_so(api, monkeypatch):
    fake_run_text(monkeypatch, usage=None)
    run = poll(api, started(api, "explain-incident")["run_id"])
    assert run["answer"]["usage"] == harness.UNKNOWN


def test_no_response_carries_the_material_the_run_read(api, monkeypatch):
    """The claim the text/manifest split exists to make. The prompt is proof
    the material was really packed; the three routes are proof none of it
    comes back."""
    seen = fake_run_text(monkeypatch)
    run = poll(api, started(api, "summarize-evidence")["run_id"])
    assert SENTINEL in seen[0]
    bodies = [api.send("GET", TOOLS_PATH)[1],
              api.send("POST", START_PATH,
                       body={"tool": "summarize-evidence", "at": AT})[1],
              api.send("GET", f"/api/advisory?run={run['run_id']}")[1]]
    for raw in bodies:
        assert SENTINEL.encode() not in raw
    assert all(set(e) == CONTEXT_KEYS for e in run["context"])


#: Every word this workbench spells a permission or a verdict with. None of
#: them may be a key on an advisory answer: a page able to read a permission
#: off a model's reply would be a second gate beside `lifecycle.TRANSITIONS`,
#: and the operator could not tell which one had let the edit through.
GATE_WORDS = {"verdict", "allowed", "permitted", "legal", "decision",
              "transition", "next_status", "status_after", "role", "closure"}


def test_the_closure_narration_carries_no_field_a_gate_could_read(
        api, monkeypatch):
    """`explain-closure` reads the closure case, so it is the row an answer
    could most plausibly be mistaken for a ruling. Its envelope is pinned
    exactly, and every word a gate is spelled with is absent from it."""
    seen = fake_run_text(monkeypatch)
    run = poll(api, started(api, "explain-closure")["run_id"])
    assert set(run) == RUN_KEYS and run["status"] == "succeeded"
    assert set(run["answer"]) == {"text", "cites", "model", "usage"}
    assert GATE_WORDS.isdisjoint(run["answer"])
    assert GATE_WORDS.isdisjoint(run)
    assert [e["kind"] for e in run["context"]] == ["closure", "digest"]
    assert f"the listener still logs {SENTINEL}" in seen[0], \
        "the contradiction the tick recorded reached the model verbatim"


def test_the_narration_is_offered_on_an_incident_with_no_facts_at_all(
        api, root, monkeypatch):
    """The page hides the button only where `closure` is null, and that is the
    page's rule alone: the server keeps offering the row, packs nothing for
    it, and answers a click that asks anyway."""
    fake_run_text(monkeypatch)
    (root / ".state" / monitoring.MONITORING_DIR / f"{SLUG}.json").unlink()
    _, incident = api.get(f"/api/incidents/{SLUG}")
    assert incident["closure"] is None
    _, body = api.get(TOOLS_PATH)
    row = next(r for r in body["tools"] if r["id"] == "explain-closure")
    assert row["ceiling"]["reason"] == "" and row["context"] == []
    assert row["context_chars"] == 0
    run = poll(api, started(api, "explain-closure")["run_id"])
    assert run["status"] == "succeeded" and run["context"] == []


def test_two_posts_of_one_click_are_one_run_and_one_model_call(api,
                                                                monkeypatch):
    """`run_id` is `sha256(tool|target|at)`, so the page's retry after a
    network failure converges on the run it already started."""
    seen = fake_run_text(monkeypatch)
    first = started(api, "next-checks")
    poll(api, first["run_id"])
    second = started(api, "next-checks")
    assert second["run_id"] == first["run_id"]
    assert second["status"] == "succeeded"
    assert len(seen) == 1


def test_an_unknown_tool_is_a_miss_naming_the_tools(api, monkeypatch):
    seen = fake_run_text(monkeypatch)
    status, body = api.post(START_PATH, {"tool": "explain-everything"})
    assert status == 404 and body["error"] == "unknown_tool"
    assert "explain-incident" in body["message"]
    assert seen == []


def test_a_row_the_deployment_turned_off_is_unavailable(root, monkeypatch):
    seen = fake_run_text(monkeypatch)
    with client(root, ONE_ROW_OFF_CONFIG) as made:
        status, body = made.post(START_PATH, {"tool": "next-checks"})
        assert status == 503 and body["error"] == "tool_disabled"
        _, manifest = made.get(TOOLS_PATH)
        rows = {row["id"]: row for row in manifest["tools"]}
        assert rows["next-checks"]["enabled"] is False
        assert rows["next-checks"]["ceiling"]["reason"] == "tool_disabled"
        assert rows["explain-incident"]["enabled"] is True
    assert seen == []


def test_the_whole_block_switches_off_every_route(root, monkeypatch):
    seen = fake_run_text(monkeypatch)
    with client(root, OFF_CONFIG) as made:
        for status, body in (made.get(TOOLS_PATH),
                             made.post(START_PATH, {"tool": "next-checks"}),
                             made.get("/api/advisory?run=abc123")):
            assert status == 503 and body["error"] == "advisory_unavailable"
            assert "advisory" in body["message"]
    assert seen == []


def test_a_ceiling_refuses_the_click_and_says_what_it_reached(root,
                                                               monkeypatch):
    """The refusal is addressable: it names the run it recorded, so a click
    that spent nothing still has an answer the page can show."""
    seen = fake_run_text(monkeypatch)
    health.append_advisory_run(root / ".state",
                               {"tool": "explain-incident", "at": now(),
                                "status": "succeeded", "cost_usd": 0.004})
    with client(root, TIGHT_CONFIG) as made:
        status, body = made.post(START_PATH, {"tool": "explain-incident",
                                              "at": AT})
        assert status == 429 and body["error"] == "ceiling_reached"
        assert body["reason"] == "max_runs"
        assert set(body["ceiling"]) == CEILING_KEYS
        ceiling = body["ceiling"]
        assert ceiling["runs"] == 1 and ceiling["max_runs"] == 1
        assert ceiling["cost_usd"] == 0.004
        assert ceiling["unmeasured_runs"] == 0
        refused = poll(made, body["run_id"])
        assert refused["status"] == "refused" and refused["answer"] is None
    assert seen == []


def viewer(root):
    return TrustedOperator(Principal(
        transaction.resolve_actor(root / "wiki"), frozenset({Role.VIEWER})))


@pytest.mark.parametrize("config", [CONFIG, OFF_CONFIG])
def test_the_role_gate_runs_before_the_switch_and_before_the_model(
        root, monkeypatch, config):
    """A caller who may not run a tool learns "forbidden" and nothing else:
    not which tools this deployment has, not whether the block is on. The
    unknown tool id and the switched-off config are both in the request, and
    neither reaches the answer."""
    seen = fake_run_text(monkeypatch)
    with client(root, config, provider=viewer(root)) as made:
        status, body = made.post(START_PATH, {"tool": "explain-everything"})
        assert status == 403 and body["error"] == "forbidden"
        assert body["required"] == "operator" and body["held"] == ["viewer"]
    assert seen == []


def test_a_viewer_may_still_read_the_manifest_and_a_finished_run(root,
                                                                 monkeypatch):
    fake_run_text(monkeypatch)
    with client(root) as made:
        run = poll(made, started(made, "next-checks")["run_id"])
    with client(root, provider=viewer(root)) as reader:
        assert reader.get(TOOLS_PATH)[0] == 200
        status, body = reader.get(f"/api/advisory?run={run['run_id']}")
        assert status == 200 and body["answer"]["text"] == ANSWER


def test_a_run_the_runner_never_held_is_a_miss(api):
    status, body = api.get("/api/advisory?run=000000000000")
    assert status == 404 and body["error"] == "unknown_run"
    assert "newest" in body["message"]


def test_a_run_id_that_is_not_a_plain_id_is_a_miss_and_reads_nothing(
        api, root, monkeypatch):
    """Issue 09: `?run=` became `advisory/{run}.json` unchecked. A real run's
    record copied outside the runs directory is what a traversal would find;
    it must stay unreachable by every spelling of its path."""
    fake_run_text(monkeypatch)
    run = poll(api, started(api, "next-checks")["run_id"])
    record = root / ".state" / advisory.RUNS_DIR / f"{run['run_id']}.json"
    (root / "outside.json").write_bytes(record.read_bytes())
    for spelling in (str(root / "outside"), "../../outside", "../outside",
                     f"./{run['run_id']}", f"{run['run_id']}/"):
        status, body = api.get("/api/advisory?"
                               + urllib.parse.urlencode({"run": spelling}))
        assert (status, body["error"]) == (404, "unknown_run"), spelling
    assert api.get(f"/api/advisory?run={run['run_id']}")[0] == 200


def test_a_slug_the_wiki_never_held_is_a_miss(api, monkeypatch):
    seen = fake_run_text(monkeypatch)
    missing = "/api/incidents/2020-01-01-nope-nope"
    assert api.get(f"{missing}/tools")[0] == 404
    status, body = api.post(f"{missing}/advisory", {"tool": "next-checks"})
    assert status == 404 and body["error"] == "no_such_incident"
    assert seen == []


def test_the_router_separates_a_missing_route_from_a_wrong_method(api):
    assert api.json("POST", TOOLS_PATH, body={})[0] == 405
    assert api.json("GET", START_PATH)[0] == 405
    assert api.json("POST", "/api/advisory", body={})[0] == 405


def test_a_body_that_asserts_an_actor_or_a_key_nobody_reads_is_refused(api):
    status, body = api.post(START_PATH, {"tool": "next-checks",
                                         "actor": "root@example.com"})
    assert status == 400 and body["error"] == "identity_is_server_side"
    status, body = api.post(START_PATH, {"tool": "next-checks", "base": "x"})
    assert status == 400 and body["error"] == "unknown_key"
    status, body = api.post(START_PATH, {"tool": "next-checks", "at": "today"})
    assert status == 400 and body["error"] == "bad_at"
    status, body = api.post(START_PATH, {"tool": ""})
    assert status == 400 and body["error"] == "bad_tool"


def files(wiki) -> list[str]:
    return sorted(str(p.relative_to(wiki)) for p in wiki.rglob("*")
                  if ".git" not in p.parts)


def test_an_advisory_run_has_no_authority_over_the_wiki(api, root,
                                                        monkeypatch):
    """`advisory.Runner` takes no lock, writes no page and reads no HEAD but
    the one the request pinned. A whole run therefore leaves the checkout
    exactly as it found it, and the only thing it wrote is its own file under
    `.state/`."""
    fake_run_text(monkeypatch)
    wiki = root / "wiki"
    before = (head(root), transaction.stray_paths(wiki), files(wiki))
    run = poll(api, started(api, "draft-note")["run_id"])
    assert run["status"] == "succeeded"
    assert (head(root), transaction.stray_paths(wiki), files(wiki)) == before
    assert (root / ".state" / advisory.RUNS_DIR
            / f"{run['run_id']}.json").is_file()
