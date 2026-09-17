"""The end-to-end path (alert line -> ES lookup -> digest section -> markdown)
is pinned by the `ora600_trace_context` fixture case in tests/test_fixtures.py.
"""

import copy
import json

import fixtures as fx
import pytest
import requests

from dbwiki import trace
from dbwiki.compactor import Compactor
from dbwiki.digest_md import render_md
from dbwiki.patterns import extract_trace_paths
from dbwiki.structured import _digest_material, build_prompt

TL = {"enabled": True, "index_patterns": [".ds-logs-oracle.trace-*"],
      "timestamp_field": "@timestamp", "path_field": "oracle.trace.file",
      "slack_hours": 1, "max_paths": 6, "max_docs_per_path": 5,
      "max_excerpt_chars": 1500, "max_total_chars": 6000}

TRACE = "/u01/app/oracle/diag/rdbms/cdb1/cdb1/trace/cdb1_ora_7903.trc"
INCDIR = "/u01/app/oracle/diag/rdbms/cdb1/cdb1/incident/incdir_48291/cdb1_ora_7903_i48291.trc"


def doc(n: int, message: str, ts: str = "2026-07-11T04:17:04Z") -> dict:
    return {"index": ".ds-logs-oracle.trace-default-000001", "id": f"t{n}",
            "ts": ts, "message": message}


# ---- extraction --------------------------------------------------------------

@pytest.mark.parametrize("msg,expect", [
    (f"Errors in file {TRACE}  (incident=48291):", [TRACE]),
    (f"Incident 48291 created, dump file: {INCDIR}", [INCDIR]),
    (f"See trace file {TRACE} for details", [TRACE]),
    (f"Errors in file {TRACE}:", [TRACE]),
    (f"{TRACE} then {TRACE}", [TRACE]),
    (f"{TRACE} and {INCDIR}", [TRACE, INCDIR]),
    ("ORA-00600: internal error code, arguments: [kdsgrp1]", []),
    ("relative/path/cdb1_ora_1.trc is not absolute", []),
    ("/u01/app/oracle/trace/cdb1_ora_1.trm is a trim file", []),
])
def test_extract_trace_paths(msg, expect):
    assert extract_trace_paths(msg) == expect


def test_paths_are_extracted_per_event_not_per_group():
    from dbwiki.patterns import message_template
    a = "Errors in file /u01/diag/trace/cdb1_ora_111.trc:"
    b = "Errors in file /u01/diag/trace/cdb1_ora_222.trc:"
    assert message_template(a) == message_template(b)
    assert extract_trace_paths(a) != extract_trace_paths(b)


# ---- excerpting --------------------------------------------------------------

HEADER = ("Trace file /u01/x/cdb1_ora_7903.trc\n"
          "Oracle Database 19c Enterprise Edition Release 19.0.0.0.0\n"
          "Version 19.27.0.0.0\n"
          "ORACLE_HOME:    /u01/app/oracle/product/19.0.0/dbhome_1\n"
          "Instance name: cdb1")
BODY = ("*** 2026-07-11T04:17:04.118000+00:00\n"
        "*** SESSION ID:(296.32736)\n"
        "DDE: Problem Key was flood controlled\n"
        "ORA-00600: internal error code, arguments: [kdsgrp1]\n"
        "Current SQL statement for this session:\n"
        "SELECT COUNT(*) FROM soe.orders\n"
        "----- Call Stack Trace -----\n"
        "kdsgrp1 <- kdsgrp <- qertbFetchByRowID")


def test_excerpt_keeps_the_header_and_the_code_lines_with_context():
    text, truncated = trace.select_excerpt([doc(1, HEADER), doc(2, BODY)],
                                           ["ORA-600"], 1500)
    assert not truncated
    assert text.splitlines()[0] == "*** 2026-07-11T04:17:04.118000+00:00", \
        "the chunk carrying the code is promoted ahead of the header"
    assert "ORA-00600: internal error code, arguments: [kdsgrp1]" in text
    assert "SELECT COUNT(*) FROM soe.orders" in text, "context line after the code"
    assert "Trace file /u01/x/cdb1_ora_7903.trc" in text, "header chunk kept too"
    assert "kdsgrp1 <- kdsgrp <- qertbFetchByRowID" not in text, \
        "lines beyond the code's context window are dropped"
    assert "…" in text, "dropped runs leave a marker"


