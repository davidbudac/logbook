"""ADR-0002 redaction: what leaves the box carries no hostname, IP, db or
service name, ES id or wiki path — and the leak check refuses anything that
slips through. Fixtures under tests/fixtures/redact/ are real-shaped copies
of wiki pages, a digest and a registry."""

import json
import re
from pathlib import Path

import pytest

from dbwiki import normalize
from dbwiki.redact import (IDENTITY_FIELD_COVERAGE, KEEP_ADDRESSES, Redactor,
                           RedactionLeak, Vocabulary, build_vocabulary,
                           harvest_page, strip_wiki_refs)
from dbwiki.research_offload import (build_research_request,
                                     build_source_review_request,
                                     occurrence_rows, request_filename)

FIX = Path(__file__).parent / "fixtures" / "redact"
WIKI = FIX / "wiki"
STATE = FIX / "state"

IDENTIFYING = re.compile(
    r"192\.168|lab-dg|ol9-|cdb1|cdb#|pdb1|localdomain|\.ds-logs|AZ__|/u01/",
    re.I)


class Cfg:
    """Bare config double: only what build_vocabulary/_estate read."""
    def __init__(self, extra_terms=(), estate=None):
        self.raw = {
            "elasticsearch": {"url": "http://es-node1.localdomain:9200"},
            "redact": {"terms": list(extra_terms)},
        }
        self.sources = {
            "alert": {"index_patterns": ["oracle-logs-alert-*", ".ds-logs-oracle.alert-*"]},
            "listener": {"index_patterns": ["oracle-logs-listener-*"],
                         "db_service_field": "service.name",
                         "db_service_domains": ["world"]},
        }
        self.state_dir = STATE
        self.research = {"estate": estate or {"version": "19c"}}


@pytest.fixture
def vocab():
    return build_vocabulary(Cfg(), WIKI, STATE)


@pytest.fixture
def red(vocab):
    return Redactor(vocab, "run-1")


# ---- vocabulary --------------------------------------------------------------------

def test_vocabulary_from_pages_registry_and_config(vocab):
    assert {"cdb1", "cdb1_stby"} <= vocab.dbs
    assert {"lab-dg1.localdomain", "lab-dg1", "lab-dg2", "ol9-em241.localdomain",
            "es-node1.localdomain"} <= vocab.hosts
    assert {"192.0.2.121", "192.0.2.122", "192.0.2.140"} <= vocab.ips
    assert {"cdb1_dgmgrl.world", "cdb1.world", "pdb1.world"} <= vocab.services
    assert "address=(protocol=tcp" not in vocab.services      # registry junk filtered
    assert {"oracle-logs-alert-", ".ds-logs-oracle.alert-"} <= vocab.indexes
    assert vocab.source_domains == {"docs.oracle.com", "example.org"}


def test_vocabulary_hashed_template_variants(vocab):
    """Compactor templates spell digit runs as `#`; the vocabulary must too."""
    assert "cdb#" in vocab.dbs and "lab-dg#.localdomain" in vocab.hosts


def test_harvested_db_service_is_not_a_host(vocab):
    # `cdb1.world` in a db page's prose is a service, `cdb1` is the db
    assert "cdb1" not in vocab.hosts and "cdb1.world" not in vocab.hosts


def test_operator_terms_and_missing_dirs(tmp_path):
    (tmp_path / "errors").mkdir()
    v = build_vocabulary(Cfg(extra_terms=["site-berlin"]), tmp_path, tmp_path / "nostate")
    assert v.terms == {"site-berlin"} and not v.dbs


def test_harvest_page_keeps_versions_and_loopback():
    v = Vocabulary()
    harvest_page("Version 19.27.0.0.0 RU 19.27.0.0.250415 on 10.1.2.3, listener 0.0.0.0", v)
    assert v.ips == {"10.1.2.3"}


# ---- redaction ---------------------------------------------------------------------

