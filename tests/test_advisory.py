"""The advisory seam: what a tool may read, what a click may spend, and what
crosses the wire.

Pure: `Tree.of` fixtures, a `Snapshot` built by hand and a fake
`harness.run_text`, so there is no server, no git, no wiki on disk and no
model. The one thing on disk is `tmp_path/.state`, because the ceiling and the
run record are files and a test that faked them would be testing its own
restatement of the rules.

The boundedness claims this module exists to protect are asserted as facts
about `pack` over every row: `SOURCES` is the only route to evidence, and a
section's kind comes from the table key.

The material rules `portal/draft.py` used to own are asserted here, over
`draft-note`, which is the row that replaced it: which digests are packed and
in what order, which are dropped, and what the prompt asks for.
"""

import inspect
import json
import re
import threading
import time
from dataclasses import replace

import pytest

from dbwiki import (advisory, harness, health, incident_action, lifecycle,
                    lock, monitoring, structured, transaction)
from dbwiki.advisory import (DEFAULT_MAX_CONCURRENT, SOURCES, TOOLS, Budget,
                             EvidencePack, Refused, Runner, RunStatus,
                             Settings, SourceKind, Target, ToolDisabled,
                             UnknownTool, build_prompt, check, cited, pack,
                             resolve, run_id_for, spend)
from dbwiki.incidents import read_incident
from dbwiki.readmodel import JournalEntry, Occurrence, Snapshot
from dbwiki.transaction import Tree
from fixtures.incident_pages import incident_page

DB = "cdb1"
SLUG = "2026-08-05-cdb1-ora-00600"
PATH = f"incidents/{SLUG}.md"
NEIGHBOUR = "incidents/2026-07-02-cdb1-earlier.md"
REV = "4c1f2d3" + "0" * 33
CODES = ("ORA-00600", "TNS-12564")
DAYS = ("2026-08-26", "2026-08-27", "2026-08-28", "2026-08-29")

#: One string nothing but a packed body may carry. Every fixture below plants
#: it in whatever it contributes to the material, so "no encoding of a run
#: carries the prompt" is one assertion over every tool.
SENTINEL = "PACKED-BODY-9f3a1c"

ANSWER = "restarted the apply process; the errors stopped"
USAGE = {"input_tokens": 4200, "output_tokens": 31, "cost_usd": 0.002}
AT = "2026-08-30T09:00:00Z"
NOW = "2026-08-30T09:00:30Z"


def action_block(at: str, summary: str) -> str:
    return (f"\n## Action {at}\n\n```yaml\nkind: mitigation\n"
            f"actor: alice\nintent: stop the bleeding\n"
            f"summary: {summary}\nstatus_after: monitoring\n```\n")


PAGE = (incident_page(DB, "ORA-00600 on cdb1", status="monitoring",
                      opened="2026-08-05T00:00:00Z",
                      body=f"The standby stopped applying redo. {SENTINEL}",
                      error_codes=CODES)
        + action_block("2026-08-06T10:00:00Z", f"restarted apply {SENTINEL}"))
INCIDENT = read_incident(PAGE, PATH)

NEIGHBOUR_PAGE = incident_page(DB, "an earlier one", status="resolved",
                               opened="2026-07-02T00:00:00Z",
                               error_codes=(CODES[0],))
NEIGHBOUR_INCIDENT = read_incident(NEIGHBOUR_PAGE, NEIGHBOUR)


def error_page(code: str) -> str:
    return f"# {code}\n\n## Meaning\n\n{code} is reported. {SENTINEL}\n"


def digest(day: str) -> str:
    return f"digests/{DB}/{day}.md"


def digest_md(day: str, *, notable: bool = True) -> str:
    text = f"# {DB} - {day}\n\n## Sources\n\n- alert: 41 events\n\n"
    if notable:
        text += (f"## Deltas (never seen before / anomalies)\n\n"
                 f"- {CODES[0]} first seen on {day} {SENTINEL}\n\n")
    return text + "### Routine (counters)\n\n- 41 ordinary lines\n"


FACTS = {"verdict": "not_met",
         "signal": {"kind": "error_absent", "code": CODES[0]},
         "window": {"start": f"{DAYS[0]}T00:00:00Z",
                    "until": f"{DAYS[-1]}T23:59:59Z"},
         "observed": [{"day": day, "digest": f"digests/{DB}/{day}.json",
                       "code": CODES[0], "count": 0} for day in DAYS],
         "contradictions": [f"the listener still logs {SENTINEL}"],
         "evaluated_at": f"{DAYS[-1]}T23:59:59Z",
         "source_revision": REV}


def tree(changes: dict | None = None) -> Tree:
    """The wiki at `REV`. A change to None takes a path out, which is how "the
    revision does not hold it" is spelled."""
    files = {PATH: PAGE, NEIGHBOUR: NEIGHBOUR_PAGE,
             **{f"errors/{c}.md": error_page(c) for c in CODES},
             **{digest(day): digest_md(day) for day in DAYS}}
    files.update(changes or {})
    return Tree.of({path: text for path, text in files.items()
                    if text is not None}, REV)