def test_excerpt_without_matching_codes_keeps_fetch_order():
    text, _ = trace.select_excerpt([doc(1, HEADER), doc(2, BODY)], [], 1500)
    assert text.splitlines()[0] == "Trace file /u01/x/cdb1_ora_7903.trc"


def test_a_chunk_with_neither_head_nor_codes_contributes_nothing():
    plain = "session closed\nnothing else happened here"
    with_plain, _ = trace.select_excerpt([doc(1, HEADER), doc(2, plain)], [], 1500)
    header_only, _ = trace.select_excerpt([doc(1, HEADER)], [], 1500)
    assert with_plain == header_only


def test_excerpt_is_capped_and_marked():
    text, truncated = trace.select_excerpt([doc(1, HEADER), doc(2, BODY)],
                                           ["ORA-600"], 80)
    assert truncated
    assert text.endswith("\n[truncated]")
    assert len(text) <= 80, "the marker fits inside the budget, not beside it"


# ---- fetching and the per-digest budget --------------------------------------

class _PoolES:
    def __init__(self, docs_by_path: dict[str, list[dict]]):
        self.docs_by_path = docs_by_path
        self.searches: list[dict] = []

    def search(self, index, body):
        self.searches.append(body)
        (term,) = [f["term"] for f in body["query"]["bool"]["filter"] if "term" in f]
        (path,) = term.values()
        hits = [{"_index": d["index"], "_id": d["id"],
                 "_source": {"@timestamp": d["ts"], "message": d["message"]}}
                for d in self.docs_by_path.get(path, [])]
        return {"hits": {"hits": hits[:body["size"]]}}


class _AngryES:
    def search(self, index, body):
        raise requests.ConnectionError("connection refused")


def test_fetch_widens_the_window_by_the_configured_slack():
    es = _PoolES({TRACE: [doc(1, HEADER)]})
    trace.fetch_trace_docs(es, TL, TRACE, "2026-07-11T00:00:00Z",
                           "2026-07-12T00:00:00Z")
    (rng,) = [f["range"] for f in es.searches[0]["query"]["bool"]["filter"]
              if "range" in f]
    assert rng["@timestamp"] == {"gte": "2026-07-10T23:00:00Z",
                                 "lt": "2026-07-12T01:00:00Z"}


def test_fetch_degrades_to_nothing_when_elasticsearch_fails(capsys):
    got = trace.fetch_trace_docs(_AngryES(), TL, TRACE, "2026-07-11T00:00:00Z",
                                 "2026-07-12T00:00:00Z")
    assert got == []
    assert TRACE in capsys.readouterr().err


def test_paths_with_no_documents_contribute_no_entry():
    es = _PoolES({TRACE: [doc(1, HEADER)]})
    meta = {"mentions": 1, "rules": ["errors_in_file"], "codes": []}
    got = trace.trace_evidence(es, TL, {TRACE: dict(meta), INCDIR: dict(meta)},
                               "2026-07-11T00:00:00Z", "2026-07-12T00:00:00Z")
    assert [e["path"] for e in got] == [TRACE]
    assert got[0]["docs_found"] == 1


def test_an_incomplete_lookup_block_disables_itself(tmp_path, capsys):
    cfg = fx.fixture_config(tmp_path)
    cfg.trace_lookup = {"enabled": True}
    assert Compactor(cfg).trace_lookup is None
    assert "trace_lookup" in capsys.readouterr().err


def _four_paths(max_total_chars: int, message: str = BODY) -> list[dict]:
    paths = [f"/u01/diag/trace/cdb1_ora_{i}.trc" for i in range(4)]
    es = _PoolES({p: [doc(1, message)] for p in paths})
    return trace.trace_evidence(
        es, {**TL, "max_total_chars": max_total_chars},
        {p: {"mentions": 1, "rules": ["errors_in_file"],
             "codes": ["ORA-600"]} for p in paths},
        "2026-07-11T00:00:00Z", "2026-07-12T00:00:00Z")


def test_the_per_digest_budget_shrinks_excerpts_rather_than_dropping_paths():
    """Which paths appear must depend only on what Elasticsearch holds.
    Dropping paths on excerpt length would smuggle the excerpt heuristic back
    into the content hash."""
    got = _four_paths(len(BODY))
    assert len(got) == 4
    assert sum(len(e["excerpt"]) for e in got) <= len(BODY)
    assert got[-1]["truncated"] and not got[-1]["excerpt"]
    assert got[-1]["docs_found"] == 1 and got[-1]["es_samples"], \
        "a path with no room left still names its documents for `es trace`"


