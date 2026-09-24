"""The durable markdown evidence reference: what a page may say, what comes
back, and what a digest yields without anyone editing a page.

The round trip is the load-bearing property. A reference is written once and
read for as long as the incident matters, so bytes that do not survive a parse
are a page that silently loses its evidence.
"""

import pytest

from dbwiki.evidence_ref import (EVIDENCE_REF_SCHEMA_VERSION, Entity,
                                 EvidenceRef, EvidenceRefMalformed, Signature,
                                 Window, from_digest_group, parse_refs,
                                 render_ref)

DB = "cdb1"
DAY = "2026-08-30"
START = f"{DAY}T10:00:00Z"
END = f"{DAY}T10:20:00Z"


def ref(**overrides) -> EvidenceRef:
    args = {"schema_version": EVIDENCE_REF_SCHEMA_VERSION,
            "kind": "elastic_filter",
            "template_id": "oracle-error-context-v1",
            "environment": "production",
            "data_view": "oracle-logs",
            "entity": Entity("database", DB),
            "signature": Signature("oracle_error", "ORA-12543"),
            "window": Window(START, END),
            "representative_document_id": "AZ83bZHL6JT2v6-J7TTb",
            "summary": "ORA-12543 transport failures on cdb1"}
    return EvidenceRef(**(args | overrides))


REFS = [
    ref(),
    ref(representative_document_id="", summary=""),
    ref(summary='Ampersand & "quoted": a colon too'),
    ref(signature=Signature("rule", "awr_top_wait"),
        representative_document_id=""),
]


@pytest.mark.parametrize("original", REFS)
def test_a_rendered_reference_parses_back_to_the_same_value(original):
    assert parse_refs(render_ref(original)) == ((original,), ())


def test_the_rendered_block_is_the_documents_key_order():
    assert render_ref(ref()) == """```yaml
evidence_ref:
  schema_version: 1
  kind: elastic_filter
  template_id: oracle-error-context-v1
  environment: production
  data_view: oracle-logs
  entity:
    type: database
    id: cdb1
  signature:
    type: oracle_error
    value: ORA-12543
  window:
    from: '2026-08-30T10:00:00Z'
    to: '2026-08-30T10:20:00Z'
  representative_document_id: AZ83bZHL6JT2v6-J7TTb
  summary: ORA-12543 transport failures on cdb1
```
"""


def test_the_empty_optionals_are_omitted_rather_than_written_empty():
    block = render_ref(ref(representative_document_id="", summary=""))
    assert "representative_document_id" not in block
    assert "summary" not in block


BAD_BLOCK = """```yaml
evidence_ref:
  schema_version: 1
  kind: elastic_filter
  template_id: oracle-error-context-v1
  environment: production
  data_view: oracle-logs
  entity:
    type: database
    id: cdb1
  signature:
    type: oracle_error
    value: ORA-12543
  window:
    from: yesterday
    to: today
```
"""


def test_one_malformed_block_costs_itself_one_problem_and_nothing_else():
    page = f"# Incident\n\n{render_ref(REFS[0])}\nprose\n\n{BAD_BLOCK}"
    refs, problems = parse_refs(page)
    assert refs == (REFS[0],)
    assert len(problems) == 1
    assert problems[0].where == "evidence_ref block 2"
    assert "window.from" in problems[0].message


def test_a_yaml_block_that_is_not_a_reference_is_skipped_in_silence():
    page = ("```yaml\nkind: resolve\nactor: dba@example.com\n```\n"
            f"\n{render_ref(REFS[0])}")
    assert parse_refs(page) == ((REFS[0],), ())


def test_a_key_outside_the_schema_is_a_problem_and_not_a_silent_drop():
    page = render_ref(ref()).replace("  summary:", "  sumary:")
    refs, problems = parse_refs(page)
    assert refs == ()
    assert "sumary" in problems[0].message


def test_a_page_with_no_reference_answers_nothing_twice():
    assert parse_refs("# Just prose\n\nand a [[link]].\n") == ((), ())


@pytest.mark.parametrize("overrides, offender", [
    ({"schema_version": 2}, "schema_version"),
    ({"kind": "sql_query"}, "kind"),
    ({"template_id": ""}, "template_id"),
    ({"entity": Entity("database", "")}, "entity.id"),
    ({"signature": Signature("", "ORA-1")}, "signature.type"),
    ({"window": Window("2026-08-30", END)}, "window.from"),
    ({"window": Window(END, START)}, "window.to"),
])
def test_construction_refuses_a_reference_that_cannot_be_one(overrides,
                                                             offender):
    with pytest.raises(EvidenceRefMalformed) as exc:
        ref(**overrides)
    assert str(exc.value).startswith(offender)


LIVE_GROUP = {
    "rule": "ora_error", "class": "error", "count": 41,
    "first_ts": "2026-08-30T10:00:00.327Z",
    "last_ts": "2026-08-30T10:20:00.881Z",
    "codes": ["ORA-12543", "ORA-12560"], "message": "TNS transport error",
    "template": "ORA-<n> transport", "es_samples": [
        {"index": ".ds-logs-oracle-2026", "id": "AZ83bZHL6JT2v6-J7TTb",
         "ts": "2026-08-30T10:00:00.327Z"}],
}

AWR_GROUP = {
    "rule": "awr_top_wait", "class": "performance", "count": 3,
    "first_ts": "2026-08-30T09:00:00Z", "last_ts": "2026-08-30T10:00:00Z",
    "codes": [], "message": "log file sync dominates the window",
    "template": "awr top wait", "es_samples": [],
}


def test_a_live_group_yields_its_first_code_and_its_representative_document():
    made = from_digest_group(DB, DAY, LIVE_GROUP)
    assert made.signature == Signature("oracle_error", "ORA-12543")
    assert made.representative_document_id == "AZ83bZHL6JT2v6-J7TTb"
    assert made.window == Window(START, END)
    assert made.entity == Entity("database", DB)
    assert made.environment == "production"
    assert "41" in made.summary and DB in made.summary and DAY in made.summary


def test_an_awr_group_yields_its_rule_and_no_document():
    made = from_digest_group(DB, DAY, AWR_GROUP)
    assert made.signature == Signature("rule", "awr_top_wait")
    assert made.representative_document_id == ""
    assert made.window == Window(f"{DAY}T09:00:00Z", f"{DAY}T10:00:00Z")


def test_a_group_with_no_timestamps_falls_back_to_the_whole_day():
    made = from_digest_group(DB, DAY, {"rule": "quiet", "codes": [],
                                       "count": 1, "es_samples": []})
    assert made.window == Window(f"{DAY}T00:00:00Z", f"{DAY}T23:59:59Z")


def test_the_environment_is_the_callers_fact_and_not_the_digests():
    made = from_digest_group(DB, DAY, LIVE_GROUP, environment="staging")
    assert made.environment == "staging"


def test_a_group_reference_round_trips_like_any_other():
    made = from_digest_group(DB, DAY, LIVE_GROUP)
    assert parse_refs(render_ref(made)) == ((made,), ())
