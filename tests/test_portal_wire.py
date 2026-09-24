"""The workbench's JSON dialect, pinned key by key.

`wire` is the only module on the server that spells a key the browser reads,
so this file is where a renamed key fails. Three things it holds beyond the
key names. The request half gets one case per stable `error` string, because
those strings are what the page branches on and a reworded message must stay
free to change. `field_json` is driven from `incident_action.VERBS` rather
than from a hand-written list, so a field added to a `lifecycle` command
arrives here as a new case instead of as silence. And the shapes that have to
survive a round trip — a window the decoder takes back, a default the page
posts back — are asserted by feeding the emitted value to the code that reads
it, not by comparing it to a second literal.

Pure: no server, no git, no clock, no wiki on disk.
"""

import dataclasses
import json
import os

import pytest

from dbwiki import (advisory, alerts, delivery, events, incident_action,
                    incidents, review)
from dbwiki.changes import Change
from dbwiki.deeplink import DeepLink, LinkState
from dbwiki.incident_action import (PRESENTATION, VERBS, LockBusy, Widget,
                                    command_fields, flag_of)
from dbwiki.incidents import (ActionProblem, ActionRecord, ErrorAbsent,
                              EventPresent, FlowResumed, Manual,
                              MonitoringWindow, Outcome, Status, read_incident)
from dbwiki.lint import Finding
from dbwiki.lock import Holder
from dbwiki.links import Link, LinkBoard, Section, Tag, empty_board
from dbwiki.monitoring import ClosureCase
from dbwiki.portal import wire
from dbwiki.portal.identity import Principal, Role
from dbwiki.readmodel import (Citation, Note, PastFixRow, Research,
                              Resolution)
from dbwiki.transaction import (Actor, BaseMoved, Commit, Committed,
                                LintBlocked, NothingToDo, Preview, Proposal,
                                TreeDirty)
from fixtures.incident_pages import incident_page

BASE = "6765a05" + "0" * 33
OTHER = "b0b0b0b" + "1" * 33
AT = "2026-08-30T14:22:10Z"
START = "2026-08-30T14:30:00Z"
UNTIL = "2026-08-31T14:30:00Z"
UPDATED = "2026-08-30T14:35:00Z"
INTENT = "stop the standby gap from growing"
SUMMARY = "restarted the apply process on cdb1"
SLUG = "2026-08-05-cdb1-ora-00600"
PATH = f"incidents/{SLUG}.md"
LINK_BASE = "https://wiki.example.com/"
RETRY_AFTER_S = 2.5

ACTOR = Actor("dba@example.com", "DB Ateam", "config")
PRINCIPAL = Principal(ACTOR, frozenset(Role))
FINDING = Finding("incidents/x.md", "digest-missing", "error",
                  "cites digests/cdb1/2026-08-28.md, which does not exist",
                  "compact that day or drop the citation")

SIGNALS = (ErrorAbsent("ORA-00600"),
           EventPresent("media recovery applied: cdb1"),
           FlowResumed("cdb1 alert log"),
           Manual("watch the gap over the night, then decide"))

TRANSPORT_VOCABULARY = {"string", "boolean", "list", "object"}

FIELDS = [(verb, field) for verb, command in VERBS.items()
          for field in command_fields(command)]


def transport_of(annotation) -> str:
    """What the page must send for one annotation, stated as branches so the
    table `wire` dispatches on is checked against something and not against
    itself."""
    if annotation is bool:
        return "boolean"
    if annotation == tuple[str, ...]:
        return "list"
    if annotation == incidents.RecoverySignal:
        return "object"
    return "string"


def control(verb: str, name: str) -> dict:
    field = next(f for f in command_fields(VERBS[verb]) if f.name == name)
    return wire.field_json(verb, field, PRESENTATION[(verb, name)])


def holder_message(pid: int) -> str:
    return (f"another dbwiki command holds the lock (pid {pid}, "
            f"portal:resolve:{SLUG}, since {AT})")


def busy(pid: int) -> LockBusy:
    """The refusal `publish` answers when `pid` holds the lock: the sentence
    and the fields it was rendered from."""
    return LockBusy(holder_message(pid),
                    Holder(pid=pid, command=f"portal:resolve:{SLUG}", since=AT))


@pytest.mark.parametrize("raw", [b"", b"   ", b"not json", b"{", b"\xff\xfe"],
                         ids=["empty", "blank", "words", "truncated", "bytes"])
def test_a_body_that_is_not_json_is_refused_before_anything_domain_shaped(raw):
    with pytest.raises(wire.BadRequest) as raised:
        wire.decode_object(raw)
    assert raised.value.error == "not_json"


@pytest.mark.parametrize("raw", [b"[]", b'"resolve"', b"3", b"null", b"true"],
                         ids=["array", "string", "number", "null", "bool"])
def test_json_that_is_not_an_object_is_its_own_refusal(raw):
    with pytest.raises(wire.BadRequest) as raised:
        wire.decode_object(raw)
    assert raised.value.error == "not_object"


def test_a_json_object_body_arrives_as_a_dict():
    assert wire.decode_object(b'{"verb": "reopen"}') == {"verb": "reopen"}


BAD_BODIES = [
    ({}, "bad_verb"),
    ({"verb": 7}, "bad_verb"),
    ({"verb": None}, "bad_verb"),
    ({"verb": "resolve", "fields": []}, "bad_fields"),
    ({"verb": "resolve", "fields": "summary=done"}, "bad_fields"),
    ({"verb": "resolve", "fields": None}, "bad_fields"),
    ({"verb": "resolve", "base": "6765a05"}, "bad_base"),
    ({"verb": "resolve", "base": "A" * 40}, "bad_base"),
    ({"verb": "resolve", "base": 6765}, "bad_base"),
    ({"verb": "resolve", "at": "2026-08-30 14:22:10"}, "bad_at"),
    ({"verb": "resolve", "at": "2026-08-30T14:22:10+02:00"}, "bad_at"),
    ({"verb": "resolve", "at": 20260830}, "bad_at"),
    ({"verb": "resolve", "confirmed": True}, "unknown_key"),
    ({"verb": "resolve", "actor": "dba@example.com"},
     "identity_is_server_side"),
]


@pytest.mark.parametrize("body, error", BAD_BODIES,
                         ids=[f"{error}-{i}" for i, (_, error)
                              in enumerate(BAD_BODIES)])
def test_a_write_body_names_the_shape_it_broke(body, error):
    with pytest.raises(wire.BadRequest) as raised:
        wire.WriteRequest.from_json(body)
    assert raised.value.error == error


def test_an_unknown_key_is_named_so_the_client_can_find_its_typo():
    with pytest.raises(wire.BadRequest) as raised:
        wire.WriteRequest.from_json({"verb": "resolve", "confirmd": True})
    assert "confirmd" in str(raised.value)


def test_an_asserted_actor_beats_every_other_unknown_key_to_the_refusal():
    with pytest.raises(wire.BadRequest) as raised:
        wire.WriteRequest.from_json({"confirmed": True, "verb": "resolve",
                                     "actor": "someone@else.example"})
    assert raised.value.error == "identity_is_server_side"


def test_a_write_body_carrying_all_four_keys_parses_into_them():
    req = wire.WriteRequest.from_json(
        {"verb": "resolve", "fields": {"summary": SUMMARY}, "base": BASE,
         "at": AT})
    assert (req.verb, req.fields, req.base, req.at) == (
        "resolve", {"summary": SUMMARY}, BASE, AT)


def test_a_body_without_fields_decodes_to_an_empty_mapping_not_to_none():
    req = wire.WriteRequest.from_json({"verb": "reopen"})
    assert req.fields == {}
    assert (req.base, req.at) == (None, None)


@pytest.mark.parametrize("verb, field", FIELDS,
                         ids=[f"{verb}-{field.name}" for verb, field in FIELDS])
def test_every_field_of_every_verb_renders_as_a_form_control(verb, field):
    pres = PRESENTATION[(verb, field.name)]
    payload = wire.field_json(verb, field, pres)
    assert set(payload) == {"name", "type", "widget", "required", "default",
                            "help", "choices", "cli_flag"}
    assert payload["name"] == field.name
    assert payload["type"] in TRANSPORT_VOCABULARY
    assert payload["type"] == transport_of(field.type)
    assert payload["widget"] == str(pres.widget)
    assert payload["help"] == pres.help
    assert payload["choices"] == list(pres.choices)
    assert payload["cli_flag"] == flag_of(verb, field.name)
    assert payload["required"] is field.required
    json.dumps(payload["default"])
    if field.required:
        assert payload["default"] is None


def test_every_widget_names_the_transport_shape_its_control_posts():
    """Keyed by the `Widget` enum, so a widget added without a transport
    row fails here rather than silently posting a string. The per-field test
    above checks each row against the annotation the decoder coerces."""
    assert set(wire.TRANSPORT_OF) == set(Widget)
    assert set(wire.TRANSPORT_OF.values()) == TRANSPORT_VOCABULARY


def test_the_resolve_flag_carries_its_negative_argv_spelling_and_a_boolean():
    payload = control("resolve", "update_error_pages")
    assert payload["cli_flag"] == "--no-error-pages"
    assert payload["type"] == "boolean"
    assert payload["default"] is True


def test_a_repeatable_field_posts_a_list_and_a_signal_posts_an_object():
    assert control("record-action", "evidence")["type"] == "list"
    assert control("monitor", "signal")["type"] == "object"


def test_an_outcome_default_arrives_as_the_string_the_choice_list_holds():
    payload = control("record-action", "outcome")
    assert payload["default"] == "pending"
    assert payload["default"] in payload["choices"]


@pytest.mark.parametrize("signal", SIGNALS,
                         ids=[type(s).__name__ for s in SIGNALS])
def test_a_window_on_the_wire_decodes_back_into_the_window_it_came_from(signal):
    window = MonitoringWindow(signal, START, UNTIL)
    payload = wire.window_json(window)
    assert payload["start"] == START
    assert payload["until"] == UNTIL
    rebuilt = incident_action.decode(
        slug=SLUG, verb="monitor",
        fields={"signal": {k: v for k, v in payload.items()
                           if k not in ("start", "until")},
                "until": UNTIL, "intent": INTENT, "summary": SUMMARY},
        actor=ACTOR, base=BASE, at=AT)
    assert MonitoringWindow(rebuilt.command.signal, START, UNTIL) == window


def test_no_window_stays_no_window():
    assert wire.window_json(None) is None


OUTCOMES = (Committed("6765a05", (PATH, "log.md"), True),
            NothingToDo(BASE),
            BaseMoved(BASE, OTHER),
            TreeDirty(("wiki/scratch.md",)),
            LintBlocked((FINDING,)),
            busy(os.getpid() + 1))