def snapshot() -> Snapshot:
    """Enough of a `Snapshot` for the three readers that join through it. Not
    `readmodel.build`, which wants git: what is under test is the join, and a
    subprocess would make it depend on how long git takes."""
    rows = tuple(
        Occurrence(code=CODES[0], day=day, db=DB,
                   note=f"seen again {SENTINEL}", evidence=digest(day))
        for day in DAYS)
    rows += (Occurrence(code=CODES[0], day="2026-07-02", db=DB, note="",
                        evidence=digest("2026-07-02")),)
    return Snapshot(
        revision=REV, built_at=NOW, inventory=frozenset(), pages={}, text={},
        links={}, backlinks={},
        incidents={SLUG: INCIDENT, NEIGHBOUR_INCIDENT.slug: NEIGHBOUR_INCIDENT},
        occurrences=rows,
        research={}, resolutions={},
        journals={DB: (JournalEntry(db=DB, day=DAYS[-1],
                                    headline=f"apply restarted {SENTINEL}",
                                    path=f"journals/{DB}/2026-08.md"),)},
        touches_by_run={}, touches_by_incident={},
        report_of_day={}, dbs=(DB,))


def target(*, with_snapshot: bool = True, facts=FACTS, changes=None,
           question: str = "") -> Target:
    return Target.incident(INCIDENT, tree=tree(changes), revision=REV,
                           snapshot=snapshot() if with_snapshot else None,
                           facts=facts, question=question)


def settings(**over) -> Settings:
    base = resolve(type("Cfg", (), {"advisory": {},
                                    "agents": {"pi": {"cheap": "gemma-3",
                                                      "strong": "qwen"}}})())
    fields = {"enabled": base.enabled, "max_concurrent": base.max_concurrent,
              "retain": base.retain, "tools": base.tools}
    budget = over.pop("budget", None)
    if budget is not None:
        fields["tools"] = {k: replace(v, budget=budget)
                           for k, v in base.tools.items()}
    fields.update(over)
    return Settings(**fields)


def runner(tmp_path, monkeypatch, *, answer=ANSWER, usage=USAGE,
           raises=None, gate=None, **over) -> Runner:
    """A runner over `tmp_path/.state` whose model call is a fake. `gate` is
    an event the fake blocks on, which is how a concurrency test holds a slot
    open without depending on how long anything takes."""
    calls: list[str] = []

    def run_text(prompt, model, timeout, provider=None, cwd=None,
                 telemetry=None):
        calls.append(prompt)
        if telemetry is not None:
            telemetry.update(adapter="pi", model=model or "gemma-3")
            if usage is not None:
                telemetry["usage"] = usage
        if gate is not None:
            gate.wait(timeout=10)
        if raises is not None:
            raise raises
        return answer

    monkeypatch.setattr(harness, "run_text", run_text)
    made = Runner(settings(**over), state_dir=tmp_path / ".state",
                  now=lambda: NOW, boot_id="boot-a")
    made.calls = calls
    return made


def wait(made: Runner, run_id: str):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = made.read(run_id)
        if run is not None and run.status in advisory.TERMINAL:
            return run
        time.sleep(0.005)
    raise AssertionError(f"{run_id} never reached a terminal status")


def ledger(made: Runner) -> list[dict]:
    path = made.state_dir / health.ADVISORY_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def write_ledger(state_dir, tool: str, at: str, **fields):
    health.append_advisory_run(state_dir, {"tool": tool, "at": at, **fields})


def test_every_source_kind_has_a_rule_and_every_rule_has_a_kind():
    assert set(SOURCES) == set(SourceKind)


def test_every_tool_id_is_its_own_registry_key():
    assert all(tool_id == spec.id for tool_id, spec in TOOLS.items())


@pytest.mark.parametrize("tool_id", sorted(TOOLS))
def test_a_pack_carries_no_kind_the_row_did_not_name(tool_id):
    """The boundedness proof: `pack` stamps the kind from the `SOURCES` key it
    is iterating, so a reader cannot contribute a kind its row never asked
    for."""
    packed = pack(TOOLS[tool_id], target())
    assert {s.kind for s in packed.sections} <= set(TOOLS[tool_id].sources)


@pytest.mark.parametrize("tool_id", sorted(TOOLS))
def test_a_pack_stays_inside_every_rules_item_and_char_caps(tool_id):
    packed = pack(TOOLS[tool_id], target())
    for kind in set(s.kind for s in packed.sections):
        rule = SOURCES[kind]
        of_kind = [s for s in packed.sections if s.kind is kind]
        assert len(of_kind) <= rule.max_items
        for section in of_kind:
            assert len(section.text) <= rule.max_chars + len("\n[truncated]")


@pytest.mark.parametrize("tool_id", sorted(TOOLS))
def test_every_instruction_formats_against_a_real_target(tool_id):
    spec = TOOLS[tool_id]
    prompt = build_prompt(spec, target(), pack(spec, target()))
    assert SLUG in prompt and DB in prompt
    assert "{" not in prompt.split("Material")[0]


def test_a_capped_section_says_it_was_cut():
    long_page = PAGE + "x" * 20000
    pinned = Target.incident(
        read_incident(long_page, PATH),
        tree=Tree.of({PATH: long_page}, REV), revision=REV)
    section = pack(TOOLS["explain-incident"], pinned).sections[0]
    assert section.truncated and section.text.endswith("[truncated]")


def test_a_derived_section_is_headed_by_its_rule_and_is_not_citable():
    packed = pack(TOOLS["summarize-evidence"], target())
    closure = [s for s in packed.sections if s.kind is SourceKind.CLOSURE]
    assert closure and closure[0].path == ""
    assert closure[0].heading == SOURCES[SourceKind.CLOSURE].heading
    assert "" not in packed.paths