@pytest.mark.parametrize("text,expect", [
    ("Host `lab-dg1.localdomain` (192.0.2.121)", "Host `HOST_A` (IP_A)"),
    ("standby cdb1_stby.world; trace cdb1_tt00_2516.trc, dr1cdb1.dat",
     "standby SVC_A; trace DB_A_tt00_2516.trc, dr1DB_A.dat"),
    ("(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=lab-dg2)(PORT=1521))"
     "(CONNECT_DATA=(SERVER=DEDICATED)(SERVICE_NAME=CDB1_DGMGRL.world)"
     "(CID=(PROGRAM=oracle)(HOST=ol9-19-dg1.localdomain)(USER=oracle))))",
     "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=HOST_A)(PORT=1521))"
     "(CONNECT_DATA=(SERVER=DEDICATED)(SERVICE_NAME=SVC_A)"
     "(CID=(PROGRAM=oracle)(HOST=HOST_B)(USER=USER_A))))"),
    ("control_files /u01/oradata/CDB1/control01.ctl and /home/oracle.",
     "control_files PATH_A and PATH_B."),
    ("from 00:00:09.776Z to 00:14:13.376Z on 0.0.0.0, fe80::1 and 2001:db8::5",
     "from 00:00:09.776Z to 00:14:13.376Z on 0.0.0.0, IP_A and IP_B"),
    ("index .ds-logs-oracle.alert-2026.07.12-000003 id AZgQ1n2sK-9xW_abcdEf",
     "index IDX_A2026.07.12-000003 id IDX_B"),
    ("Version 19.0.0.0.0, RU 19.27.0.0.250415, 12.2.0.1 vs 10.2.0.4 and 172.16.0.5",
     "Version 19.0.0.0.0, RU 19.27.0.0.250415, 12.2.0.1 vs IP_A and IP_B"),
    ("internationalization is a long plain word", "internationalization is a long plain word"),
    ("(PDBNAME=PDB1): ORA-00600: internal error code, arguments: [4194]",
     "(PDBNAME=SVC_A): ORA-00600: internal error code, arguments: [4194]"),
    ("(PDBNAME=CDB$ROOT) then (PDBNAME=PDB$SEED)", "(PDBNAME=CDB$ROOT) then (PDBNAME=PDB$SEED)"),
])
def test_redact_samples(red, text, expect):
    out = red.redact(text)
    assert out == expect
    assert red.leak_check(out) == []


def test_trace_evidence_lines_sweep_clean(red):
    """The trace path, the file header and the excerpt's identity lines are
    the same trust boundary as an alert line, and must redact the same."""
    out = red.redact(
        "- `/u01/app/oracle/diag/rdbms/cdb1/cdb1/trace/cdb1_ora_7903.trc` — 2 docs\n"
        "  > Trace file /u01/app/oracle/diag/rdbms/cdb1/cdb1/trace/cdb1_ora_7903.trc\n"
        "  > Node name:\tol9-19-dg1.localdomain\n"
        "  > Instance name: cdb1\n"
        "  > ORA-00600: internal error code, arguments: [kdsgrp1]\n"
        "  - es: .ds-logs-oracle.trace-default-2026.08.31-000001/AaBZYbtS9QeZauM9jh39")
    assert not IDENTIFYING.search(out), out
    assert red.leak_check(out) == []
    assert "ORA-00600: internal error code, arguments: [kdsgrp1]" in out, \
        "the error code and its arguments are the point of the excerpt"


def test_never_seen_sibling_host_and_odd_suffix(red):
    """`ol9-19-dg3.localdomain1` appears in no wiki page: the digit family of
    known hosts and the suffix-with-digits rule still catch it whole."""
    out = red.redact("switching master observer to 'ol9-19-dg3.localdomain1'")
    assert out == "switching master observer to 'HOST_A'"
    assert red.mapping["HOST_A"] == "ol9-19-dg3.localdomain1"


def test_hashed_templates(red):
    out = red.redact("Failed to connect to remote database cdb#_stby on "
                     "(HOST=lab-dg#.localdomain) file /u#/diag/rdbms/cdb#/cdb#/trace/cdb#_ora_#.trc")
    assert not IDENTIFYING.search(out), out


def test_approved_url_survives_other_urls_swept(red):
    out = red.redact("(source: sources/oracle-docs; url: <https://docs.oracle.com/en/error-help/db/ora-12154/>;"
                     " accessed: 2026-07-28) see http://lab-dg1.localdomain:1158/em")
    assert "https://docs.oracle.com/en/error-help/db/ora-12154/" in out
    assert "lab-dg1" not in out and "http://HOST_A:1158/em" in out