@pytest.mark.parametrize("outcome", OUTCOMES,
                         ids=[type(o).__name__ for o in OUTCOMES])
def test_every_write_response_carries_the_base_and_at_it_answers_for(outcome):
    body = wire.outcome_json(outcome, base=BASE, at=AT,
                             retry_after_s=RETRY_AFTER_S)
    assert body["base"] == BASE
    assert body["at"] == AT


def test_a_committed_outcome_reports_the_short_sha_the_paths_and_the_push():
    body = wire.outcome_json(Committed("6765a05", (PATH, "log.md"), False),
                             base=BASE, at=AT, retry_after_s=RETRY_AFTER_S)
    assert body == {"sha": "6765a05", "paths": [PATH, "log.md"],
                    "pushed": False, "base": BASE, "at": AT}


def test_nothing_to_do_says_so_rather_than_reporting_an_error():
    body = wire.outcome_json(NothingToDo(BASE), base=BASE, at=AT,
                             retry_after_s=RETRY_AFTER_S)
    assert body == {"nothing_to_do": True, "base": BASE, "at": AT}


def test_a_moved_base_names_both_shas_so_the_page_can_re_preview():
    body = wire.outcome_json(BaseMoved(BASE, OTHER), base=BASE, at=AT,
                             retry_after_s=RETRY_AFTER_S)
    assert body == {"error": "base_moved", "expected": BASE, "actual": OTHER,
                    "message": f"the wiki moved from {BASE[:12]} to "
                               f"{OTHER[:12]}; nothing was written",
                    "base": BASE, "at": AT}


def test_a_dirty_tree_names_the_paths_the_operator_has_to_settle():
    body = wire.outcome_json(TreeDirty(("wiki/scratch.md",)), base=BASE, at=AT,
                             retry_after_s=RETRY_AFTER_S)
    assert body["error"] == "tree_dirty"
    assert body["paths"] == ["wiki/scratch.md"]
    assert "nothing was written" in body["message"]


def test_blocking_lint_arrives_as_findings_the_page_can_render():
    body = wire.outcome_json(LintBlocked((FINDING,)), base=BASE, at=AT,
                             retry_after_s=RETRY_AFTER_S)
    assert body["error"] == "lint_blocked"
    assert body["findings"] == [FINDING.to_dict()]
    assert "nothing was written" in body["message"]


def test_every_refused_commit_is_the_same_shape_as_every_other_refusal():
    """A page that reads `error` and `message` off a 403 must read them off a
    409 too, or every refusal grows a branch of its own."""
    for outcome in OUTCOMES[2:]:
        body = wire.outcome_json(outcome, base=BASE, at=AT,
                                 retry_after_s=RETRY_AFTER_S)
        assert isinstance(body["error"], str) and body["error"]
        assert isinstance(body["message"], str) and body["message"]


def test_a_busy_lock_carries_the_holder_and_the_wait_the_caller_was_given():
    message = holder_message(os.getpid() + 1)
    body = wire.outcome_json(busy(os.getpid() + 1), base=BASE, at=AT,
                             retry_after_s=RETRY_AFTER_S)
    assert body == {"error": "lock_busy", "holder": message,
                    "message": message, "retry_after_s": RETRY_AFTER_S,
                    "base": BASE, "at": AT}


def test_a_busy_lock_the_portal_itself_holds_says_so_in_both_copies():
    """The page prints `holder`, so leaving that copy naming our own pid
    while `message` says otherwise would put two spellings of one refusal on
    the same screen."""
    body = wire.outcome_json(busy(os.getpid()), base=BASE,
                             at=AT, retry_after_s=RETRY_AFTER_S)
    assert body["holder"] == wire.SELF_HOLDER
    assert body["message"] == wire.SELF_HOLDER


def test_an_outcome_type_wire_has_no_row_for_raises_instead_of_answering_empty():
    with pytest.raises(TypeError):
        wire.outcome_json(object(), base=BASE, at=AT,
                          retry_after_s=RETRY_AFTER_S)


def test_a_lock_this_very_process_holds_reads_as_the_workbench_not_a_stranger():
    body = wire.outcome_json(busy(os.getpid()), base=BASE, at=AT,
                             retry_after_s=RETRY_AFTER_S)
    assert body["message"] == "another workbench request is publishing"
    assert str(os.getpid()) not in json.dumps(body)


def test_a_lock_another_pid_holds_keeps_its_diagnostic_verbatim():
    body = wire.outcome_json(busy(os.getpid() + 1), base=BASE, at=AT,
                             retry_after_s=RETRY_AFTER_S)
    assert body["message"] == holder_message(os.getpid() + 1)


def test_whose_lock_it_is_is_read_off_the_holder_never_off_the_sentence():
    """A sentence that happens to spell our pid is not our lock: only
    `held_by.pid` decides, so no wording of the sentence can change it."""
    outcome = LockBusy(holder_message(os.getpid()),
                       Holder(pid=os.getpid() + 1, command="run", since=AT))
    body = wire.outcome_json(outcome, base=BASE, at=AT,
                             retry_after_s=RETRY_AFTER_S)
    assert body["message"] == holder_message(os.getpid())


def test_an_error_body_carries_its_message_as_given():
    message = holder_message(os.getpid())
    assert wire.error_json("lock_busy", message)["message"] == message
    assert wire.error_json("tree_dirty", message)["message"] == message


def test_a_holder_the_lock_file_could_not_name_passes_through_unchanged():
    message = "another dbwiki command holds the lock"
    body = wire.outcome_json(LockBusy(message), base=BASE, at=AT,
                             retry_after_s=RETRY_AFTER_S)
    assert body["message"] == body["holder"] == message


def test_two_renders_of_one_response_are_byte_identical():
    assert wire.encode({"b": 1, "a": 2}) == wire.encode({"a": 2, "b": 1})
    assert wire.encode({"b": 1, "a": 2}) == b'{"a": 2, "b": 1}'


def test_non_ascii_travels_as_itself_rather_than_as_an_escape():
    text = "réplica secundaria al día; 日本語のログも確認済み ✅"
    raw = wire.encode({"notes": text})
    assert text in raw.decode()
    assert json.loads(raw)["notes"] == text


RECORD = ActionRecord(at=AT, kind="start-monitoring", actor=ACTOR.email,
                      intent=INTENT, summary=SUMMARY,
                      status_after=Status.MONITORING, ticket="INC-4471",
                      outcome=Outcome.SUCCEEDED,
                      rollback="stop the apply and reopen the incident",
                      window=MonitoringWindow(SIGNALS[0], START, UNTIL),
                      evidence=("digests/cdb1/2026-08-28.md",),
                      notes="the gap closed within the hour")

BROKEN_SECTION = ("\n## Action 2026-08-29T09:00:00Z\n\n```yaml\n"
                  "kind: teleport\nactor: dba@example.com\n"
                  "intent: try something\nsummary: it went sideways\n"
                  "status_after: open\n```\n")

PAGE = incidents.append_action(
    incidents.set_status(
        incident_page("cdb1", "ORA-00600 on cdb1",
                      error_codes=("ORA-00600", "TNS-12564")),
        Status.MONITORING, updated=UPDATED,
        monitoring=MonitoringWindow(SIGNALS[0], START, UNTIL)),
    RECORD) + BROKEN_SECTION

INCIDENT = read_incident(PAGE, PATH)

COMMIT = Commit(BASE, "6765a05", AT, ACTOR.email,
                "incident: start monitoring ORA-00600 on cdb1", ACTOR.email)

CLOSURE = {"verdict": "not_met",
           "digests": ["digests/cdb1/2026-08-28.md",
                       "digests/cdb1/2026-08-28.md",
                       "digests/cdb1/2026-08-29.md"]}

PRESENT = frozenset({PATH, "errors/ORA-00600.md",
                     "digests/cdb1/2026-08-28.md"})

#: ORA-00600 has a page and a `## Reference` block; TNS-12564 has neither,
#: which is the pair the research cards have to tell apart.
RESEARCH = {"ORA-00600": Research(
    code="ORA-00600", path="errors/ORA-00600.md", researched="2026-08-17",
    cause="a low-level unexpected condition",
    action="raise it with support",
    citations=(Citation("sources/oracle-docs",
                        "https://docs.oracle.com/en/error-help/db/ora-600/",
                        "2026-08-17"),),
    notes=(Note("the trace file names the failing function",
                Citation("sources/jonathan-lewis", "https://example.invalid/",
                         "2026-08-18")),
           Note("a hand-written note nobody cited", None)))}

#: Two resolutions on ORA-00600, newest day first the way the snapshot keys
#: them, so a list that reordered them would be visible.
RESOLUTIONS = {"ORA-00600": (
    Resolution(code="ORA-00600", day="2026-08-28", db="cdb1",
               incident="2026-08-28-cdb1-ora-00600",
               remediation="bounced the instance and raised an SR",
               evidence="digests/cdb1/2026-08-28.md"),
    Resolution(code="ORA-00600", day="2026-07-02", db="cdb2",
               incident="2026-07-02-cdb2-ora-00600",
               remediation="applied the one-off patch",
               evidence="2026-07-02T09:14:00Z"))}

#: Two past fixes on ORA-00600 in the order the page spells them, the second
#: a failed attempt, so the card can show what did not work as well.
PAST_FIXES = {"ORA-00600": (
    PastFixRow(code="ORA-00600", day="2026-08-28", db="cdb1",
               incident="2026-08-28-cdb1-ora-00600",
               action="bounced the instance (ticket: SR-42)",
               outcome="succeeded", held="held (14d)"),
    PastFixRow(code="ORA-00600", day="2026-08-27", db="cdb1",
               incident="2026-08-28-cdb1-ora-00600",
               action="flushed the shared pool", outcome="failed",
               held="n/a"))}

LAST_SEEN = "2026-08-28"

INCIDENT_KEYS = {"revision", "head", "slug", "path", "db", "title", "status",
                 "label", "unknown_status", "opened", "updated",
                 "error_codes", "research", "window", "actions", "problems",
                 "allowed", "closure", "history", "references", "links",
                 "strays", "touched_by",
                 "dirty", "last_seen", "body"}


def incident_view(**overrides) -> dict:
    args = {"revision": BASE, "body": PAGE, "allowed": (),
            "closure": CLOSURE, "history": (COMMIT,), "strays": (),
            "link_base": LINK_BASE, "exists": PRESENT.__contains__,
            "links": (), "touched_by": (), "research": RESEARCH,
            "resolutions": RESOLUTIONS, "past_fixes": PAST_FIXES,
            "last_seen": LAST_SEEN}
    return wire.incident_json(INCIDENT, **(args | overrides))