def test_a_subject_the_snapshot_joins_nothing_to_says_so():
    lonely = Snapshot(revision=REV, built_at=NOW, inventory=frozenset(),
                      pages={}, text={}, links={}, backlinks={},
                      incidents={SLUG: INCIDENT}, occurrences=(),
                      research={}, resolutions={}, journals={},
                      touches_by_run={}, touches_by_incident={},
                      report_of_day={}, dbs=(DB,))
    pinned = Target.incident(INCIDENT, tree=tree(), revision=REV,
                             snapshot=lonely, facts=FACTS)
    packed = pack(TOOLS["similar-incidents"], pinned)
    none = [s for s in packed.sections if s.kind is SourceKind.NEIGHBOURS]
    assert [(s.path, s.text) for s in none] == [("", "no similar incidents "
                                                     "found")]


def test_a_neighbour_sharing_an_error_code_is_found_through_the_occurrences():
    packed = pack(TOOLS["similar-incidents"], target())
    found = [s for s in packed.sections if s.kind is SourceKind.NEIGHBOURS]
    assert [s.path for s in found] == [NEIGHBOUR]
    assert CODES[0] in found[0].text


def test_a_neighbour_is_citable_and_the_empty_answer_is_not():
    """The one join whose line is about a single page, so an answer that names
    a neighbour comes back with the link the operator wants."""
    packed = pack(TOOLS["similar-incidents"], target())
    assert NEIGHBOUR in packed.paths
    assert f"### {NEIGHBOUR}\n" in packed.text
    assert cited(f"see {NEIGHBOUR}", packed.paths) == (NEIGHBOUR,)


def test_cited_reads_the_answer_and_never_the_wiki():
    assert cited("a and c", ("a", "b", "c")) == ("a", "c")
    assert cited("", ("a",)) == ()


