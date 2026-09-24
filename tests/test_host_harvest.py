"""Host names the pipeline observes are learned into the redaction
vocabulary (ADR-0002): the compactor records the host fields of every ES
document it reads in the per-db registry, and `build_vocabulary` turns them
into HOST terms. Deterministic, no model, no extra ES query."""

import json

import fixtures as fx
import pytest

from dbwiki.normalize import host_name_like, observed_hosts
from dbwiki.redact import Redactor, build_vocabulary, print_fuzz
from dbwiki.state import Registry


# ---- the guard ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    ("dbsrv77", "dbsrv77"),
    ("DBSRV77.CORPX.", "dbsrv77.corpx"),
    (" lab-dg1.localdomain ", "lab-dg1.localdomain"),
    ("app07", "app07"),
    ("PC_JDOE", "pc_jdoe"),
])
def test_host_name_like_accepts_host_names(raw, expect):
    assert host_name_like(raw) == expect


@pytest.mark.parametrize("raw", [
    "", None, 42, {"name": "x"}, "ab", "db", "12345", "1.2", "10.1.2.3",
    "fe80::1", "::1", "localhost", "LOCALHOST.localdomain", "localhost6",
    "__jdbc__", "unknown", "oracle", "19c", "23ai", "a b", "x/y", "HOST=x",
    "-", "(none)", "x" * 254,
])
def test_host_name_like_rejects_what_is_not_a_host(raw):
    assert host_name_like(raw) == ""


# ---- what a document offers ---------------------------------------------------

def test_observed_hosts_reads_host_fields_paths_and_descriptors():
    hit = {"_source": {
        "host": {"name": "lab-dg1.localdomain", "hostname": "lab-dg1"},
        "agent": {"hostname": "ship01"},
        "log": {"file": {"path": "/u01/app/oracle/diag/tnslsnr/dbsrv77/listener/trace/listener.log"}},
    }}
    ev = {"message": "Fatal NI connect error 12170, (ADDRESS=(PROTOCOL=TCP)"
                     "(HOST = dbsrv78)(PORT=1521)) (HOST=10.1.2.3)",
          "client_host": "app07"}
    assert observed_hosts(hit, ev) == ["lab-dg1.localdomain", "lab-dg1", "ship01",
                                       "dbsrv77", "dbsrv78", "app07"]


def test_observed_hosts_flat_keys_string_host_and_junk():
    hit = {"_source": {"host.name": "DBSRV.CORPX", "host": "dbsrv.corpx",
                       "agent.hostname": "localhost",
                       "log": {"file": {"path": "/u01/app/oracle/diag/rdbms/cdb1/cdb1/trace/alert_cdb1.log"}}}}
    assert observed_hosts(hit, {"message": "", "client_host": "__jdbc__"}) == ["dbsrv.corpx"]
    assert observed_hosts({"_source": {}}) == []


# ---- the registry --------------------------------------------------------------

def test_registry_without_hosts_still_loads_and_stays_without(tmp_path):
    """Backward compat: a registry written before the field existed loads
    with no hosts, and a save that learned none does not add the key."""
    p = tmp_path / "cdb1.json"
    p.write_text(json.dumps({"schema_version": 2, "codes": {}, "services": {},
                             "programs": {}, "daily_counts": {}, "legacy_codes": {}}))
    reg = Registry(p)
    assert reg.hosts == {}
    reg.save()
    assert "hosts" not in json.loads(p.read_text())


def test_registry_notes_hosts_idempotently(tmp_path):
    p = tmp_path / "cdb1.json"
    for _ in range(2):
        reg = Registry(p)
        reg.note_host("dbsrv77", "2026-07-10T10:00:00Z")
        reg.note_host("dbsrv77", "2026-07-10T12:00:00Z")
        reg.note_host("", "2026-07-10T12:00:00Z")
        reg.save()
    assert json.loads(p.read_text())["hosts"] == {
        "dbsrv77": {"first_seen": "2026-07-10T10:00:00Z",
                    "last_seen": "2026-07-10T12:00:00Z"}}


def test_compactor_learns_client_hosts_but_not_addresses(tmp_path):
    cfg = fx.fixture_config(tmp_path)
    case = fx.load_case("listener_establish_fail")
    fx.seed_registry(cfg, case)
    fx.compact_case(cfg, case)
    hosts = Registry(cfg.state_dir / "registry" / f"{case['db']}.json").hosts
    assert {"app07", "app01"} <= set(hosts)
    assert not any(h[0].isdigit() and "." in h for h in hosts)   # no 10.1.4.21


def test_explain_compaction_learns_nothing(tmp_path):
    cfg = fx.fixture_config(tmp_path)
    case = fx.load_case("listener_establish_fail")
    fx.compact_case(cfg, case, persist=False)
    assert not (cfg.state_dir / "registry" / f"{case['db']}.json").exists()


def test_metric_documents_contribute_host_name(tmp_path):
    cfg = fx.metric_fixture_config(tmp_path)
    case = fx.load_metric_case("metrics_db_identity")
    fx.compact_case(cfg, case)
    hosts = Registry(cfg.state_dir / "registry" / f"{case['db']}.json").hosts
    assert "lab-dg1.localdomain" in hosts


# ---- into the vocabulary -------------------------------------------------------

def Cfg(state_dir, terms=()):
    return fx.make_config(
        state_dir, state_dir=state_dir, redact={"terms": list(terms)},
        elasticsearch={"url": "http://es-node1.localdomain:9200"},
        sources={"alert": {"index_patterns": ["oracle-logs-alert-*"]}})