def test_the_incident_view_names_exactly_the_keys_the_page_reads():
    assert set(incident_view()) == INCIDENT_KEYS


def test_the_incident_view_says_nothing_about_the_advisory_tools():
    """`incident_json` says why: the panel reads `advisory_tools_json`, and a
    boolean here would be a second answer to the question that one settles."""
    assert not [key for key in incident_view() if "draft" in key
                or "advisory" in key]


def test_the_queue_response_names_the_stray_list_strays():
    body = wire.queue_json(revision=BASE, strays=(PATH,), rows=({"slug": SLUG},))
    assert body == {"revision": BASE, "strays": [PATH],
                    "incidents": [{"slug": SLUG}]}


HEAT_DAYS = ("2026-08-29", "2026-08-30", "2026-08-31")

HEAT_KEYS = {"revision", "built_at", "head", "days", "classes", "rows"}

HEAT_ROW_KEYS = {"db", "host", "counts"}


def heat_row(**overrides) -> dict:
    args = {"db": "cdb1", "host": "lab-dg1.localdomain",
            "counts": {"error": (3, None, 0), "warning": (0, None, 0),
                       "unmatched": (84, None, 12)}}
    return wire.heat_row_json(**(args | overrides))


def heat_view(**overrides) -> dict:
    args = {"prov": {"revision": BASE, "built_at": UPDATED, "head": BASE},
            "days": HEAT_DAYS, "classes": ("error", "warning", "unmatched"),
            "rows": (heat_row(),)}
    return wire.heat_json(**(args | overrides))


def test_the_heat_row_names_exactly_the_keys_the_maps_read():
    assert set(heat_row()) == HEAT_ROW_KEYS


def test_the_heat_view_names_exactly_the_keys_the_board_reads():
    assert set(heat_view()) == HEAT_KEYS


def test_a_heat_row_puts_null_on_the_wire_for_a_day_with_no_digest():
    """`fleet_row_json` refuses null for an absent journal and this builder
    insists on it, because a zero cell says the database was quiet and a null
    cell says nobody was watching. The arrays are plain lists, which is what
    `json.dumps` wants and what the page indexes against `days`."""
    row = heat_row()
    assert row["counts"] == {"error": [3, None, 0], "warning": [0, None, 0],
                             "unmatched": [84, None, 12]}
    for values in row["counts"].values():
        assert isinstance(values, list)
        assert len(values) == len(HEAT_DAYS)


def test_a_heat_row_carries_an_unknown_host_as_the_empty_string():
    """The band label, not a second kind of nothing: the row is drawn either
    way and null here would be a value the page has to branch on."""
    assert heat_row(host="")["host"] == ""


def test_the_heat_view_spells_the_axis_once_for_the_whole_board():
    view = heat_view()
    assert view["days"] == list(HEAT_DAYS)
    assert view["classes"] == ["error", "warning", "unmatched"]
    assert view["rows"] == [heat_row()]
    assert "days" not in view["rows"][0], \
        "a date repeated per row is a date two rows could disagree about"


def test_the_incident_view_reports_head_and_revision_as_one_value():
    view = incident_view()
    assert view["head"] == view["revision"] == BASE


def test_the_incident_view_carries_the_page_as_the_committed_wiki_holds_it():
    view = incident_view()
    assert view["slug"] == SLUG
    assert view["path"] == PATH
    assert view["db"] == "cdb1"
    assert view["title"] == "ORA-00600 on cdb1"
    assert view["status"] == "monitoring"
    assert view["label"] == "Monitoring"
    assert view["unknown_status"] is None
    assert view["updated"] == UPDATED
    assert view["error_codes"] == ["ORA-00600", "TNS-12564"]
    assert view["window"] == {"kind": "error_absent", "code": "ORA-00600",
                              "start": START, "until": UNTIL}
    assert view["actions"] == [wire.action_json(RECORD)]
    assert view["problems"] == [wire.problem_json(p)
                                for p in INCIDENT.actions.problems]
    assert view["history"] == [wire.commit_json(COMMIT)]
    assert view["body"] == PAGE


def test_the_incident_view_derives_dirty_from_the_strays_it_was_handed():
    assert incident_view(strays=())["dirty"] is False
    assert incident_view(strays=("wiki/scratch.md",))["dirty"] is False
    view = incident_view(strays=(PATH, "wiki/scratch.md"))
    assert view["dirty"] is True
    assert view["strays"] == [PATH, "wiki/scratch.md"]


def test_the_incident_view_links_the_page_its_errors_and_the_cited_digests():
    assert incident_view()["references"] == [
        {"path": PATH, "kind": "page", "label": "ORA-00600 on cdb1",
         "exists": True, "url": f"https://wiki.example.com/{PATH}"},
        {"path": "errors/ORA-00600.md", "kind": "error", "label": "ORA-00600",
         "exists": True,
         "url": "https://wiki.example.com/errors/ORA-00600.md"},
        {"path": "errors/TNS-12564.md", "kind": "error", "label": "TNS-12564",
         "exists": False,
         "url": "https://wiki.example.com/errors/TNS-12564.md"},
        {"path": "digests/cdb1/2026-08-28.md", "kind": "digest",
         "label": "digests/cdb1/2026-08-28.md", "exists": True,
         "url": "https://wiki.example.com/digests/cdb1/2026-08-28.md"},
        {"path": "digests/cdb1/2026-08-29.md", "kind": "digest",
         "label": "digests/cdb1/2026-08-29.md", "exists": False,
         "url": "https://wiki.example.com/digests/cdb1/2026-08-29.md"},
    ]


def test_the_incident_view_carries_one_research_row_per_code_it_names():
    assert incident_view()["research"] == [
        {"code": "ORA-00600", "path": "errors/ORA-00600.md", "exists": True,
         "researched": "2026-08-17",
         "cause": "a low-level unexpected condition",
         "action": "raise it with support",
         "citations": [{"source": "sources/oracle-docs",
                        "url": "https://docs.oracle.com/en/error-help/db/"
                               "ora-600/",
                        "accessed": "2026-08-17"}],
         "notes": [{"source": "sources/jonathan-lewis",
                    "url": "https://example.invalid/",
                    "accessed": "2026-08-18",
                    "text": "the trace file names the failing function"},
                   {"source": "", "url": "", "accessed": "",
                    "text": "a hand-written note nobody cited"}],
         "resolutions": [
             {"day": "2026-08-28", "db": "cdb1",
              "incident": "2026-08-28-cdb1-ora-00600",
              "path": "incidents/2026-08-28-cdb1-ora-00600.md",
              "remediation": "bounced the instance and raised an SR",
              "evidence": "digests/cdb1/2026-08-28.md"},
             {"day": "2026-07-02", "db": "cdb2",
              "incident": "2026-07-02-cdb2-ora-00600",
              "path": "incidents/2026-07-02-cdb2-ora-00600.md",
              "remediation": "applied the one-off patch",
              "evidence": "2026-07-02T09:14:00Z"}],
         "past_fixes": [
             {"day": "2026-08-28", "db": "cdb1",
              "incident": "2026-08-28-cdb1-ora-00600",
              "path": "incidents/2026-08-28-cdb1-ora-00600.md",
              "action": "bounced the instance (ticket: SR-42)",
              "outcome": "succeeded", "held": "held (14d)"},
             {"day": "2026-08-27", "db": "cdb1",
              "incident": "2026-08-28-cdb1-ora-00600",
              "path": "incidents/2026-08-28-cdb1-ora-00600.md",
              "action": "flushed the shared pool", "outcome": "failed",
              "held": "n/a"}]},
        {"code": "TNS-12564", "path": "errors/TNS-12564.md", "exists": False,
         "researched": "", "cause": "", "action": "", "citations": [],
         "notes": [],
         "resolutions": [],
         "past_fixes": []},
    ]


def test_an_unresearched_code_keeps_its_row_rather_than_being_dropped():
    rows = incident_view(research={})["research"]
    assert [r["code"] for r in rows] == ["ORA-00600", "TNS-12564"]
    assert all(r["researched"] == "" for r in rows)
    assert [r["exists"] for r in rows] == [True, False]


def test_a_research_row_carries_a_note_with_the_source_that_backs_it():
    """Each note names its own page, so the reader can tell which sentence
    came from which practitioner rather than reading one shared source line
    over all of them. A note nobody cited crosses as three empty strings."""
    rows = incident_view()["research"]
    assert [note["source"] for note in rows[0]["notes"]] == \
        ["sources/jonathan-lewis", ""]
    assert rows[1]["notes"] == [], \
        "a code nobody researched has no note to carry"


def test_a_research_row_keeps_the_resolutions_in_the_order_it_was_given():
    rows = incident_view()["research"]
    assert [r["day"] for r in rows[0]["resolutions"]] == ["2026-08-28",
                                                          "2026-07-02"]


def test_a_resolution_row_carries_the_case_file_path_beside_the_slug():
    """The screen links to the case file, and the slug-to-path rule stays on
    the server rather than being spelled a second time in JavaScript."""
    row = incident_view()["research"][0]["resolutions"][0]
    assert row["incident"] == "2026-08-28-cdb1-ora-00600"
    assert row["path"] == "incidents/2026-08-28-cdb1-ora-00600.md"


def test_a_code_nothing_was_ever_resolved_against_carries_an_empty_list():
    rows = incident_view(resolutions={})["research"]
    assert [r["resolutions"] for r in rows] == [[], []]


def test_a_research_row_keeps_the_past_fixes_in_page_order():
    """The writer already put them newest first, failed attempts included;
    the wire neither sorts nor filters."""
    rows = incident_view()["research"]
    assert [(f["day"], f["outcome"]) for f in rows[0]["past_fixes"]] == [
        ("2026-08-28", "succeeded"), ("2026-08-27", "failed")]


def test_a_code_with_no_past_fixes_table_carries_an_empty_list():
    rows = incident_view(past_fixes={})["research"]
    assert [r["past_fixes"] for r in rows] == [[], []]


def test_a_database_error_row_names_its_page_and_whether_it_is_researched():
    assert wire.error_row_json(code="ORA-00600", count=4,
                               last_day="2026-08-28",
                               researched="2026-08-17", resolved=2) == {
        "code": "ORA-00600", "count": 4, "last_day": "2026-08-28",
        "researched": "2026-08-17", "resolved": 2,
        "page": "errors/ORA-00600.md"}
    assert wire.error_row_json(code="TNS-12564", count=1,
                               last_day="2026-08-28",
                               researched="", resolved=0)["researched"] == ""