@pytest.mark.parametrize("tool_id", sorted(TOOLS))
def test_no_encoding_of_a_run_carries_a_packed_body(tool_id, tmp_path,
                                                    monkeypatch):
    """Every fixture plants `SENTINEL` in whatever it contributes to the
    material, so this is one assertion that the prompt never leaves the
    module: neither the manifest the wire is handed nor the run record's JSON
    may contain it."""
    spec = TOOLS[tool_id]
    packed = pack(spec, target())
    assert SENTINEL in packed.text

    assert SENTINEL not in json.dumps(
        [(str(e.kind), e.path, e.heading, e.chars, e.truncated)
         for e in packed.manifest])

    made = runner(tmp_path, monkeypatch)
    run = wait(made, made.start(spec, target(), at=AT).run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert SENTINEL not in json.dumps(run.to_dict())


def test_the_count_ceiling_denies_once_the_window_is_full(tmp_path):
    for i in range(3):
        write_ledger(tmp_path, "draft-note", f"2026-08-30T0{i}:00:00Z",
                     cost_usd=0.001)
    current = spend(tmp_path, "draft-note", window_h=24, now=NOW)
    assert current.runs == 3
    assert check(current, Budget(24, 3, None)) == "max_runs"
    assert check(current, Budget(24, 4, None)) == ""


def test_a_run_outside_the_window_or_for_another_tool_is_not_counted(tmp_path):
    write_ledger(tmp_path, "draft-note", "2026-08-01T00:00:00Z", cost_usd=1.0)
    write_ledger(tmp_path, "next-checks", "2026-08-30T08:00:00Z", cost_usd=1.0)
    write_ledger(tmp_path, "draft-note", "not a timestamp", cost_usd=1.0)
    assert spend(tmp_path, "draft-note", window_h=24, now=NOW).runs == 0


def test_a_run_the_caller_backdated_still_counts_against_the_window(
        tmp_path, monkeypatch):
    """`at` rides in on a request body. A ledger keyed on it would let anybody
    posting a date in 1999 walk straight through the count ceiling, so the
    line carries the terminal time this process minted."""
    made = runner(tmp_path, monkeypatch)
    wait(made, made.start(made.spec("draft-note"), target(),
                          at="1999-01-01T00:00:00Z").run_id)
    line = ledger(made)[0]
    assert line["at"] == NOW and line["requested_at"] == "1999-01-01T00:00:00Z"
    assert spend(made.state_dir, "draft-note", window_h=24, now=NOW).runs == 1


def test_the_ledger_line_lands_before_the_terminal_record(tmp_path,
                                                          monkeypatch):
    """A poller learns a run is over from the run file and reads the ledger
    next — the test above does exactly that — so the terminal record must be
    the worker's second write. The same order means a crash between the two
    errs on the side of a counted run, never a free one."""
    made = runner(tmp_path, monkeypatch)
    order: list[str] = []
    write, record = made._write, made._ledger

    def spy_write(run):
        if run.status in advisory.TERMINAL:
            order.append("record")
        return write(run)

    def spy_ledger(run, spec):
        order.append("ledger")
        return record(run, spec)

    monkeypatch.setattr(made, "_write", spy_write)
    monkeypatch.setattr(made, "_ledger", spy_ledger)
    wait(made, made.start(made.spec("draft-note"), target(), at=AT).run_id)
    assert order == ["ledger", "record"]


def test_a_clock_nobody_can_read_counts_every_line_rather_than_none(tmp_path):
    """Fail closed: a window that cannot be computed must not read as a window
    nothing is inside."""
    write_ledger(tmp_path, "draft-note", "2020-01-01T00:00:00Z", cost_usd=0.5)
    assert spend(tmp_path, "draft-note", window_h=24, now="whenever").runs == 1


def test_the_dollar_ceiling_denies_on_measured_spend(tmp_path):
    for i in range(2):
        write_ledger(tmp_path, "draft-note", f"2026-08-30T0{i}:00:00Z",
                     cost_usd=0.03, usage_known=True)
    current = spend(tmp_path, "draft-note", window_h=24, now=NOW)
    assert current.measured_runs == 2 and current.cost_usd == pytest.approx(0.06)
    assert check(current, Budget(24, 99, 0.06)) == "max_cost_usd"
    assert check(current, Budget(24, 99, 0.07)) == ""


def test_a_run_nobody_priced_refuses_a_dollar_ceiling_rather_than_costing_zero(
        tmp_path):
    write_ledger(tmp_path, "draft-note", "2026-08-30T08:00:00Z",
                 usage_known=True, input_tokens=10, output_tokens=2)
    current = spend(tmp_path, "draft-note", window_h=24, now=NOW)
    assert current.unmeasured_runs == 1 and current.cost_usd == 0.0
    assert check(current, Budget(24, 99, 1.0)) == "cost_unknown"
    assert check(current, Budget(24, 99, None)) == ""


def test_a_real_cost_beside_usage_known_false_is_measured_spend(tmp_path):
    """`usage_known` is `health.record_agent_run`'s token rule and says
    nothing about the cost. Keying on it would drop a priced run."""
    write_ledger(tmp_path, "draft-note", "2026-08-30T08:00:00Z",
                 usage_known=False, cost_usd=0.05)
    current = spend(tmp_path, "draft-note", window_h=24, now=NOW)
    assert current.measured_runs == 1 and current.cost_usd == pytest.approx(0.05)
    assert check(current, Budget(24, 99, 1.0)) == ""


def test_a_ceiling_refusal_records_an_addressable_run_and_spends_nothing(
        tmp_path, monkeypatch):
    made = runner(tmp_path, monkeypatch, budget=Budget(24, 0, None))
    with pytest.raises(Refused) as caught:
        made.start(made.spec("draft-note"), target(), at=AT)
    assert caught.value.reason == "max_runs"
    assert caught.value.run.status is RunStatus.REFUSED
    assert made.read(caught.value.run.run_id).status is RunStatus.REFUSED
    assert made.calls == [] and ledger(made) == []


def test_the_block_overrides_a_row_and_the_top_level_defaults_every_row():
    cfg = type("Cfg", (), {
        "advisory": {"max_concurrent": 5, "retain": 7, "window_h": 6,
                     "tools": {"draft-note": {"max_runs": 2, "tier": "strong",
                                              "enabled": False}}},
        "agents": {"pi": {"cheap": "gemma-3", "strong": "qwen"}}})()
    resolved = resolve(cfg)
    assert (resolved.max_concurrent, resolved.retain) == (5, 7)
    assert resolved.tools["draft-note"].budget == Budget(6, 2, None)
    assert resolved.tools["draft-note"].model == "qwen"
    assert resolved.tools["draft-note"].enabled is False
    assert resolved.tools["next-checks"].budget.window_h == 6
    assert resolved.tools["next-checks"].model == "gemma-3"


@pytest.mark.parametrize("block, named", [
    ({"tools": {"explain-everything": {}}}, "advisory.tools.explain-everything"),
    ({"tier": "medium"}, "advisory.tier"),
    ({"tools": {"draft-note": {"tier": "medium"}}},
     "advisory.tools.draft-note.tier"),
])
def test_a_misconfiguration_is_refused_by_the_key_that_named_it(block, named):
    cfg = type("Cfg", (), {"advisory": block, "agents": {}})()
    with pytest.raises(ValueError, match=named.replace(".", r"\.")):
        resolve(cfg)


def test_an_unknown_or_disabled_row_is_two_different_refusals(tmp_path,
                                                              monkeypatch):
    made = runner(tmp_path, monkeypatch)
    with pytest.raises(UnknownTool):
        made.spec("explain-everything")
    off = runner(tmp_path, monkeypatch, enabled=False)
    with pytest.raises(ToolDisabled):
        off.spec("draft-note")


def test_two_starts_with_the_same_at_are_one_run_one_line_and_one_call(
        tmp_path, monkeypatch):
    made = runner(tmp_path, monkeypatch)
    spec = made.spec("draft-note")
    first = made.start(spec, target(), at=AT)
    wait(made, first.run_id)
    second = made.start(spec, target(), at=AT)
    assert second.run_id == first.run_id == run_id_for("draft-note", SLUG, AT)
    assert second.status is RunStatus.SUCCEEDED
    assert len(made.calls) == 1
    assert len(ledger(made)) == 1


QUESTION = "did the standby ever catch up on the gap?"
OTHER_QUESTION = "which listener logged the connect failures?"


def test_a_row_asks_exactly_when_its_instruction_names_the_question():
    """The single definition of "this row takes a question". A test that
    listed the ids instead would be a second registry, and the one an operator
    can edit is `TOOLS`."""
    asking = {tool_id for tool_id in TOOLS if advisory.asks(TOOLS[tool_id])}
    assert asking == {"ask-incident"}
    assert {tool_id for tool_id in TOOLS
            if "{question}" in TOOLS[tool_id].instruction} == asking


def test_a_run_id_moves_for_a_question_and_stands_still_without_one():
    """The empty question is left out of the hashed string, so every id
    already on disk means what it always meant."""
    assert run_id_for("draft-note", SLUG, AT, "") == \
        run_id_for("draft-note", SLUG, AT)
    assert run_id_for("ask-incident", SLUG, AT, QUESTION) != \
        run_id_for("ask-incident", SLUG, AT, OTHER_QUESTION)
    assert run_id_for("ask-incident", SLUG, AT, QUESTION) != \
        run_id_for("ask-incident", SLUG, AT)


def test_the_question_reaches_the_prompt_as_one_more_word_of_the_target():
    asked = target(question=QUESTION)
    assert asked.words()["question"] == QUESTION
    spec = TOOLS["ask-incident"]
    head = build_prompt(spec, asked, pack(spec, asked)).split("Material")[0]
    assert QUESTION in head
    assert "{" not in head


def test_a_run_records_the_question_and_an_older_record_reads_as_none_asked():
    """`from_dict` defaults the key, so a run written before this row existed
    is still readable rather than a file the poll endpoint drops."""
    record = advisory.AdvisoryRun(
        schema_version=advisory.SCHEMA_VERSION, run_id="abc123def456",
        tool="ask-incident", target=SLUG, at=AT, question=QUESTION,
        status=RunStatus.QUEUED, boot_id="boot-a", started=NOW, finished="",
        duration_s=None, evidence_revision=REV, context=(), answer=None,
        error="")
    body = record.to_dict()
    assert body["question"] == QUESTION
    assert advisory.AdvisoryRun.from_dict(body) == record

    older = {key: value for key, value in body.items() if key != "question"}
    assert advisory.AdvisoryRun.from_dict(older).question == ""


def test_two_questions_at_one_at_are_two_runs_and_both_are_kept(
        tmp_path, monkeypatch):
    """The other half of idempotency: a re-POST of one click converges, but a
    second question is a second thing asked and must not be answered with the
    first one's answer."""
    made = runner(tmp_path, monkeypatch)
    spec = made.spec("ask-incident")
    first = made.start(spec, target(question=QUESTION), at=AT)
    wait(made, first.run_id)
    second = made.start(spec, target(question=OTHER_QUESTION), at=AT)
    wait(made, second.run_id)

    assert first.run_id != second.run_id
    assert first.question == QUESTION and second.question == OTHER_QUESTION
    assert made.read(first.run_id).question == QUESTION
    assert made.read(second.run_id).question == OTHER_QUESTION
    assert len(made.calls) == 2
    assert QUESTION in made.calls[0] and OTHER_QUESTION in made.calls[1]


def test_only_max_concurrent_starts_are_admitted_and_the_loser_is_busy(
        tmp_path, monkeypatch):
    gate = threading.Event()
    made = runner(tmp_path, monkeypatch, gate=gate,
                  max_concurrent=DEFAULT_MAX_CONCURRENT)
    spec = made.spec("draft-note")
    started, refused = [], []
    ready = threading.Barrier(DEFAULT_MAX_CONCURRENT + 1)

    def go(n):
        ready.wait(timeout=10)
        try:
            started.append(made.start(spec, target(), at=f"{AT[:-4]}{n:02d}Z"))
        except Refused as e:
            refused.append(e)

    threads = [threading.Thread(target=go, args=(n,))
               for n in range(DEFAULT_MAX_CONCURRENT + 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    gate.set()

    assert len(started) == DEFAULT_MAX_CONCURRENT
    assert [e.reason for e in refused] == ["busy"]
    assert refused[0].run.status is RunStatus.REFUSED
    assert refused[0].retry_after_s
    for run in started:
        assert wait(made, run.run_id).status is RunStatus.SUCCEEDED


def test_two_concurrent_starts_with_one_run_left_admit_exactly_one(
        tmp_path, monkeypatch):
    """The reservation, not the ledger: a queued run has no ledger line yet,
    so a spend read alone would let both through."""
    gate = threading.Event()
    made = runner(tmp_path, monkeypatch, gate=gate, budget=Budget(24, 1, None))
    spec = made.spec("draft-note")
    started, refused = [], []
    ready = threading.Barrier(2)

    def go(n):
        ready.wait(timeout=10)
        try:
            started.append(made.start(spec, target(), at=f"{AT[:-4]}{n:02d}Z"))
        except Refused as e:
            refused.append(e)

    threads = [threading.Thread(target=go, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    gate.set()

    assert len(started) == 1
    assert [e.reason for e in refused] == ["max_runs"]
    assert wait(made, started[0].run_id).status is RunStatus.SUCCEEDED


def test_the_answer_comes_back_stripped(tmp_path, monkeypatch):
    """The operator edits this text in a form control, so the leading newline
    an adapter is fond of would arrive as an empty first line."""
    made = runner(tmp_path, monkeypatch, answer=f"  {ANSWER}\n")
    run = wait(made, made.start(made.spec("draft-note"), target(),
                                at=AT).run_id)
    assert run.answer.text == ANSWER


def test_a_failed_call_is_a_recorded_run_and_not_a_raised_one(tmp_path,
                                                              monkeypatch):
    made = runner(tmp_path, monkeypatch,
                  raises=harness.HarnessError("pi exited 1: no model"))
    run = wait(made, made.start(made.spec("draft-note"), target(),
                                at=AT).run_id)
    assert run.status is RunStatus.FAILED
    assert "no model" in run.error and run.answer is None
    assert len(ledger(made)) == 1


def test_a_missing_adapter_binary_is_the_same_recorded_failure(tmp_path,
                                                              monkeypatch):
    made = runner(tmp_path, monkeypatch,
                  raises=FileNotFoundError(2, "No such file", "pi"))
    run = wait(made, made.start(made.spec("draft-note"), target(),
                                at=AT).run_id)
    assert run.status is RunStatus.FAILED and run.error


def test_the_answer_cites_only_paths_the_material_carried(tmp_path,
                                                          monkeypatch):
    made = runner(tmp_path, monkeypatch,
                  answer=f"see {PATH} and runbooks/invented.md")
    run = wait(made, made.start(made.spec("draft-note"), target(),
                                at=AT).run_id)
    assert run.answer.cites == (PATH,)


def test_a_run_left_in_flight_by_a_dead_boot_reads_interrupted(tmp_path,
                                                               monkeypatch):
    made = runner(tmp_path, monkeypatch)
    run = wait(made, made.start(made.spec("draft-note"), target(),
                                at=AT).run_id)
    stale = replace(run, status=RunStatus.RUNNING, boot_id="boot-gone")
    made._write(stale)
    assert made.read(run.run_id).status is RunStatus.INTERRUPTED


def test_sweeping_twice_rewrites_no_bytes(tmp_path, monkeypatch):
    made = runner(tmp_path, monkeypatch)
    run = wait(made, made.start(made.spec("draft-note"), target(),
                                at=AT).run_id)
    made._write(replace(run, status=RunStatus.RUNNING,
                                 boot_id="boot-gone"))
    made.sweep()
    before = {p.name: p.read_bytes() for p in made.runs_dir.glob("*.json")}
    made.sweep()
    after = {p.name: p.read_bytes() for p in made.runs_dir.glob("*.json")}
    assert before == after
    assert json.loads(before[f"{run.run_id}.json"])["status"] == "interrupted"


def test_a_sweep_keeps_the_newest_retain_run_files(tmp_path, monkeypatch):
    made = runner(tmp_path, monkeypatch, retain=2)
    spec = made.spec("draft-note")
    for n in range(4):
        made.now = lambda n=n: f"2026-08-30T0{n}:00:00Z"
        wait(made, made.start(spec, target(), at=f"{AT[:-4]}{n:02d}Z").run_id)
    made.sweep()
    kept = sorted(json.loads(p.read_text())["started"]
                  for p in made.runs_dir.glob("*.json"))
    assert kept == ["2026-08-30T02:00:00Z", "2026-08-30T03:00:00Z"]


def test_a_run_reads_no_head_takes_no_wiki_lock_and_writes_only_state(
        tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("an advisory run reached for wiki authority")

    monkeypatch.setattr(transaction, "head", forbidden)
    monkeypatch.setattr(lock, "single_flight", forbidden)
    made = runner(tmp_path, monkeypatch)
    run = wait(made, made.start(made.spec("explain-incident"), target(),
                                at=AT).run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert [p.name for p in tmp_path.iterdir()] == [".state"]


def test_the_material_carries_the_page_its_error_pages_and_the_digests():
    packed = pack(TOOLS["draft-note"], target())
    assert f"### {PATH}\n" in packed.text
    assert "The standby stopped applying redo." in packed.text
    for code in CODES:
        assert f"### errors/{code}.md\n" in packed.text
        assert f"{code} is reported" in packed.text
    assert f"### {digest(DAYS[-1])}\n" in packed.text
    assert f"{CODES[0]} first seen on {DAYS[-1]}" in packed.text


def test_the_material_drops_the_noise_digest_material_drops():
    packed = pack(TOOLS["draft-note"], target())
    assert "### Routine (counters)" not in packed.text
    assert "- alert: 41 events" not in packed.text


def test_the_material_heads_a_section_with_the_bare_path():
    """Not a `[[wikilink]]`: the summary must carry none, and the post-hoc
    scan that builds `cites` looks for paths."""
    packed = pack(TOOLS["draft-note"], target())
    for path in packed.paths:
        assert f"### {path}\n" in packed.text
        assert f"[[{path}]]" not in packed.text


def test_the_packed_paths_are_the_newest_digests_in_pack_order():
    packed = pack(TOOLS["draft-note"], target())
    assert packed.paths == (PATH, f"errors/{CODES[0]}.md",
                            f"errors/{CODES[1]}.md", digest("2026-08-29"),
                            digest("2026-08-28"), digest("2026-08-27"))
    assert digest(DAYS[0]) not in packed.text, \
        "a fourth cited digest is past the rule's max_items"


def test_a_digest_with_no_deltas_or_notable_block_is_skipped():
    """`_digest_material` returns nothing for a routine day, and a section
    with an empty body is a path the model could cite for no evidence."""
    packed = pack(TOOLS["draft-note"], target(
        changes={digest(DAYS[-1]): digest_md(DAYS[-1], notable=False)}))
    assert digest(DAYS[-1]) not in packed.paths
    assert packed.paths[3:] == (digest("2026-08-28"), digest("2026-08-27"),
                                digest("2026-08-26"))


def test_a_path_the_revision_does_not_hold_is_skipped():
    packed = pack(TOOLS["draft-note"],
                  target(changes={f"errors/{CODES[0]}.md": None}))
    assert f"errors/{CODES[0]}.md" not in packed.paths
    assert f"errors/{CODES[1]}.md" in packed.paths


def test_no_facts_packs_no_digests():
    packed = pack(TOOLS["draft-note"], target(facts=None))
    assert packed.paths == (PATH, f"errors/{CODES[0]}.md",
                            f"errors/{CODES[1]}.md")


#: `draft-note`'s whole instruction, filled for the fixtures above: every word
#: the model is asked, ahead of the material. Spelled out here rather than
#: read off `TOOLS`, because a test that formats the row it is checking would
#: agree with any rewrite of it.
DRAFT_NOTE_INSTRUCTION = (
    "You are drafting the summary line of an action record on an operations "
    f"wiki, for incident {SLUG} on {DB}: ORA-00600 on cdb1.\n"
    "\n"
    "Reply with ONE line of plain prose and NOTHING else: no preamble, no "
    "bullet, no markdown, no quotation marks.\n"
    "\n"
    "Write it the way the operator would: past tense, at most 200 characters. "
    "Say what happened and what was done about it.\n"
    "\n"
    "Rules:\n"
    "- State only what the Material below shows. Never claim a cause, a fix "
    "or a recovery it does not show.\n"
    "- Events stopping is not recovery: say the errors stopped, never that "
    "the problem is solved.\n"
    "- You may name at most one path from the Material, spelled exactly as "
    "the Material spells it, when it is the evidence for the claim.\n"
    "- No [[wikilinks]], no headings, no URLs.\n"
    "\n"
    "Material (assembled deterministically; every path in it is a page the "
    "wiki holds at this revision):")


def test_the_draft_note_prompt_is_the_one_the_workbench_drafted_summaries_with():
    """The golden string, and the only prompt in this registry that has one.

    `draft-note` is the row whose answer lands in a form control an operator
    then publishes, and its wording is what the workbench drafted summary
    lines with before this module existed. Every phrase in it was chosen
    against real answers: "ONE line and NOTHING else" because a model offered
    a bullet list, "at most one path" because it invented citations, "events
    stopping is not recovery" because it claimed a fix nobody had made.

    So a change here is a product decision and not a refactor, and this test
    is what makes somebody make it on purpose. The material half is built from
    the fixtures rather than restated, because the packing rules are asserted
    above and a second copy of them would only agree with itself.
    """
    spec = TOOLS["draft-note"]
    pinned = target()
    packed = pack(spec, pinned)
    assert build_prompt(spec, pinned, packed) == (
        f"{DRAFT_NOTE_INSTRUCTION}\n\n{packed.text}")
    assert spec.max_answer_chars == structured.MAX_SUMMARY, \
        "the length the prompt asks for is the length a summary may be"
    assert str(structured.MAX_SUMMARY) in DRAFT_NOTE_INSTRUCTION, \
        "and the prompt says the number rather than a copy of it"


VERDICTS = (monitoring.MET, monitoring.NOT_MET, monitoring.INSUFFICIENT,
            monitoring.ERROR)

CONTRADICTIONS = (f"the listener still logs {SENTINEL}",
                  "ORA-00600 fired again on 2026-08-29",
                  "the standby is 41 minutes behind")


def closure_facts(verdict: str) -> dict:
    """`FACTS` at one verdict. A met verdict carries no contradiction:
    `MonitoringFacts` cannot produce one, and `ClosureCase` states the
    invariant, so a fixture that fed it one would be testing a file the
    evaluator never writes."""
    return {**FACTS, "verdict": verdict,
            "contradictions": ([] if verdict == monitoring.MET
                               else list(CONTRADICTIONS))}


@pytest.mark.parametrize("verdict", VERDICTS)
def test_the_closure_row_narrates_every_verdict_and_is_gated_on_none(verdict):
    """The row is available wherever a case exists. Withholding the narration
    on the verdicts that carry contradictions would hide it exactly where an
    operator reads it hardest, so `pack` has to produce a case for all four."""
    spec = TOOLS["explain-closure"]
    packed = pack(spec, target(facts=closure_facts(verdict)))
    assert {section.kind for section in packed.sections} == {
        SourceKind.CLOSURE, SourceKind.DIGEST}
    assert f"verdict: {verdict}" in packed.text


def test_a_subject_with_no_facts_narrates_nothing_rather_than_failing():
    """The button is hidden only when there is no case at all, so the row has
    to survive a target the monitoring never wrote a file for."""
    spec = TOOLS["explain-closure"]
    pinned = target(facts=None)
    packed = pack(spec, pinned)
    assert packed.sections == () and packed.chars == 0
    assert "verdict:" not in build_prompt(spec, pinned, packed)


def test_every_contradiction_the_case_holds_reaches_the_prompt_verbatim():
    """The contradictions are the reason the narration exists, so they cross
    into the prompt as the evaluator wrote them rather than as a count or a
    summary a model would have to take on trust."""
    spec = TOOLS["explain-closure"]
    facts = closure_facts(monitoring.NOT_MET)
    pinned = target(facts=facts)
    case = monitoring.closure_case(facts, revision=REV)
    prompt = build_prompt(spec, pinned, pack(spec, pinned))
    assert set(CONTRADICTIONS) <= set(case.against), \
        "the fixture's contradictions are what the case argues against"
    for line in case.against:
        assert line in prompt, f"{line!r} never reached the prompt"
    assert prompt.index("arguing against closing:") < prompt.index(
        CONTRADICTIONS[0]), "and they are under the heading that names them"


def test_the_closure_instruction_asks_for_the_contradictions_first():
    instruction = TOOLS["explain-closure"].instruction
    assert "Begin with what argues against closing." in instruction
    assert "State every such line the Material holds" in instruction
    assert instruction.index("against closing") < instruction.index(
        "for closing"), "the contradictions are asked for first, not last"


#: Every name a command goes by: the portal's verbs and the CLI's kind
#: strings, which are the same five transitions spelled two ways.
COMMAND_WORDS = tuple(incident_action.VERBS) + tuple(lifecycle.KINDS.values())


@pytest.mark.parametrize("tool_id", sorted(TOOLS))
def test_no_instruction_recommends_a_transition(tool_id):
    """An advisory answer describes; `lifecycle.TRANSITIONS` decides. A row
    that asked a model which command to run would be putting a model's words
    where a table's belong, and an operator reading "resolve this" beside a
    Resolve button would be reading a recommendation as a permission."""
    instruction = TOOLS[tool_id].instruction.lower()
    named = [word for word in COMMAND_WORDS
             if re.search(rf"\b{re.escape(word)}\b", instruction)]
    assert not named, f"{tool_id} names {named}, which are commands to run"


def test_the_closure_instruction_says_the_decision_is_not_its_own():
    assert "Never say what should happen to this incident next." in \
        TOOLS["explain-closure"].instruction


#: `lifecycle.TRANSITIONS` as it stands: ten rows over five commands, keyed on
#: a status and a command type and nothing else. Spelled out rather than
#: derived, because what this pins is that the table did not grow a key — an
#: advisory verdict, a monitoring fact, an agent's opinion — that would make a
#: model's answer an input to whether an edit is legal.
LEGAL_TRANSITIONS = {
    ("open", "RecordAction"): "open",
    ("monitoring", "RecordAction"): "monitoring",
    ("resolved", "RecordAction"): "resolved",
    ("open", "StartMonitoring"): "monitoring",
    ("monitoring", "StartMonitoring"): "monitoring",
    ("monitoring", "ExtendMonitoring"): "monitoring",
    ("open", "Resolve"): "resolved",
    ("monitoring", "Resolve"): "resolved",
    ("resolved", "Reopen"): "open",
    ("open", "Merge"): "resolved",
    ("monitoring", "Merge"): "resolved",
}


def test_the_transition_table_is_the_rows_it_was_and_no_advisory_input():
    drawn = {(str(status), command.__name__): str(result)
             for (status, command), result in lifecycle.TRANSITIONS.items()}
    assert drawn == LEGAL_TRANSITIONS
    signature = inspect.signature(lifecycle.next_status)
    assert list(signature.parameters) == ["status", "command"], \
        "legality still takes a status and a command, and nothing else"
    assert signature.parameters["status"].annotation is lifecycle.Status
    assert signature.parameters["command"].annotation is lifecycle.Command
    assert signature.return_annotation is lifecycle.Status
    imports = [line for line in inspect.getsource(lifecycle).splitlines()
               if line.startswith(("import ", "from "))]
    reached = [line for line in imports
               if "advisory" in line or "monitoring" in line]
    assert not reached, \
        f"lifecycle reaches {reached}, so a model's answer could become an " \
        f"input to whether an edit is legal"


def test_the_ledger_carries_one_line_per_run_and_no_prompt_or_answer(
        tmp_path, monkeypatch):
    made = runner(tmp_path, monkeypatch)
    spec = made.spec("draft-note")
    run = wait(made, made.start(spec, target(), at=AT).run_id)
    lines = ledger(made)
    assert len(lines) == 1
    line = lines[0]
    assert line["run_id"] == run.run_id and line["status"] == "succeeded"
    assert line["tool"] == "draft-note" and line["target"] == SLUG
    assert line["sections"] == len(run.context)
    assert line["cites"] == len(run.answer.cites)
    assert line["answer_chars"] == len(ANSWER)
    assert line["cost_usd"] == USAGE["cost_usd"] and line["usage_known"] is True
    blob = json.dumps(line)
    assert SENTINEL not in blob and ANSWER not in blob


def test_an_unpriced_run_writes_no_cost_field_rather_than_a_zero(tmp_path,
                                                                 monkeypatch):
    made = runner(tmp_path, monkeypatch, usage=None)
    wait(made, made.start(made.spec("draft-note"), target(), at=AT).run_id)
    line = ledger(made)[0]
    assert "cost_usd" not in line and line["usage_known"] is False


def test_the_ledger_is_capped(tmp_path):
    """`_append_capped`'s cap, not an exact length: it trims to
    `MAX_ADVISORY_EVENTS` only once the log has drifted past cap plus 10%
    slack, so what is guaranteed is the ceiling and that a trim happened."""
    written = health.MAX_ADVISORY_EVENTS + 300
    for i in range(written):
        write_ledger(tmp_path, "draft-note", NOW, n=i)
    lines = (tmp_path / health.ADVISORY_LOG).read_text().splitlines()
    assert len(lines) < written
    assert len(lines) <= health.MAX_ADVISORY_EVENTS * 11 // 10


def test_an_evidence_pack_with_no_sections_is_an_empty_prompt_tail():
    empty = EvidencePack(())
    assert empty.text == "" and empty.paths == () and empty.chars == 0