def test_the_admitted_paths_do_not_move_when_the_excerpt_does():
    long_run = "\n".join(["ORA-00600: internal error"] + ["filler"] * 60)
    assert ([e["path"] for e in _four_paths(200, long_run)]
            == [e["path"] for e in _four_paths(200, "ORA-00600: internal error")])


# ---- content hash ------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path):
    return fx.fixture_config(tmp_path)


def test_a_trace_less_digest_hashes_as_it_did_before_the_feature(cfg):
    """No fleet-wide re-ingest on upgrade."""
    for name in ("ora600_internal_error", "routine_traffic",
                 "dataguard_transport_error"):
        case = fx.load_case(name)
        fx.seed_registry(cfg, case)
        digest = fx.compact_case(cfg, case)
        assert not any("trace_evidence" in s for s in digest["sources"].values())
        assert Compactor.content_hash(digest) == _legacy_hash(digest)


def _legacy_hash(digest: dict) -> str:
    """`content_hash` exactly as it read before trace evidence existed."""
    import hashlib
    core = {"deltas": digest["deltas"],
            "sources": {n: {"total": s["total_events"],
                            "notable": [(g["rule"], g["template"], g["count"])
                                        for g in s["notable"]]}
                        for n, s in digest["sources"].items()}}
    return hashlib.sha256(
        json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def test_arriving_trace_documents_move_the_hash_once(cfg):
    case = fx.load_case("ora600_trace_context")
    fx.seed_registry(cfg, case)
    with_trace = fx.compact_case(cfg, case)

    late = copy.deepcopy(case)
    late["hits"]["trace"] = []
    fx.seed_registry(cfg, late)
    without = fx.compact_case(cfg, late)

    assert Compactor.content_hash(without) == _legacy_hash(without)
    assert Compactor.content_hash(with_trace) != Compactor.content_hash(without)


def test_rewriting_only_the_excerpt_text_leaves_the_hash_alone(cfg):
    case = fx.load_case("ora600_trace_context")
    fx.seed_registry(cfg, case)
    digest = fx.compact_case(cfg, case)
    retuned = copy.deepcopy(digest)
    for section in retuned["sources"].values():
        for e in section.get("trace_evidence", []):
            e["excerpt"] = "a completely different excerpt"
            e["truncated"] = not e["truncated"]
    assert Compactor.content_hash(retuned) == Compactor.content_hash(digest)


# ---- what the prompts carry --------------------------------------------------

def test_the_ingest_prompt_carries_the_excerpt(cfg, tmp_path):
    case = fx.load_case("ora600_trace_context")
    fx.seed_registry(cfg, case)
    digest = fx.compact_case(cfg, case)
    (tmp_path / "wiki").mkdir(exist_ok=True)
    prompt = build_prompt("cdb1", digest, tmp_path / "wiki")
    assert "### Trace evidence" in prompt
    assert "DDE: Problem Key 'ORA 600 [kdsgrp1]' was flood controlled" in prompt
    assert "not a separate event" in prompt, "the reading rule travels with it"


def test_the_escalated_material_keeps_the_trace_section(cfg):
    case = fx.load_case("ora600_trace_context")
    fx.seed_registry(cfg, case)
    material = _digest_material(render_md(fx.compact_case(cfg, case)))
    assert "### Trace evidence" in material
    assert "ORA-00600: internal error code" in material
    assert "### Routine (counters)" not in material


def test_asm_trace_paths_are_extracted():
    """ASM writes its diag tree under `+asm/+ASM1`, and `+` is not a word
    character."""
    p = "/u01/app/oracle/diag/asm/+asm/+ASM1/trace/+ASM1_ora_12345.trc"
    assert extract_trace_paths(f"Errors in file {p}:") == [p]


def test_a_non_utc_window_is_shifted_as_a_real_instant():
    assert trace._shift("2026-07-11T10:00:00+02:00", -1) == "2026-07-11T07:00:00Z"
    assert trace._shift("2026-07-11T10:00:00Z", -1) == "2026-07-11T09:00:00Z"


def test_es_trace_refuses_an_unusable_config(capsys):
    from dbwiki import cli

    class _Cfg:
        trace_lookup = {"enabled": True, "timestamp_field": "@timestamp"}

    class _Args:
        path, from_, to, size = TRACE, None, None, 20

    assert cli._es_trace(_Cfg(), _AngryES(), _Args()) == 2
    err = capsys.readouterr().err
    assert "index_patterns" in err and "path_field" in err