def test_a_database_error_row_counts_the_fixes_rather_than_listing_them():
    """The database table is scanned, not read: one number says whether
    anybody has closed this code before, and the rows live on the error
    page."""
    row = wire.error_row_json(code="ORA-00600", count=4,
                              last_day="2026-08-28",
                              researched="2026-08-17", resolved=3)
    assert row["resolved"] == 3
    assert wire.error_row_json(code="TNS-12564", count=1,
                               last_day="2026-08-28",
                               researched="", resolved=0)["resolved"] == 0


def test_an_incident_citing_no_closure_still_links_itself_and_its_errors():
    refs = incident_view(closure=None)["references"]
    assert [r["kind"] for r in refs] == ["page", "error", "error"]


def test_the_queue_row_names_exactly_the_keys_the_queue_screen_reads():
    row = wire.queue_row_json(INCIDENT, verdict="not_met", dirty=True,
                              allowed=("record-action", "extend", "resolve"),
                              last_seen=LAST_SEEN)
    assert row == {"slug": SLUG, "path": PATH, "db": "cdb1",
                   "title": "ORA-00600 on cdb1",
                   "error_codes": ["ORA-00600", "TNS-12564"],
                   "status": "monitoring",
                   "label": "Monitoring", "unknown_status": None,
                   "opened": "2026-07-01T00:00:00Z", "updated": UPDATED,
                   "verdict": "not_met", "dirty": True,
                   "allowed": ["record-action", "extend", "resolve"],
                   "last_seen": LAST_SEEN}


def test_a_page_with_an_off_vocabulary_status_reports_it_and_reads_as_open():
    inc = read_incident(incident_page("cdb1", "half-written",
                                      status="mitigating"), PATH)
    row = wire.queue_row_json(inc, verdict=None, dirty=False, allowed=(),
                              last_seen=None)
    assert row["unknown_status"] == "mitigating"
    assert row["status"] == "open"


def test_a_page_naming_no_error_code_sends_an_empty_list_and_not_null():
    """The queue pivots by code, and a page that names none is a row with
    nothing to pivot on rather than a row whose codes are unknown."""
    inc = read_incident(incident_page("cdb1", "nothing named"), PATH)
    assert wire.queue_row_json(inc, verdict=None, dirty=False, allowed=(),
                               last_seen=None)["error_codes"] == []


def test_both_screens_carry_the_day_the_codes_were_last_seen():
    row = wire.queue_row_json(INCIDENT, verdict=None, dirty=False,
                              allowed=(), last_seen=LAST_SEEN)
    assert row["last_seen"] == LAST_SEEN
    assert incident_view()["last_seen"] == LAST_SEEN, \
        "one incident reads the same on the queue and on its own page"


def test_an_incident_no_occurrence_row_joins_sends_a_null_day():
    """Null and not `""`: the page draws a dash for a day nobody can name,
    and an empty string would reach its date arithmetic as one more value to
    parse."""
    row = wire.queue_row_json(INCIDENT, verdict=None, dirty=False,
                              allowed=(), last_seen=None)
    assert row["last_seen"] is None
    assert incident_view(last_seen=None)["last_seen"] is None


def test_neither_screen_is_told_whether_the_incident_is_quiet():
    """The day is the operand. A server-computed boolean would be a second
    answer to the question the page already settles against its own today,
    the argument `provenance_json` makes about the stale badge."""
    assert not [key for key in incident_view() if "quiet" in key]
    assert not [key for key in wire.queue_row_json(
        INCIDENT, verdict=None, dirty=False, allowed=(),
        last_seen=LAST_SEEN) if "quiet" in key]


def test_a_principal_reaches_the_page_as_an_actor_and_sorted_role_strings():
    principal = Principal(ACTOR, frozenset({Role.VIEWER, Role.OPERATOR}))
    assert wire.principal_json(principal) == {
        "email": "dba@example.com", "name": "DB Ateam", "source": "config",
        "roles": ["operator", "viewer"]}


def test_an_action_record_reaches_the_page_field_for_field():
    payload = wire.action_json(RECORD)
    assert set(payload) == {f.name for f in dataclasses.fields(ActionRecord)}
    assert payload == {
        "at": AT, "kind": "start-monitoring", "actor": ACTOR.email,
        "intent": INTENT, "summary": SUMMARY, "status_after": "monitoring",
        "ticket": "INC-4471", "outcome": "succeeded",
        "rollback": "stop the apply and reopen the incident",
        "window": {"kind": "error_absent", "code": "ORA-00600",
                   "start": START, "until": UNTIL},
        "evidence": ["digests/cdb1/2026-08-28.md"],
        "notes": "the gap closed within the hour",
        "error_pages": True}


def test_an_action_with_no_window_carries_none_rather_than_an_empty_object():
    record = ActionRecord(at=AT, kind="record-action", actor=ACTOR.email,
                          intent=INTENT, summary=SUMMARY,
                          status_after=Status.OPEN)
    payload = wire.action_json(record)
    assert payload["window"] is None
    assert payload["evidence"] == []
    assert payload["outcome"] == "pending"


def test_a_malformed_action_section_reaches_the_page_as_a_problem():
    problem = ActionProblem("## Action 2026-08-29T09:00:00Z",
                            "kind: 'teleport' is not one of record-action")
    assert wire.problem_json(problem) == {
        "heading": "## Action 2026-08-29T09:00:00Z",
        "message": "kind: 'teleport' is not one of record-action"}


def test_a_lint_finding_travels_as_lint_already_renders_it():
    assert wire.finding_json(FINDING) == FINDING.to_dict()


def test_a_commit_carries_its_actor_trailer_so_the_page_can_label_it():
    assert wire.commit_json(COMMIT) == {
        "sha": BASE, "short": "6765a05", "at": AT, "author": ACTOR.email,
        "subject": "incident: start monitoring ORA-00600 on cdb1",
        "actor": ACTOR.email}


def test_a_commit_made_out_of_band_carries_an_empty_actor():
    out_of_band = Commit(OTHER, "b0b0b0b", AT, "someone@else.example",
                         "fix a typo", "")
    assert wire.commit_json(out_of_band)["actor"] == ""


def test_the_closure_case_reaches_the_page_under_for_and_against():
    facts = {"verdict": "not_met", "source_revision": OTHER}
    case = ClosureCase(verdict="not_met", evaluated_at=AT,
                       source_revision=OTHER, stale=True,
                       supporting=("2026-08-28: ORA-00600 absent",),
                       against=("2026-08-29: 3 hits of ORA-00600",),
                       digests=("digests/cdb1/2026-08-28.md",))
    assert wire.closure_json(case, facts) == {
        "verdict": "not_met", "evaluated_at": AT, "source_revision": OTHER,
        "stale": True, "for": ["2026-08-28: ORA-00600 absent"],
        "against": ["2026-08-29: 3 hits of ORA-00600"],
        "digests": ["digests/cdb1/2026-08-28.md"], "facts": facts}


def test_no_closure_case_is_no_closure_block():
    assert wire.closure_json(None, None) is None


def test_a_verb_the_caller_may_not_run_still_arrives_with_the_role_it_needs():
    fields = (control("resolve", "summary"),)
    assert wire.allowed_json("resolve", Role.CLOSER, False, fields) == {
        "verb": "resolve", "permitted": False, "role": "closer",
        "fields": [fields[0]]}


def preview_action():
    return incident_action.decode(
        slug=SLUG, verb="resolve",
        fields={"summary": SUMMARY, "evidence": ["digests/cdb1/2026-08-28.md"]},
        actor=ACTOR, base=BASE, at=AT)


def test_the_preview_is_flat_and_echoes_the_action_the_commit_must_repeat():
    action = preview_action()
    proposal = Proposal(base=BASE, actor=ACTOR,
                        message="incident: resolve ORA-00600 on cdb1",
                        files={PATH: PAGE})
    pv = Preview(base=BASE, paths=(PATH, "log.md"), diff="--- a\n+++ b\n",
                 findings=(FINDING,), strays=("wiki/scratch.md",),
                 notes=("stray: wiki/scratch.md",))
    payload = wire.preview_json(action, proposal, pv, head=OTHER,
                                status_after="resolved", principal=PRINCIPAL,
                                requires=("residual_risk",))
    assert set(payload) == {"verb", "fields", "base", "at", "head", "actor",
                            "status_after", "message", "paths", "diff",
                            "findings", "blocked", "strays", "notes",
                            "nothing_to_do", "requires", "cli"}
    assert payload["verb"] == "resolve"
    assert payload["fields"] == incident_action.encode(action)
    assert payload["base"] == BASE
    assert payload["at"] == AT
    assert payload["head"] == OTHER
    assert payload["actor"] == wire.principal_json(PRINCIPAL)
    assert payload["status_after"] == "resolved"
    assert payload["message"] == "incident: resolve ORA-00600 on cdb1"
    assert payload["paths"] == [PATH, "log.md"]
    assert payload["diff"] == "--- a\n+++ b\n"
    assert payload["findings"] == [FINDING.to_dict()]
    assert payload["blocked"] is True
    assert payload["strays"] == ["wiki/scratch.md"]
    assert payload["notes"] == ["stray: wiki/scratch.md"]
    assert payload["nothing_to_do"] is False
    assert payload["requires"] == ["residual_risk"]
    assert payload["cli"] == incident_action.retry_line(action)


def test_a_preview_that_would_write_nothing_says_so_and_does_not_block():
    action = preview_action()
    proposal = Proposal(base=BASE, actor=ACTOR, message="incident: resolve",
                        files={})
    pv = Preview(base=BASE, paths=(), diff="", findings=(), strays=())
    payload = wire.preview_json(action, proposal, pv, head=BASE,
                                status_after="resolved", principal=PRINCIPAL,
                                requires=())
    assert payload["nothing_to_do"] is True
    assert payload["blocked"] is False


LIVE_LINK = DeepLink(LinkState.AVAILABLE, "https://kibana.invalid/app/x",
                     "Open exact logs", "ORA-12543 on cdb1")
DEAD_LINK = DeepLink(LinkState.MISSING, None, "Open the exact document",
                     "The representative document none recorded",
                     "AWR-derived group; no log documents exist")


def test_a_deep_link_crosses_the_wire_as_five_keys_and_a_plain_word():
    assert wire.link_json(LIVE_LINK) == {
        "state": "available", "url": "https://kibana.invalid/app/x",
        "label": "Open exact logs", "description": "ORA-12543 on cdb1",
        "note": ""}
    assert json.dumps(wire.link_json(LIVE_LINK))


def test_a_link_that_is_not_available_carries_a_null_url_and_its_reason():
    payload = wire.link_json(DEAD_LINK)
    assert payload["state"] == "missing"
    assert payload["url"] is None
    assert payload["note"] == "AWR-derived group; no log documents exist"