def test_pseudonyms_consistent_across_categories_and_case(red):
    out = red.redact('db_unique_name="CDB1" then cdb1 again and Cdb1_stby')
    assert out == 'db_unique_name="DB_A" then DB_A again and DB_B'
    assert red.mapping == {"DB_A": "CDB1", "DB_B": "Cdb1_stby"}


def test_redact_obj_and_demap_roundtrip(red):
    obj = {"a": ["lab-dg1.localdomain", {"b": "cdb1 on 192.0.2.121"}], "n": 3}
    out = red.redact_obj(obj)
    assert out == {"a": ["HOST_A", {"b": "DB_A on IP_A"}], "n": 3}
    back = red.demap("The standby DB_A on HOST_A (IP_A); DB_AB unknown")
    assert back == "The standby cdb1 on lab-dg1.localdomain (192.0.2.121); DB_AB unknown"
    assert red.demap_obj(out) == {"a": ["lab-dg1.localdomain", {"b": "cdb1 on 192.0.2.121"}], "n": 3}


def test_pseudonym_labels_beyond_z(vocab):
    r = Redactor(vocab)
    names = [r.pseudonym("IP", f"10.0.0.{i}") for i in range(28)]
    assert names[0] == "IP_A" and names[25] == "IP_Z" and names[26] == "IP_AA" and names[27] == "IP_AB"


def test_keep_addresses():
    assert {"0.0.0.0", "127.0.0.1", "::1", "localhost"} <= KEEP_ADDRESSES


# ---- leak check --------------------------------------------------------------------

def test_leak_check_catches_untouched_payload(red):
    hits = red.leak_check({"x": "seen on lab-dg2 with 192.0.2.122"})
    assert set(hits) == {"lab-dg2", "192.0.2.122"}
    with pytest.raises(RedactionLeak):
        red.check(["lab-dg2"])


def test_leak_check_ignores_pseudonyms(red):
    assert red.leak_check("HOST_A talked to DB_B via SVC_C; PATH_A missing") == []


# ---- persistence -------------------------------------------------------------------

def test_save_and_load_mapping(red, tmp_path):
    red.redact("cdb1 at 192.0.2.121")
    p = red.save(tmp_path)
    assert p == tmp_path / "redaction" / "run-1.json"
    r2 = Redactor.load(tmp_path, "run-1")
    # several requests share a run_id: a per-request key keeps their mappings apart
    r3 = Redactor(red.vocab, "run-1", key="run-1-ORA-600")
    r3.redact("lab-dg2")
    assert r3.save(tmp_path) == tmp_path / "redaction" / "run-1-ORA-600.json"
    assert Redactor.load(tmp_path, "run-1-ORA-600").mapping == {"HOST_A": "lab-dg2"}
    assert Redactor.load(tmp_path, "run-1").mapping == red.mapping
    assert r2.mapping == red.mapping
    assert r2.demap("DB_A / IP_A") == "cdb1 / 192.0.2.121"
    # a re-loaded redactor keeps handing out the same pseudonyms
    assert r2.pseudonym("DB", "CDB1") == "DB_A"


# ---- drop, don't mask ---------------------------------------------------------------

def test_strip_wiki_refs():
    s = ("see [[databases/cdb1]] and digests/cdb1/2026-07-12.md "
         "(observed — digests/cdb1/2026-07-28.md, startup blocks) done")
    assert strip_wiki_refs(s) == "see and done"


# ---- normalize identity fields are classified ----------------------------------------

def test_every_normalize_field_is_classified():
    src = (Path(normalize.__file__)).read_text()
    keys = set(re.findall(r'ev\["([a-z_]+)"\]', src))
    classified = set(normalize.IDENTITY_FIELDS) | set(normalize.BENIGN_FIELDS)
    assert keys <= classified, f"unclassified normalize fields: {sorted(keys - classified)}"
    assert set(IDENTITY_FIELD_COVERAGE) == set(normalize.IDENTITY_FIELDS)
    assert not set(normalize.IDENTITY_FIELDS) & set(normalize.BENIGN_FIELDS)


# ---- request builder ---------------------------------------------------------------

def test_occurrence_rows():
    rows = occurrence_rows((WIKI / "errors" / "ORA-12154.md").read_text())
    assert [r["day"] for r in rows] == ["2026-08-06", "2026-08-14"]
    assert rows[1]["evidence"] == "digests/cdb1/2026-08-14.md"