def _registry(state_dir, db, hosts):
    reg = Registry(state_dir / "registry" / f"{db}.json")
    for h in hosts:
        reg.hosts[h] = {"first_seen": "2026-09-01T00:00:00Z",
                        "last_seen": "2026-09-01T00:00:00Z"}
    reg.save()


def test_vocabulary_learns_registry_hosts_with_short_names(tmp_path):
    _registry(tmp_path, "cdb1", ["dbsrv77.corpx", "app07", "mercury"])
    v = build_vocabulary(Cfg(tmp_path), tmp_path / "wiki", tmp_path)
    assert {"dbsrv77.corpx", "dbsrv77", "app07", "mercury"} <= v.hosts
    # the observed FQDN's domain is a known suffix from now on
    assert "corpx" in v.domain_suffixes


def test_vocabulary_guards_harvested_junk(tmp_path):
    """Hand-edited or old registry values go through the same guard, plus
    the redactor's own tables of names that identify nothing."""
    junk = ["localhost", "12", "db", "123456", "oracle", "19c", "sqlplus",
            "sys", "listener", "trc", "10.1.2.3", "log.trc", "docs.oracle.com",
            "docs", "www", "db.corpx"]
    _registry(tmp_path, "cdb1", junk)
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "wiki" / "sources" / "oracle-docs.md").write_text(
        "---\ntype: source\nstatus: approved\ndomains: [docs.oracle.com]\n---\n")
    v = build_vocabulary(Cfg(tmp_path), tmp_path / "wiki", tmp_path)
    assert v.hosts & set(junk) == {"db.corpx"}
    assert "db" not in v.hosts                 # first label too short
    assert "log" not in v.domain_suffixes and "trc" not in v.domain_suffixes


def test_harvested_registry_hosts_may_be_a_list(tmp_path):
    p = tmp_path / "registry" / "cdb1.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"schema_version": 2, "hosts": ["dbsrv77"]}))
    v = build_vocabulary(Cfg(tmp_path), tmp_path / "wiki", tmp_path)
    assert "dbsrv77" in v.hosts


def test_vocabulary_counts_terms_per_source(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "hosts").mkdir(parents=True)
    (wiki / "hosts" / "lab-dg1.md").write_text("---\nhost: lab-dg1\n---\n")
    _registry(tmp_path, "cdb1", ["dbsrv77", "lab-dg1"])
    v = build_vocabulary(Cfg(tmp_path, terms=["site-berlin"]), wiki, tmp_path)
    src = v.sources()
    assert list(src) == ["wiki", "registry", "harvested", "config", "derived"]
    assert src["harvested"] == 2              # dbsrv77 + lab-dg1 (also a wiki term)
    assert src["wiki"] == 1 and src["registry"] == 1        # lab-dg1 / cdb1
    assert src["config"] == 3                 # site-berlin, ES host, index prefix
    assert src["derived"] > 0                 # cdb1_dgmgrl, dbsrv#, …


def test_fuzz_output_reports_vocabulary_sources(tmp_path, capsys):
    import sys
    _registry(tmp_path, "cdb1", ["dbsrv77"])
    v = build_vocabulary(Cfg(tmp_path), tmp_path / "wiki", tmp_path)
    from dbwiki.redact import fuzz
    assert print_fuzz(fuzz(v), sys.stdout, v) == 0
    out = capsys.readouterr().out
    assert "vocabulary sources: wiki 0, registry 1, harvested 1, config 2" in out


def test_letters_only_harvested_host_matches_whole_words(tmp_path):
    """A harvested name with no digit or hyphen may be an English word:
    it is redacted where it stands alone or carries a domain, never inside
    another word."""
    _registry(tmp_path, "cdb1", ["mercury"])
    v = build_vocabulary(Cfg(tmp_path), tmp_path / "wiki", tmp_path)
    red = Redactor(v)
    assert red.redact("mercury is down, mercury.corpx too") == "HOST_A is down, HOST_B too"
    assert red.redact("mercurial behaviour") == "mercurial behaviour"
    assert red.leak_check("mercurial behaviour") == []
    assert red.leak_check("seen on MERCURY") == ["MERCURY"]


def test_a_wiki_host_keeps_substring_matching_when_also_harvested(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "hosts").mkdir(parents=True)
    (wiki / "hosts" / "mercury.md").write_text("---\nhost: mercury\n---\n")
    _registry(tmp_path, "cdb1", ["mercury"])
    v = build_vocabulary(Cfg(tmp_path), wiki, tmp_path)
    assert "mercury" not in v.words
    assert Redactor(v).redact("mercuryvip is down") == "HOST_A is down"


def test_every_outbound_builder_sees_harvested_hosts(tmp_path):
    """The caveats prompt goes through the same vocabulary builder as the
    offload request, so a harvested host is redacted there too."""
    from dbwiki.research_caveats import outbound_caveats_prompt
    _registry(tmp_path, "cdb1", ["dbsrv77"])
    cfg = Cfg(tmp_path)
    cfg.wiki_repo = tmp_path / "wiki"
    prompt, red = outbound_caveats_prompt(
        cfg, "ORA-12170", "Timeout connecting on dbsrv77.",
        "Check the firewall on dbsrv77.", [])
    assert "dbsrv77" not in prompt.lower() and "HOST_A" in prompt