def test_every_link_state_crosses_the_wire_as_its_own_word():
    words = {wire.link_json(dataclasses.replace(LIVE_LINK, state=state))
             ["state"] for state in LinkState}
    assert words == {str(state) for state in LinkState}


def test_the_page_and_run_views_both_carry_their_links_in_order():
    links = (wire.link_json(LIVE_LINK), wire.link_json(DEAD_LINK))
    page = wire.page_json(prov={"revision": BASE}, body={"path": "a.md"},
                          links=links)
    run = wire.run_json(run={}, stages=(), dbs=(), agents=(), links=links,
                        totals=wire.totals_json(events.Totals()), compare=None)
    assert page["links"] == run["links"] == list(links)


CHANGE = Change(day="2026-08-30", ts="2026-08-30T02:10:00Z",
                rule="alter_system_set", count=9,
                message="ALTER SYSTEM SET log_archive_dest_state_2=DEFER;")


def test_a_change_row_is_the_change_plus_its_database_and_digest_page():
    """The recorded shape (`Change.to_dict`) with the two keys a fleet
    timeline needs: whose change it was, and the digest that lists it."""
    row = wire.change_row_json(db="cdb1", change=CHANGE,
                               page="digests/cdb1/2026-08-30.md")
    assert row == {**CHANGE.to_dict(), "db": "cdb1",
                   "page": "digests/cdb1/2026-08-30.md"}


def test_a_page_says_no_changes_with_an_empty_list_and_never_null():
    quiet = wire.page_json(prov={"revision": BASE}, body={"path": "a.md"},
                           links=())
    assert quiet["changes"] == []
    row = wire.change_row_json(db="cdb1", change=CHANGE, page="")
    busy = wire.page_json(prov={"revision": BASE}, body={"path": "a.md"},
                          links=(), changes=(row,))
    assert busy["changes"] == [row]


def test_the_incident_view_carries_its_links_beside_its_references():
    view = incident_view(links=(wire.link_json(LIVE_LINK),))
    assert view["links"] == [wire.link_json(LIVE_LINK)]
    assert view["references"], "links do not replace the wiki references"


TOTALS_KEYS = {"stages", "rolled_back", "tokens", "token_stages", "seconds",
               "timed_stages", "cost_usd", "priced_stages", "unpriced_stages"}
DAY_KEYS = {"day", "runs", "failed", "totals"}
GROUP_KEYS = {"fingerprint", "category", "db", "count", "days", "first_seen",
              "last_seen", "commands", "sample_error", "alert"}
OPEN_ALERT_KEYS = {"first_seen", "last_seen", "count"}
RUNS_KEYS = {"generated_at", "coverage", "freshness", "runs", "backlog",
             "pending", "trend", "failures"}
RUN_KEYS = {"run", "stages", "dbs", "agents", "links", "totals", "compare"}

MEASURED = events.Totals(stages=89, rolled_back=12, tokens=455298,
                         token_stages=84, seconds=9092.72, timed_stages=89,
                         cost_usd=0.0, priced_stages=84, unpriced_stages=5)

RUN_HEAD = wire.run_head_json(
    run_id="9f2c1a0b7de4", command="run", started="2026-08-29T06:00:00Z",
    finished="2026-08-29T06:01:02Z", outcome="ok", error_category="",
    dbs_ingested=3, dbs_skipped=11)

OPEN = alerts.OpenAlert(fingerprint="0bf83a578ab2",
                        first_seen="2026-08-29T18:16:16Z",
                        last_seen="2026-09-01T21:00:50Z", count=43)

GROUP = alerts.Recurrence(
    fingerprint=OPEN.fingerprint, category="harness_error", db="cdb1",
    count=23, days=7, first_seen="2026-08-10T04:15:11Z",
    last_seen=OPEN.last_seen, commands=("retry", "run"),
    sample_error="pi produced no answer — provider unreachable", alert=OPEN)


def test_a_days_work_crosses_as_one_measure_vector_under_a_date():
    """The same nine keys the run screen reads, so the trend and one run
    cannot disagree about what the loop cost."""
    day = wire.day_json(events.Day(day="2026-08-29", runs=19, failed=7,
                                   totals=MEASURED))
    assert set(day) == DAY_KEYS
    assert set(day["totals"]) == TOTALS_KEYS
    assert day["totals"] == wire.totals_json(MEASURED)
    assert day["totals"]["priced_stages"] == 84
    assert day["totals"]["unpriced_stages"] == 5, \
        "the split is carried, never left to be subtracted"


def test_a_failure_group_carries_the_open_alert_beside_its_own_count():
    """Two counts that mean different things: failures the logs hold, and
    assessments that saw the finding."""
    row = wire.failure_group_json(GROUP)
    assert set(row) == GROUP_KEYS
    assert set(row["alert"]) == OPEN_ALERT_KEYS
    assert (row["count"], row["alert"]["count"]) == (23, 43)
    assert "fingerprint" not in row["alert"], \
        "the group already spells the identity both share"
    assert row["commands"] == ["retry", "run"]


def test_a_group_no_alert_is_open_about_carries_no_row_of_zeros():
    assert wire.open_alert_json(None) is None
    assert wire.failure_group_json(
        dataclasses.replace(GROUP, alert=None))["alert"] is None


def test_the_two_run_envelopes_grew_and_still_claim_no_revision():
    body = wire.runs_json(generated_at=AT, coverage=(), freshness=(), rows=(),
                          backlog=(), pending=(),
                          trend=(wire.day_json(events.Day("2026-08-29", 1, 0,
                                                          MEASURED)),),
                          failures=(wire.failure_group_json(GROUP),))
    one = wire.run_json(run={}, stages=(), dbs=(), agents=(), links=(),
                        totals=wire.totals_json(MEASURED),
                        compare=wire.compare_json(
                            head=RUN_HEAD, totals=wire.totals_json(MEASURED)))
    assert set(body) == RUNS_KEYS and "revision" not in body
    assert set(one) == RUN_KEYS and "revision" not in one
    assert set(one["compare"]) == set(RUN_HEAD) | {"totals"}
    assert one["compare"]["run_id"] == RUN_HEAD["run_id"], \
        "the head is the head builder's own output"


def test_no_loop_view_emits_a_difference_no_adapter_reported():
    """Subtracting two sums with different measured splits produces a number
    nobody measured. The rule is that the wire carries both columns; this is
    that rule as a shape."""
    drawn = [wire.totals_json(MEASURED),
             wire.day_json(events.Day("2026-08-29", 1, 0, MEASURED)),
             wire.failure_group_json(GROUP), wire.open_alert_json(OPEN),
             wire.compare_json(head=RUN_HEAD,
                               totals=wire.totals_json(MEASURED))]
    spelled = set()
    for payload in drawn:
        spelled |= _spelled(payload)
    assert spelled
    assert not {key for key in spelled
                if "delta" in key or "change" in key or "diff" in key}


def _spelled(value) -> set:
    """Every key a payload names, however deeply nested."""
    if isinstance(value, dict):
        return set(value) | {key for item in value.values()
                             for key in _spelled(item)}
    if isinstance(value, list):
        return {key for item in value for key in _spelled(item)}
    return set()


def test_a_stages_spend_names_what_was_measured_in_both_currencies():
    """`known` answers for the tokens and `cost_known` for the price, so a
    line that reported one and not the other crosses as itself."""
    assert wire.usage_json(events.Usage(4820, 63, 0.0021, True, True)) == {
        "input_tokens": 4820, "output_tokens": 63, "cost_usd": 0.0021,
        "known": True, "cost_known": True}
    tokens_only = wire.usage_json(events.Usage(4820, 63, 0.0, True, False))
    assert tokens_only["known"] is True
    assert tokens_only["cost_known"] is False
    assert wire.usage_json(None) is None


#: A review file as `review.run` publishes one, built out of the dataclasses
#: that write it rather than hand-typed, so a renamed field in `review.py`
#: reaches these tests as a failure instead of as a fixture nobody updated.
REVIEW_ID = "2026-W36"
FINGERPRINT = review.fingerprint("stale_open", SLUG, "cdb1", "stale_open")
OTHER_FINGERPRINT = review.fingerprint("cross_db", "ORA-00600", "-", "multi_db")
GENERATED = "2026-09-07T10:00:00Z"

REVIEW_FINDING = review.Finding(
    fingerprint=FINGERPRINT, kind="stale_open", code="stale_open", slug=SLUG,
    db="cdb1", title="ORA-00600 on cdb1", path=PATH, severity="high",
    reasons=({"code": "stale_open",
              "evidence": {"opened": "2026-08-05", "updated": "2026-08-06",
                           "age_days": 32.0}},),
    evidence_hash="0123456789ab", movement="carried",
    explanation=f"{SLUG} on cdb1: open and untouched (stale_open, high).")

REVIEW_SELECTION = review.Selection(
    schema_version=review.REVIEW_SCHEMA_VERSION, review_id=REVIEW_ID,
    generated_at=GENERATED, source_revision=BASE,
    window={"from": "2026-08-10T10:00:00Z", "to": GENERATED, "days": 28},
    findings=(REVIEW_FINDING,),
    changes={name: () for name in review.CHANGE_BUCKETS},
    counts={"selected": 1, "high": 1, "normal": 0, "shadowed": 0,
            **{name: 0 for name in review.CHANGE_BUCKETS}},
    explanation="2026-W36: 1 selected (1 high, 0 normal) over 28 days.")

REVIEW_SYNTHESIS = review.Synthesis(
    summary="one incident has been open and untouched for a month",
    themes=({"title": "nobody has acted", "detail": "the page has no record "
             "since 2026-08-06", "evidence_refs": (PATH,)},),
    evidence_refs=(PATH,), model_tier="cheap")


#: Built through `EvidencePack` rather than typed out, so the manifest's key
#: set is `review.py`'s and the completeness pin below reads the real one.
REVIEW_PACK = review.EvidencePack((
    review.PackSection(kind="summary", heading="the selection", path="",
                       text="1 selected", truncated=False),))

#: Built through `Attempt` for `REVIEW_PACK`'s reason, so the key set the
#: completeness pin reads is `delivery.py`'s own. `detail` is populated
#: because the wire dropping it is what one of the tests below asserts.
REVIEW_ATTEMPT = delivery.Attempt(
    delivery=delivery.Delivery(
        key=delivery.delivery_key(REVIEW_ID, "ops"), review_id=REVIEW_ID,
        channel=str(delivery.Channel.EMAIL), recipient="ops",
        address="ops@example.com",
        content_class=delivery.DEFAULT_CONTENT_CLASS),
    status=delivery.FAILED, at=GENERATED, error="timeout",
    detail="relay.example.com said nothing for 10s")