def test_research_request_is_clean_and_useful():
    req, red = build_research_request(Cfg(), WIKI, WIKI / "errors" / "ORA-12154.md",
                                      "run-7", state_dir=STATE)
    dumped = json.dumps(req)
    assert not IDENTIFYING.search(dumped), dumped
    assert not re.search(r"\[\[|digests/", dumped)
    assert req["kind"] == "research" and req["code"] == "ORA-12154"
    assert req["run_id"] == "run-7" and req["schema_version"] == 1
    assert req["estate"] == {"product": "Oracle Database", "version": "19c"}
    syn = req["synopsis"]
    # message samples come from the cited digest, redacted, deduped by template
    assert len(syn["message_templates"]) == 2
    assert "ORA-16782" in syn["message_templates"][0] and "DB_" in syn["message_templates"][0]
    assert syn["occurrences"] == {"count": 46, "first_seen": "2026-07-28T00:00:36.686Z",
                                  "last_seen": "2026-08-14T13:23:52.917Z", "spread_days": 2}
    assert syn["co_occurring_codes"] == ["ORA-16607", "ORA-16782"]
    assert len(syn["context"]) == 2 and "HOST_" in syn["context"][1]
    assert "https://docs.oracle.com/en/error-help/db/ora-12154/" in syn["current_reference"]
    assert syn["open_incidents"] == 1 and syn["resolution_state"] == "no confirmed fix"
    assert [s["slug"] for s in req["sources"]] == ["oracle-docs", "some-blog"]
    assert req["sources"][1]["status"] == "deprecated"
    assert "pseudonym" in req["instructions"]
    # the redactor carries the de-mapping table for the way back
    assert red.mapping and red.demap("DB_A") in {"cdb1", "cdb1_stby"}
    assert request_filename(req) == "research-ORA-12154-run-7.json"


def test_research_request_without_digest_or_registry(tmp_path):
    (tmp_path / "errors").mkdir()
    (tmp_path / "sources").mkdir()
    (tmp_path / "incidents").mkdir()
    page = tmp_path / "errors" / "ORA-1013.md"
    page.write_text((WIKI / "errors" / "ORA-1013.md").read_text())
    req, _ = build_research_request(Cfg(), tmp_path, page, "r", state_dir=tmp_path / "none")
    syn = req["synopsis"]
    assert syn["message_templates"] == [] and syn["co_occurring_codes"] == []
    assert syn["occurrences"] == {"count": 1, "first_seen": "2026-07-28",
                                  "last_seen": "2026-07-28", "spread_days": 1}
    assert syn["current_reference"] == "" and req["sources"] == []


def test_research_request_fails_closed_on_leak(monkeypatch):
    """If redaction ever leaves an identifier behind, the request is refused."""
    import dbwiki.research_offload as ro
    monkeypatch.setattr(ro.Redactor, "redact_obj", lambda self, obj: obj)
    with pytest.raises(RedactionLeak) as ei:
        build_research_request(Cfg(), WIKI, WIKI / "errors" / "ORA-12154.md", "r",
                               state_dir=STATE)
    assert "cdb1" in str(ei.value)


def test_source_review_request_has_no_estate_data():
    req = build_source_review_request(WIKI, WIKI / "sources" / "oracle-docs.md", "r9")
    assert req["kind"] == "source-review" and req["slug"] == "oracle-docs"
    assert req["domains"] == ["docs.oracle.com"] and req["last_reviewed"] == "2026-07-27"
    assert set(req) == {"schema_version", "kind", "run_id", "slug", "created_at", "attempts", "url",
                        "domains", "status", "last_reviewed", "review_after_days",
                        "previous_notes"}
    assert request_filename(req) == "source-review-oracle-docs-r9.json"


# ---- every real fixture page sweeps clean -----------------------------------------------

@pytest.mark.parametrize("page", sorted((WIKI / "errors").glob("*.md")), ids=lambda p: p.stem)
def test_fixture_pages_sweep_clean(page):
    req, _ = build_research_request(Cfg(), WIKI, page, "sweep", state_dir=STATE)
    assert not IDENTIFYING.search(json.dumps(req))