def review_file(**overrides) -> dict:
    return {**REVIEW_SELECTION.to_dict(),
            "synthesis": REVIEW_SYNTHESIS.to_dict(),
            "synthesis_error": "",
            "pack_manifest": list(REVIEW_PACK.manifest),
            "deliveries": [REVIEW_ATTEMPT.to_dict()],
            **overrides}


ACKS = {"schema_version": review.REVIEW_SCHEMA_VERSION,
        "items": {FINGERPRINT: {"acknowledged_at": "2026-09-07T11:00:00Z",
                                "suppressed_until": None,
                                "actor": "dba@example.com",
                                "updated_at": "2026-09-07T11:00:00Z"}}}

INBOX_KEYS_SEEN = {"generated_at", "reviews"}
INBOX_ROW_KEYS = {"review_id", "generated_at", "source_revision", "counts",
                  "synthesis_error", "synthesized"}
REVIEW_KEYS_SEEN = {"review_id", "generated_at", "source_revision", "window",
                    "counts", "changes", "explanation", "synthesis_error",
                    "findings", "synthesis", "pack_manifest", "deliveries"}
REVIEW_FINDING_KEYS = {"fingerprint", "kind", "code", "slug", "db", "title",
                       "path", "severity", "movement", "explanation",
                       "reasons", "ack"}
SYNTHESIS_KEYS = {"summary", "themes", "evidence_refs", "model_tier"}
THEME_KEYS = {"title", "detail", "evidence_refs"}
ACK_KEYS = {"acknowledged_at", "suppressed_until", "actor"}
DELIVERY_KEYS = {"key", "channel", "recipient", "content_class", "status",
                 "at", "error"}


def test_the_inbox_names_exactly_the_keys_the_list_screen_reads():
    row = wire.inbox_row_json(review_file())
    body = wire.inbox_json(generated_at=GENERATED, rows=(row,))
    assert set(body) == INBOX_KEYS_SEEN
    assert set(row) == INBOX_ROW_KEYS
    assert body["reviews"] == [row]


def test_the_review_names_exactly_the_keys_the_week_screen_reads():
    body = wire.review_json(review_file(), acks=ACKS)
    assert set(body) == REVIEW_KEYS_SEEN
    assert set(body["findings"][0]) == REVIEW_FINDING_KEYS
    assert set(body["synthesis"]) == SYNTHESIS_KEYS
    assert set(body["synthesis"]["themes"][0]) == THEME_KEYS
    assert set(body["findings"][0]["ack"]) == ACK_KEYS
    assert set(body["findings"][0]["reasons"][0]) == {"code", "evidence"}
    assert set(body["deliveries"][0]) == DELIVERY_KEYS


def test_a_delivery_row_reaches_the_page_without_the_relays_own_words():
    """`detail` is raw text from outside the deployment. The page draws the
    `health.categorize` category, so the text has no reason to cross."""
    row = wire.review_json(review_file(), acks=ACKS)["deliveries"][0]
    assert "detail" not in row
    assert REVIEW_ATTEMPT.detail not in json.dumps(row)
    assert row["status"] == "failed" and row["error"] == "timeout"
    assert row["key"] == delivery.delivery_key(REVIEW_ID, "ops")
    assert row["recipient"] == "ops" and row["channel"] == "email"
    assert row["content_class"] == delivery.DEFAULT_CONTENT_CLASS
    assert row["at"] == GENERATED


def test_neither_review_envelope_claims_a_wiki_revision():
    """Both read `.state/review/` and not the wiki, the `runs_json` rule. The
    review names `source_revision`, the revision its deterministic evaluator
    judged at, which is `closure_json`'s word and not `advisory`'s."""
    body = wire.review_json(review_file(), acks=ACKS)
    inbox = wire.inbox_json(generated_at=GENERATED,
                            rows=(wire.inbox_row_json(review_file()),))
    for envelope in (body, inbox, inbox["reviews"][0], body["findings"][0]):
        assert "revision" not in envelope
        assert "evidence_revision" not in envelope
        assert "head" not in envelope
    assert body["source_revision"] == inbox["reviews"][0]["source_revision"] \
        == BASE


def test_the_acks_are_merged_at_read_and_an_untouched_finding_carries_none():
    body = wire.review_json(review_file(), acks=ACKS)
    assert body["findings"][0]["ack"] == {
        "acknowledged_at": "2026-09-07T11:00:00Z", "suppressed_until": None,
        "actor": "dba@example.com"}
    empty = wire.review_json(review_file(), acks={"items": {}})
    assert empty["findings"][0]["ack"] == {
        "acknowledged_at": None, "suppressed_until": None, "actor": ""}


def test_an_ack_carries_no_key_the_ack_file_keeps_for_itself():
    """`updated_at` is `review.acknowledge`'s own bookkeeping and never
    reaches the screen; the two timestamps the operator reads do."""
    item = ACKS["items"][FINGERPRINT]
    assert set(wire.ack_json(item)) == ACK_KEYS
    assert "updated_at" not in wire.ack_json(item)
    assert set(wire.inbox_act_json(fingerprint=FINGERPRINT,
                                   ack=wire.ack_json(item))) \
        == {"fingerprint", "ack"}


def test_a_review_with_no_covering_note_still_carries_its_findings():
    body = wire.review_json(
        review_file(synthesis=None, synthesis_error="timeout"), acks=ACKS)
    assert body["synthesis"] is None
    assert body["synthesis_error"] == "timeout"
    assert len(body["findings"]) == 1, \
        "the model's silence is a fact about the model, not a gap in the week"
    assert wire.inbox_row_json(review_file(synthesis=None))["synthesized"] \
        is False


#: Keys a review file must never leak, at every level a `_REVIEW_KEYS`
#: sub-table reads. `events`' own poisoned-key test's list, because these are
#: the names a prompt, a model answer or a captured stream arrives under.
POISON = ("prompt", "stdout", "message", "text", "body", "input", "output")


def _poisoned(value):
    return {**value, **{key: f"SECRET-{key}" for key in POISON}}


def test_no_key_the_table_does_not_name_reaches_a_review_envelope():
    """The whole point of `_REVIEW_KEYS`: a field a future `review.py` starts
    writing has no path into an envelope, because nothing here copies a review
    file's mapping.

    Every level a sub-table reads is poisoned except one. A reason's
    `evidence` is the detectors' own key namespace, so allowlisting its names
    would be a second copy of `review.DETECTORS`; its shape is what is
    allowlisted, and the test below is where that claim is made."""
    document = _poisoned(review_file())
    document["findings"] = [_poisoned({
        **REVIEW_FINDING.to_dict(),
        "reasons": [_poisoned({"code": "stale_open",
                               "evidence": {"age_days": 32.0}})]})]
    document["synthesis"] = _poisoned({
        **REVIEW_SYNTHESIS.to_dict(),
        "themes": [_poisoned(dict(REVIEW_SYNTHESIS.themes[0],
                                  evidence_refs=[PATH]))]})
    document["pack_manifest"] = [_poisoned(document["pack_manifest"][0])]
    document["deliveries"] = [_poisoned(document["deliveries"][0])]
    acks = {"items": {FINGERPRINT: _poisoned(ACKS["items"][FINGERPRINT])}}

    payloads = [json.dumps(wire.review_json(document, acks=acks)),
                json.dumps(wire.inbox_json(
                    generated_at=GENERATED,
                    rows=(wire.inbox_row_json(document),)))]
    for payload in payloads:
        leaked = sorted(key for key in POISON if f"SECRET-{key}" in payload)
        assert not leaked, f"{leaked} crossed the wire out of a review file"


def test_a_reason_evidence_is_allowlisted_by_shape_and_capped():
    """The detectors write the key names, so the shape is what is
    allowlisted: scalars and flat lists of scalars, nothing nested, and no
    more than `MAX_EVIDENCE_KEYS` of them."""
    evidence = {"days": ["2026-08-30", "2026-08-31"], "count": 4,
                "clean": True, "nested": {"prompt": "SECRET"},
                "deep": [["SECRET"]]}
    document = review_file(findings=[{
        **REVIEW_FINDING.to_dict(),
        "reasons": [{"code": "repeat_occurrences", "evidence": evidence}]}])
    kept = wire.review_json(document, acks={})["findings"][0]["reasons"][0][
        "evidence"]
    assert "SECRET" not in json.dumps(kept)
    assert kept.get("deep") == [], "a list of lists keeps none of its rows"
    assert "nested" not in kept, "and a mapping is not a value at all"
    assert kept["count"] == 4 and kept["clean"] is True
    assert kept["days"] == ["2026-08-30", "2026-08-31"]

    many = {f"k{i}": i for i in range(wire.MAX_EVIDENCE_KEYS * 2)}
    document = review_file(findings=[{
        **REVIEW_FINDING.to_dict(),
        "reasons": [{"code": "repeat_occurrences", "evidence": many}]}])
    kept = wire.review_json(document, acks={})["findings"][0]["reasons"][0][
        "evidence"]
    assert len(kept) == wire.MAX_EVIDENCE_KEYS

    long = {"note": "x" * (wire.MAX_EVIDENCE_CHARS * 2),
            "rows": list(range(wire.MAX_EVIDENCE_ITEMS * 2))}
    document = review_file(findings=[{
        **REVIEW_FINDING.to_dict(),
        "reasons": [{"code": "multi_db", "evidence": long}]}])
    bounded = wire.review_json(document, acks={})["findings"][0]["reasons"][0][
        "evidence"]
    assert len(bounded["note"]) == wire.MAX_EVIDENCE_CHARS
    assert len(bounded["rows"]) == wire.MAX_EVIDENCE_ITEMS


def test_every_key_review_py_writes_is_a_decision_somebody_made():
    """A new field in `review.py` is either a row in a `_REVIEW_KEYS`
    sub-table or a line in that table's `_REVIEW_DROPPED` entry with a
    reason. Neither is not an option: a field that silently never reaches the
    screen is the failure this pins.

    Every shape the file carries is here, including the four keys `review.run`
    adds around the selection and the two `review.py` writes that no
    dataclass names, `_empty_item` and `EvidencePack.manifest`."""
    written = {
        "review": set(REVIEW_SELECTION.to_dict())
                  | {"synthesis", "synthesis_error", "pack_manifest",
                     "deliveries"},
        "finding": set(REVIEW_FINDING.to_dict()),
        "synthesis": set(REVIEW_SYNTHESIS.to_dict()),
        "theme": set(REVIEW_SYNTHESIS.themes[0]),
        "ack": set(review._empty_item()),
        "pack_manifest": set(REVIEW_PACK.manifest[0]),
        "delivery": set(REVIEW_ATTEMPT.to_dict()),
    }
    assert set(written) | {"reason"} == set(wire._REVIEW_KEYS), \
        "every sub-table is checked against what review.py writes into it"
    for table, keys in written.items():
        classified = (set(wire._REVIEW_KEYS[table])
                      | set(wire._REVIEW_DROPPED[table]))
        unclassified = sorted(keys - classified)
        assert not unclassified, \
            f"review.py writes {unclassified}, which the {table} table " \
            f"neither carries nor drops"
        for name, why in wire._REVIEW_DROPPED[table].items():
            assert why, f"{table}.{name} is dropped with no reason recorded"
            assert name not in wire._REVIEW_KEYS[table], \
                f"{table}.{name} is both carried and dropped"


def test_a_key_dropped_for_one_shape_is_not_excused_on_every_other():
    """`_REVIEW_DROPPED` is keyed by sub-table, so a finding dropping
    `evidence_hash` says nothing about a synthesis that ever grows one."""
    assert "evidence_hash" in wire._REVIEW_DROPPED["finding"]
    assert "evidence_hash" not in wire._REVIEW_DROPPED["synthesis"]
    assert set(wire._REVIEW_DROPPED) == set(wire._REVIEW_KEYS), \
        "every sub-table has a drop list, empty where it drops nothing"


def test_the_counts_and_the_change_buckets_are_allowlisted_by_name():
    """The one review vocabulary that is closed, unlike a reason's evidence:
    `review.select` writes these names and no others, so a name outside them
    is a key the browser would read that no table ever named."""
    document = review_file(
        counts={**REVIEW_SELECTION.counts, "prompt": 3, "leaked": 1},
        changes={**{name: [] for name in review.CHANGE_BUCKETS},
                 "prompt": ["SECRET"]})
    body = wire.review_json(document, acks={})
    assert set(body["counts"]) <= set(wire.COUNT_NAMES)
    assert set(body["changes"]) == set(review.CHANGE_BUCKETS)
    assert "SECRET" not in json.dumps(body)
    assert set(wire.COUNT_NAMES) >= set(REVIEW_SELECTION.counts), \
        "every count review.select writes has a name on the wire"


def test_the_wire_allowlist_is_review_pys_own_vocabulary():
    """Pinned rather than respelled: a count added in `review.select` reaches
    the page without a matching edit here."""
    assert wire.COUNT_NAMES is review.COUNT_NAMES


def test_a_fingerprint_list_is_bounded_the_way_the_evidence_is():
    """These are fingerprints and wiki paths, and an unbounded list of
    unbounded strings under a key the browser reads is the shape a prompt
    would arrive in."""
    document = review_file(changes={
        **{name: [] for name in review.CHANGE_BUCKETS},
        "new": ["x" * (wire.MAX_EVIDENCE_CHARS * 2)]
               * (wire.MAX_EVIDENCE_ITEMS * 2)})
    kept = wire.review_json(document, acks={})["changes"]["new"]
    assert len(kept) == wire.MAX_EVIDENCE_ITEMS
    assert len(kept[0]) == wire.MAX_EVIDENCE_CHARS


def test_a_number_no_browser_can_parse_never_reaches_the_page():
    """`json.dumps` writes a NaN as a bare word, which is not JSON and which
    the page's `JSON.parse` refuses: one unusable number in one reason would
    cost the whole response."""
    document = review_file(findings=[{
        **REVIEW_FINDING.to_dict(),
        "reasons": [{"code": "occurrences_up",
                     "evidence": {"increase": float("nan"),
                                  "ratio": float("inf"), "recent": 4}}]}])
    kept = wire.review_json(document, acks={})["findings"][0]["reasons"][0][
        "evidence"]
    assert kept == {"recent": 4}
    assert json.loads(json.dumps(kept)) == {"recent": 4}


def test_the_fingerprints_of_a_review_are_read_where_the_key_is_spelled():
    assert wire.review_fingerprints(review_file()) == {FINGERPRINT}
    assert wire.review_fingerprints({}) == frozenset()
    assert wire.review_fingerprints({"findings": ["not a row", None]}) \
        == {""}


def test_the_bands_and_movements_review_selects_are_the_words_on_the_wire():
    for severity in review.BANDS:
        row = wire.review_finding_json(
            {**REVIEW_FINDING.to_dict(), "severity": severity}, None)
        assert row["severity"] == severity
    for movement in review.MOVEMENTS:
        row = wire.review_finding_json(
            {**REVIEW_FINDING.to_dict(), "movement": movement}, None)
        assert row["movement"] == movement


QUESTION = "did the standby ever catch up on the gap?"


def test_an_advisory_body_without_a_question_asks_nothing_rather_than_none():
    parsed = wire.ToolRequest.from_json({"tool": "explain-incident", "at": AT})
    assert (parsed.tool, parsed.at, parsed.question) == \
        ("explain-incident", AT, "")


def test_an_advisory_body_carrying_a_question_parses_into_it():
    parsed = wire.ToolRequest.from_json({"tool": "ask-incident", "at": AT,
                                         "question": QUESTION})
    assert parsed.question == QUESTION
    assert wire.ToolRequest.from_json(
        {"tool": "ask-incident",
         "question": "q" * wire.MAX_QUESTION}).question \
        == "q" * wire.MAX_QUESTION


def test_an_advisory_body_names_the_shape_its_question_broke():
    good = {"tool": "ask-incident", "at": AT, "question": QUESTION}
    cases = [
        ({**good, "question": ""}, "bad_question"),
        ({**good, "question": 7}, "bad_question"),
        ({**good, "question": None}, "bad_question"),
        ({**good, "question": ["why"]}, "bad_question"),
        ({**good, "question": "q" * (wire.MAX_QUESTION + 1)}, "bad_question"),
        ({**good, "base": BASE}, "unknown_key"),
        ({**good, "tool": ""}, "bad_tool"),
        ({**good, "at": "yesterday"}, "bad_at"),
        ({**good, "actor": "root@example.com"}, "identity_is_server_side"),
    ]
    for body, word in cases:
        with pytest.raises(wire.BadRequest) as raised:
            wire.ToolRequest.from_json(body)
        assert raised.value.error == word, f"{body} should be {word}"


def advisory_run(**over) -> advisory.AdvisoryRun:
    fields = {"schema_version": advisory.SCHEMA_VERSION,
              "run_id": "abc123def456", "tool": "ask-incident",
              "target": SLUG, "at": AT, "question": QUESTION,
              "status": advisory.RunStatus.QUEUED, "boot_id": "boot-a",
              "started": AT, "finished": "", "duration_s": None,
              "evidence_revision": BASE, "context": (), "answer": None,
              "error": ""}
    fields.update(over)
    return advisory.AdvisoryRun(**fields)


def advisory_row(tool_id: str) -> dict:
    return wire.advisory_tool_json(advisory.ToolStatus(
        spec=advisory.TOOLS[tool_id], packed=advisory.EvidencePack(()),
        spent=advisory.Spend(runs=0, measured_runs=0, cost_usd=0.0),
        reason=""))


def test_an_advisory_run_reaches_the_page_with_the_question_it_answered():
    """The page threads answers under the question that produced them, and
    the question is half the run's identity anyway."""
    assert wire.advisory_run_json(advisory_run())["question"] == QUESTION
    assert wire.advisory_run_json(advisory_run(question=""))["question"] == ""


def test_the_manifest_says_which_rows_draw_a_question_box():
    assert advisory_row("ask-incident")["asks"] is True
    assert advisory_row("explain-incident")["asks"] is False


def test_an_inbox_body_names_its_refusals_one_word_at_a_time():
    good = {"fingerprint": FINGERPRINT, "action": "suppress", "days": 7,
            "at": AT}
    parsed = wire.InboxRequest.from_json(good)
    assert (parsed.fingerprint, parsed.action, parsed.days, parsed.at) \
        == (FINGERPRINT, "suppress", 7, AT)
    assert wire.InboxRequest.from_json(
        {"fingerprint": FINGERPRINT, "action": "acknowledge"}).days is None

    cases = [
        ({**good, "actor": "root@example.com"}, "identity_is_server_side"),
        ({**good, "base": BASE}, "unknown_key"),
        ({**good, "fingerprint": ""}, "bad_fingerprint"),
        ({**good, "fingerprint": 7}, "bad_fingerprint"),
        ({**good, "action": "delete"}, "bad_action"),
        ({"fingerprint": FINGERPRINT}, "bad_action"),
        ({"fingerprint": FINGERPRINT, "action": "acknowledge", "days": 7},
         "bad_days"),
        ({"fingerprint": FINGERPRINT, "action": "suppress"}, "bad_days"),
        ({**good, "days": 0}, "bad_days"),
        ({**good, "days": True}, "bad_days"),
        ({**good, "days": "7"}, "bad_days"),
        ({**good, "days": -3}, "bad_days"),
        # issue 11: both reached `review.suppress` and answered 500 there,
        # one overflowing the calendar, one with no such day to count from
        ({**good, "days": 10 ** 9}, "bad_days"),
        ({**good, "days": wire.MAX_SUPPRESS_DAYS + 1}, "bad_days"),
        ({**good, "at": "yesterday"}, "bad_at"),
        ({**good, "at": "2026-02-30T10:00:00Z"}, "bad_at"),
        ({**good, "at": "2026-09-08T25:00:00Z"}, "bad_at"),
    ]
    for body, word in cases:
        with pytest.raises(wire.BadRequest) as raised:
            wire.InboxRequest.from_json(body)
        assert raised.value.error == word, f"{body} should be {word}"


def test_the_longest_suppression_the_wire_takes_is_the_cap():
    body = {"fingerprint": FINGERPRINT, "action": "suppress",
            "days": wire.MAX_SUPPRESS_DAYS}
    assert wire.InboxRequest.from_json(body).days == wire.MAX_SUPPRESS_DAYS


@pytest.mark.parametrize("at", ["2026-02-30T10:00:00Z", "2026-13-01T00:00:00Z",
                                "2026-09-08T23:61:00Z"])
def test_an_instant_with_the_shape_but_no_such_moment_is_refused_everywhere(at):
    """The shape regex alone let `2026-02-30` through to a record or a
    suppression counted from nowhere; every body's `at` is the one
    `validate.instant`."""
    for parse, body in ((wire.WriteRequest.from_json, {"verb": "resolve"}),
                        (wire.ToolRequest.from_json, {"tool": "ask-incident"}),
                        (wire.InboxRequest.from_json,
                         {"fingerprint": FINGERPRINT,
                          "action": "acknowledge"})):
        with pytest.raises(wire.BadRequest) as raised:
            parse({**body, "at": at})
        assert raised.value.error == "bad_at", parse


def test_an_actor_in_an_inbox_body_is_refused_before_the_unknown_key_sweep():
    """`WriteRequest.from_json`'s rule: a client that asserts an identity is
    told identity is server-side, not that it misspelled a field."""
    with pytest.raises(wire.BadRequest) as raised:
        wire.InboxRequest.from_json({"actor": "root@example.com",
                                     "nonsense": 1})
    assert raised.value.error == "identity_is_server_side"


AGENTS_KEYS = {"revision", "built_at", "head", "generated_at", "window_hours",
               "coverage", "ticks", "models", "totals"}
TICK_KEYS = {"run_id", "command", "started", "finished", "stages",
             "incidents", "totals"}
AGENT_STAGE_KEYS = {"event_id", "task", "db", "model", "model_tier", "mode",
                    "adapter", "started", "duration_s", "ok", "rolled_back",
                    "usage", "trace"}
TRACE = DeepLink(LinkState.AVAILABLE,
                 "http://lf:3000/project/p/traces/00ff", "Open this stage's trace",
                 "The Langfuse trace of stage e1", "")
MODEL_KEYS = {"model", "adapters", "tiers", "ok", "failed", "totals"}
TOUCHED_KEYS = {"slug", "title", "status", "db", "commits"}
TOUCH_KEYS = {"run_id", "at", "commits", "stages"}

INGEST = events.RunEvent(
    kind=events.Kind.STAGE, event_id="ev-ingest", run_id="4a6961ce610c",
    at="2026-09-10T04:18:03Z", duration_s=363.4, task="ingest", db="cdb1_stby",
    adapter="pi", model="nemotron", model_tier="strong", mode="structured",
    validation_ok=True, rolled_back=False,
    usage=events.Usage(input_tokens=12000, output_tokens=900, cost_usd=0.0,
                       known=True, cost_known=False))

REPORT = dataclasses.replace(
    INGEST, event_id="ev-report", at="2026-09-10T04:28:25Z", duration_s=99.5,
    task="report", db="", adapter="codex", model="gpt-5.6-luna",
    model_tier="cheap", mode="agentic", validation_ok=False, usage=None)


def agents_view(**overrides) -> dict:
    args = {"prov": {"revision": BASE, "built_at": AT, "head": BASE},
            "generated_at": AT, "window_hours": 48, "coverage": (),
            "ticks": (), "models": (), "totals": wire.totals_json(MEASURED)}
    return wire.agents_json(**(args | overrides))


def test_the_agents_view_names_exactly_the_keys_the_screen_reads():
    assert set(agents_view()) == AGENTS_KEYS


def test_the_agents_view_is_the_one_envelope_carrying_both_records():
    """`ticks` come from `.state/`, which `coverage` bounds, and the incidents
    on them come from the wiki at a revision. A reader has to be able to say
    which half an absence came from, so both provenances cross."""
    view = agents_view(coverage=(wire.coverage_json(
        events.Coverage(name="agent_runs", lines=626, cap=4000,
                        oldest=AT, newest=AT, truncated=False)),))
    assert view["revision"] == BASE and view["head"] == BASE
    assert view["coverage"][0]["cap"] == 4000


def test_the_agents_view_echoes_the_window_it_answered_for():
    assert agents_view(window_hours=168)["window_hours"] == 168


def test_an_agent_stage_carries_the_line_the_ledger_wrote():
    stage = wire.agent_stage_json(INGEST, TRACE)
    assert set(stage) == AGENT_STAGE_KEYS
    assert (stage["task"], stage["db"]) == ("ingest", "cdb1_stby")
    assert (stage["model"], stage["model_tier"]) == ("nemotron", "strong")
    assert stage["usage"] == wire.usage_json(INGEST.usage)
    assert stage["usage"]["known"] and not stage["usage"]["cost_known"]
    assert stage["trace"] == wire.link_json(TRACE)


def test_a_stage_that_recorded_no_verdict_is_not_reported_as_a_failure():
    """`ok` is `validation_ok` verbatim: true, false, or the literal None."""
    blank = dataclasses.replace(INGEST, validation_ok=None)
    assert wire.agent_stage_json(blank, TRACE)["ok"] is None
    assert wire.agent_stage_json(REPORT, TRACE)["ok"] is False


def test_a_stage_that_measured_nothing_carries_no_row_of_zeros():
    assert wire.agent_stage_json(REPORT, TRACE)["usage"] is None


def test_a_tick_measures_its_stages_with_the_one_measure_vector():
    """`totals_json` and never a bespoke pair of sums, so the house rule holds
    without being restated: every sum ships beside the count that reported
    it."""
    tick = wire.tick_json(
        run_id=INGEST.run_id, command="run", started=INGEST.at,
        finished=REPORT.at,
        stages=(wire.agent_stage_json(INGEST, TRACE), wire.agent_stage_json(REPORT, TRACE)),
        incidents=(), totals=wire.totals_json(events.totals([INGEST, REPORT])))
    assert set(tick) == TICK_KEYS
    assert set(tick["totals"]) == TOTALS_KEYS
    assert tick["totals"]["stages"] == 2
    assert tick["totals"]["token_stages"] == 1, \
        "one of the two lines reported tokens"
    assert tick["totals"]["timed_stages"] == 2


def test_an_incident_a_tick_wrote_carries_every_commit_that_wrote_it():
    row = wire.touched_incident_json(
        slug="2026-09-08-cdb1_stby-parse-errors", title="parse errors",
        status="open", db="cdb1_stby", commits=("a" * 40, "b" * 40))
    assert set(row) == TOUCHED_KEYS
    assert row["commits"] == ["a" * 40, "b" * 40]


def test_a_model_roll_keeps_ok_and_failed_apart_from_the_stage_count():
    """They do not add up, on purpose: a line with no verdict is in neither,
    and the screen draws two counts rather than a rate."""
    blank = dataclasses.replace(REPORT, model="nemotron", validation_ok=None,
                                model_tier="cheap")
    rolls = events.by_model([INGEST, blank])
    row = wire.model_roll_json(rolls[0])
    assert set(row) == MODEL_KEYS
    assert (row["ok"], row["failed"]) == (1, 0)
    assert row["totals"]["stages"] == 2
    assert row["tiers"] == {"cheap": 1, "strong": 1}, \
        "the tier is a routing decision per stage, not a label on the model"
    assert row["adapters"] == ["codex", "pi"]


def test_the_model_table_is_busiest_first_then_by_name():
    rolls = events.by_model([INGEST, REPORT,
                             dataclasses.replace(REPORT, event_id="ev-2")])
    assert [r.model for r in rolls] == ["gpt-5.6-luna", "nemotron"]


def test_a_touch_row_ships_the_stages_rather_than_a_sentence_about_them():
    """The screen words the summary from these keys with the function the
    agents screen uses, so one wording exists rather than two."""
    row = wire.touch_json(run_id=INGEST.run_id, at=INGEST.at,
                          commits=("c" * 40,),
                          stages=(wire.agent_stage_json(INGEST, TRACE),))
    assert set(row) == TOUCH_KEYS
    assert row["stages"][0]["duration_s"] == 363.4
    assert not [key for key in row if "summary" in key or "said" in key]


def test_the_agents_view_emits_no_difference_no_adapter_reported():
    """`test_no_loop_view_emits_a_difference_no_adapter_reported`'s rule, over
    the envelope that grew after it."""
    spelled = _spelled(agents_view(
        ticks=(wire.tick_json(
            run_id=INGEST.run_id, command="run", started=INGEST.at,
            finished=REPORT.at, stages=(wire.agent_stage_json(INGEST, TRACE),),
            incidents=(), totals=wire.totals_json(MEASURED)),),
        models=(wire.model_roll_json(events.by_model([INGEST])[0]),)))
    assert spelled
    assert not {key for key in spelled
                if "delta" in key or "change" in key or "diff" in key}


BOARD = LinkBoard(
    title="dbhost Link Board",
    intro="Everything reachable over `tailscale`.",
    sections=(
        Section(title="Dashboards",
                note="Generated by `build_dashboards.py`.",
                links=(Link(name="Fleet triage",
                            what="Where on call starts.",
                            url="http://box.example.invalid:5601/",
                            tag=Tag.TAILSCALE),)),
        Section(title="Repositories", note="",
                links=(Link(name="logbook", what="",
                            url="https://github.com/example/logbook",
                            tag=Tag.GITHUB),)),
    ))


def test_the_link_board_carries_its_heading_and_its_sections():
    got = wire.links_json(BOARD)
    assert set(got) == {"title", "intro", "sections"}
    assert got["title"] == "dbhost Link Board", "the board names itself"
    assert got["intro"] == BOARD.intro, "the intro crosses verbatim"
    assert [s["title"] for s in got["sections"]] == ["Dashboards",
                                                     "Repositories"], \
        "sections keep the order the file gave them"


def test_a_section_carries_its_note_and_its_links():
    dashboards, repositories = wire.links_json(BOARD)["sections"]
    assert set(dashboards) == {"title", "note", "links"}
    assert dashboards["note"] == BOARD.sections[0].note, "a note crosses whole"
    assert repositories["note"] == "", "a section with no note sends an empty one"


def test_a_link_carries_the_four_keys_and_nothing_else():
    link = wire.links_json(BOARD)["sections"][0]["links"][0]
    assert set(link) == {"name", "what", "url", "tag"}
    assert link["name"] == "Fleet triage", "the name is the operator's"
    assert link["url"] == "http://box.example.invalid:5601/", \
        "the url crosses unchanged"


def test_an_absent_what_is_an_empty_string_and_never_a_missing_key():
    link = wire.links_json(BOARD)["sections"][1]["links"][0]
    assert link["what"] == "", "the page asks whether it is empty, not whether it is there"


def test_a_tag_crosses_as_its_word():
    sections = wire.links_json(BOARD)["sections"]
    tags = [link["tag"] for s in sections for link in s["links"]]
    assert tags == ["tailscale", "github"], "each tag is the word the chip shows"
    assert all(type(tag) is str for tag in tags), \
        "a plain str and never the StrEnum, which isinstance would not tell apart"


def test_an_empty_board_is_a_heading_and_no_sections():
    got = wire.links_json(empty_board())
    assert got["sections"] == [], "no sections is the whole signal for no board"
    assert got["title"], "the empty board still has something to head the screen with"
    assert set(got) == {"title", "intro", "sections"}, \
        "the empty board carries no flag of its own"
