"""Permanent regression table for the redactor and the leak check (issue 08,
verification 2026-09-23).

Three tables:

- LEAKS: inputs that once left the box with an identifier in them. The
  redacted text must not contain any of the listed secrets, and the leak
  check must pass the redacted text.
- KEEPS: public text research depends on (error codes, release numbers,
  approved citations, file names, Oracle object names). It must come out
  byte-identical and the leak check must not flag it.
- UNREDACTED: identifying text as a broken redactor might emit it. The leak
  check alone must flag every row, whatever the redactor's regexes say —
  that is what "independent" means.

Every new leak class found in the field gets a row here."""

import re

import pytest
from fixtures import make_config

from dbwiki import redact
from dbwiki.redact import (Redactor, Vocabulary, _fqdn_like, build_vocabulary,
                           harvest_page, leak_scan)


def vocab() -> Vocabulary:
    v = Vocabulary(source_domains={"docs.oracle.com", "oracle-base.com"})
    for d in ("prodfin9", "cdb1"):
        v.add("DB", d)
    for h in ("lab-dg1.localdomain", "lab-dg1"):
        v.add("HOST", h)
    v.add("SVC", "finsvc")
    v.add("IP", "10.20.30.40")
    return v


# (name, input, secrets that must not survive (case-insensitive))
LEAKS = [
    ("ip at sentence end", "connection from 10.1.2.3.", ["10.1.2.3"]),
    ("ip after underscore", "addr_10.1.2.3 refused", ["10.1.2.3"]),
    ("ip after a dotted word", "peer abc.10.1.2.3 refused", ["10.1.2.3"]),
    ("ipv4-mapped ipv6", "client ::ffff:10.1.2.3 dropped", ["10.1.2.3", "1.2.3"]),
    ("public ip with a version shape", "peer 19.45.0.7 timed out", ["19.45.0.7"]),
    ("descriptor with spaces", "(ADDRESS = (PROTOCOL = TCP)(HOST = dbsrv77)(PORT = 1521))",
     ["dbsrv77"]),
    ("service with spaces", "(CONNECT_DATA = (SERVICE_NAME = payroll_rw))", ["payroll_rw"]),
    ("ORACLE_SID", "ORACLE_SID=payroll2 export", ["payroll2"]),
    ("ORACLE_UNQNAME", "export ORACLE_UNQNAME=payroll2_stby", ["payroll2"]),
    ("db_unique_name with spaces", "DB_UNIQUE_NAME = payroll_stby", ["payroll_stby"]),
    ("quoted host kv", "host='dbsrv77'", ["dbsrv77"]),
    ("audit client user", "CLIENT USER:[4] 'jdoe' CLIENT TERMINAL:[5] 'pts/1'", ["jdoe"]),
    ("audit userhost", 'USERHOST:[7] "dbsrv77"', ["dbsrv77"]),
    ("audit database user", "DATABASE USER:[11] 'PAYROLL_APP'", ["payroll_app"]),
    ("email", "mail sent to jan.novak@bank.cz", ["jan", "novak", "bank.cz"]),
    ("email on a known host", "oracle@lab-dg1.localdomain", ["lab-dg1", "localdomain"]),
    ("connect string", "sqlplus sys/secret@payroll2", ["secret", "payroll2"]),
    ("db + unknown domain", "service prodfin9.bank.cz is down", ["prodfin9", "bank.cz"]),
    ("svc + unknown domain", "finsvc.corp.example refused", ["finsvc", "corp.example"]),
    ("unknown tld", "host dbsrv77.corpx is down", ["dbsrv77", "corpx"]),
    ("unknown tld with port", "dbsrv77.prod.intra:1521", ["dbsrv77", "prod.intra"]),
    ("three-label internal name", "resolved x.prod.intra fine", ["prod.intra"]),
    ("diag path, unknown db", "/app/oracle/diag/rdbms/payroll2/PAYROLL2/trace/alert_PAYROLL2.log",
     ["payroll2"]),
    ("relative diag path", "under diag/rdbms/payroll2/PAYROLL2/trace", ["payroll2"]),
    ("alert log name", "see alert_PAYROLL2.log", ["payroll2"]),
    ("trace file name", "dumped to payroll2_ora_12345.trc and payroll2_lgwr_77.trm",
     ["payroll2"]),
    ("asm path", "+DATA/PAYROLL2/DATAFILE/system.256.1", ["payroll2"]),
    ("windows path", r"D:\app\oracle\oradata\PAYROLL2\system01.dbf", ["payroll2"]),
    ("other absolute path", "restore to /backup/payroll2/full_1.bkp", ["payroll2"]),
    ("user kv, value equal to the key's letters", "(USER=U)", ["=u)"]),
    ("known host, upper case", "LAB-DG1.LOCALDOMAIN:1521", ["lab-dg1"]),
    ("hyphen-split host", "lab-dg1-vip is down", ["lab-dg1"]),
    ("glued host", "lab-dg1vip is down", ["lab-dg1"]),
    ("glued db", "cdb1stby is the standby", ["cdb1"]),
    ("known ip in a url", "http://10.20.30.40:1158/em", ["10.20.30.40"]),
    ("unknown host in a url", "https://dbsrv77.corpx/em", ["dbsrv77"]),
    pytest.param("bare single-label host the pipeline never saw",
                 "Fatal NI connect error 12170 on dbsrv77", ["dbsrv77"],
                 marks=pytest.mark.xfail(strict=True, reason=(
                     "residual, inherent: a one-label name no ES host field, "
                     "descriptor, page or config term has ever named has no "
                     "shape to catch; redact.terms is the lever (the same "
                     "name once observed is HARVESTED below)"))),
]


def _name(case) -> str:
    return case.values[0] if hasattr(case, "values") else case[0]


@pytest.mark.parametrize("name,text,secrets", LEAKS, ids=[_name(c) for c in LEAKS])
def test_leak_is_redacted_and_passes_the_check(name, text, secrets):
    red = Redactor(vocab(), "r")
    out = red.redact(text)
    for s in secrets:
        assert s.lower() not in out.lower(), f"{name}: {s!r} survived in {out!r}"
    assert red.leak_check(out) == [], (name, out)
    # and the check would have refused the raw text
    assert red.leak_check(text), f"{name}: leak check passes the raw input {text!r}"


# host names the compactor observed on ES documents (host.name,
# agent.hostname, listener descriptors, diag paths), as the per-db registry
# holds them; `build_vocabulary` learns them with their short names
HARVESTED_HOSTS = ["dbsrv77", "dbsrv.corpx", "app07.bank.intra", "mercury"]


def harvested_vocab(tmp_path) -> Vocabulary:
    from dbwiki.state import Registry

    reg = Registry(tmp_path / "registry" / "cdb1.json")
    for h in HARVESTED_HOSTS:
        reg.note_host(h, "2026-09-01T00:00:00Z")
    reg.save()
    v = build_vocabulary(make_config(tmp_path, state_dir=tmp_path, redact={}),
                         tmp_path / "wiki")
    v.source_domains |= {"docs.oracle.com", "oracle-base.com"}
    return v


# (name, input, secrets) redacted with the harvested vocabulary: what the
# bare-host residual above cannot catch, a name the pipeline has seen is
HARVESTED = [
    ("bare single-label host the pipeline saw",
     "Fatal NI connect error 12170 on dbsrv77", ["dbsrv77"]),
    ("harvested host, upper case", "DBSRV77 refused", ["dbsrv77"]),
    ("harvested host glued", "dbsrv77-vip and dbsrv77vip", ["dbsrv77"]),
    ("sibling of a harvested host", "failover to dbsrv78", ["dbsrv78"]),
    ("all-caps host shaped like SCHEMA.OBJECT", "connect to DBSRV.CORPX failed",
     ["dbsrv", "corpx"]),
    ("SCHEMA.OBJECT-shaped host in a stack", 'ORA-06512: at "DBSRV.CORPX", line 1',
     ["dbsrv", "corpx"]),
    ("bare first label of a harvested fqdn", "APP07 lost its session", ["app07"]),
    ("sibling under a learned domain", "OTHERSRV.CORPX answered", ["othersrv", "corpx"]),
    ("letters-only harvested host", "mercury is down", ["mercury"]),
    ("program on a harvested host", "sqlplus@dbsrv77", ["dbsrv77"]),
]


@pytest.mark.parametrize("name,text,secrets", HARVESTED, ids=[h[0] for h in HARVESTED])
def test_harvested_host_is_redacted_and_passes_the_check(tmp_path, name, text, secrets):
    red = Redactor(harvested_vocab(tmp_path), "r")
    out = red.redact(text)
    for s in secrets:
        assert s.lower() not in out.lower(), f"{name}: {s!r} survived in {out!r}"
    assert red.leak_check(out) == [], (name, out)
    assert red.leak_check(text), f"{name}: leak check passes the raw input {text!r}"


# (name, text) — must come out unchanged and pass the check
KEEPS = [
    ("error codes", "ORA-12541: TNS:no listener; TNS-12560, ORA-00600 [kdsgrp1]"),
    ("releases", "Version 19.27.0.0.0, RU 19.27.0.0.250415, 12.2.0.1, 11.2.0.4, "
                 "12.1.0.2, 19.0.0.0, 21.3.0.0, 18.3.0.0 and 23.4.0.24.05"),
    ("approved url", "see https://docs.oracle.com/en/error-help/db/ora-12541/ now"),
    ("approved domain, bare", "docs.oracle.com and oracle-base.com/articles/misc"),
    ("oracle files", "check sqlnet.ora, listener.ora, tnsnames.ora, alert.log and spfile.ora"),
    ("plsql object in a stack", 'ORA-06512: at "SYS.DBMS_STATS", line 123'),
    ("schema.object", 'ORA-06512: at "SCOTT.MYPROC", line 5'),
    ("fixed views", "query v$session.sid, x$ksppi.ksppinm and SYS.OBJ$"),
    ("abbreviations", "e.g. set it, i.e. the default"),
    ("package call", "DBMS_SCHEDULER.RUN_JOB failed, DBMS_XPLAN.DISPLAY_CURSOR"),
    ("java namespaces", "java.lang.NullPointerException at oracle.sysman.emSDK.Foo"),
    ("asm file number", "file system.256.1 on +DATA"),
    ("oracle home path", "$ORACLE_HOME/network/admin/sqlnet.ora"),
    ("system paths", "/etc/oratab, /dev/shm and /proc/sys/kernel/sem"),
    ("sysctl", "net.ipv4.ip_local_port_range and kernel.shmmax"),
    ("times and loopback", "from 00:00:09.776Z to 00:14:13.376Z on 0.0.0.0, 127.0.0.1, "
                           "localhost and ::1"),
    ("dedicated server", "(CONNECT_DATA=(SERVER=DEDICATED))"),
    ("container names", "(PDBNAME=CDB$ROOT) then (PDBNAME=PDB$SEED)"),
    ("init parameters", "DB_BLOCK_SIZE, DB_RECOVERY_FILE_DEST_SIZE and DB_UNIQUE_NAME"),
    ("mos note", "Doc ID 1234567.1 and Patch 35643107"),
    ("audit terminal and sys", "CLIENT TERMINAL:[5] 'pts/1' DATABASE USER:[3] 'SYS'"),
    ("asm alert", "alert_+ASM1.log on +ASM1"),
    ("prose", "The listener was restarted. Then the standby caught up."),
    ("ratios", "read/write, TCP/IP, 3/4 of the SGA and ORA-12154/ORA-12514"),
]


@pytest.mark.parametrize("name,text", KEEPS, ids=[k[0] for k in KEEPS])
def test_public_text_survives_and_passes_the_check(name, text):
    red = Redactor(vocab(), "r")
    assert red.redact(text) == text
    assert red.leak_check(text) == []


@pytest.mark.parametrize("name,text", KEEPS + [
    ("words around a letters-only host", "mercurial moods and a Mercurial repo"),
    ("learned suffix, code", "check SYS.CORPX_JOBS and corpx.log"),
], ids=[k[0] for k in KEEPS] + ["words around a letters-only host",
                                 "learned suffix, code"])
def test_public_text_survives_the_harvested_vocabulary(tmp_path, name, text):
    """Learning hosts must not over-redact: error codes, releases, approved
    domains, file names and common words stay byte-identical."""
    red = Redactor(harvested_vocab(tmp_path), "r")
    assert red.redact(text) == text
    assert red.leak_check(text) == []


# identifying text as a broken redactor could emit it: leak_check must flag
# every one of these on its own
UNREDACTED = [
    "connection from 10.1.2.3.",
    "addr_10.1.2.3",
    "client IP_A.1.2.3 dropped",
    "client ::ffff:10.1.2.3",
    "peer fe80::1ff:fe23:4567:890a",
    "peer 19.45.0.7",
    "(HOST = dbsrv77)",
    "(SERVICE_NAME = payroll_rw)",
    "ORACLE_SID=payroll2",
    "DB_UNIQUE_NAME = payroll_stby",
    "CLIENT USER:[4] 'jdoe'",
    "jan.novak@HOST_A",
    "DB_A.bank.cz",
    "dbsrv77.corpx",
    "dbsrv77.prod.intra:1521",
    "http://dbsrv77.corpx:1158/em",
    "/app/oracle/diag/rdbms/payroll2/PAYROLL2/trace",
    "diag/rdbms/payroll2/PAYROLL2",
    "alert_PAYROLL2.log",
    "payroll2_ora_12345.trc",
    "+DATA/PAYROLL2/DATAFILE/system.256.1",
    r"D:\app\oracle\oradata\PAYROLL2",
    "(USER=jdoe)",
    "seen on prodfin9 and lab-dg1",
    "sibling cdb7 and lab-dg4",
]


@pytest.mark.parametrize("text", UNREDACTED)
def test_leak_check_flags_raw_identifiers(text):
    assert leak_scan(text, vocab()), text


def test_leak_check_is_independent_of_the_redactor_patterns(monkeypatch):
    """With every redactor pattern switched off the leak check still sees
    addresses, hosts and key=value names: it does not reuse PATTERNS."""
    monkeypatch.setattr(redact, "PATTERNS", [])
    red = Redactor(vocab(), "r")
    assert red.redact("from 10.1.2.3 on dbsrv77.corpx") == "from 10.1.2.3 on dbsrv77.corpx"
    hits = red.leak_check("from 10.1.2.3 on dbsrv77.corpx (HOST = dbsrv9)")
    assert {"10.1.2.3", "dbsrv77.corpx", "dbsrv9"} <= set(hits)


def test_leak_check_passes_pseudonyms_and_reports_originals():
    red = Redactor(vocab(), "r")
    assert red.leak_check("HOST_A talked to DB_B via SVC_C (IP_A); PATH_A missing") == []
    assert set(red.leak_check({"x": ["seen on LAB-DG1 at 10.1.2.3"]})) == {"LAB-DG1", "10.1.2.3"}


# ---- span-based replacement ---------------------------------------------------

def test_kv_value_replaced_by_span_not_first_occurrence():
    red = Redactor(vocab(), "r")
    assert red.redact("(USER=U)") == "(USER=USER_A)"
    assert red.redact("(HOST=H)(PORT=1)") == "(HOST=HOST_A)(PORT=1)"
    assert red.redact("sid=s") == "sid=DB_A"


# ---- domain suffixes ------------------------------------------------------------

def test_configured_domain_suffixes_extend_the_defaults(tmp_path):
    cfg = make_config(tmp_path,
                      redact={"domain_suffixes": ["corp.example", "Intra2"]})
    v = build_vocabulary(cfg, tmp_path)
    assert "cz" in v.domain_suffixes and "corp.example" in v.domain_suffixes
    assert "intra2" in v.domain_suffixes
    assert _fqdn_like("dbsrv77.corp.example", v.domain_suffixes)
    assert _fqdn_like("dbsrv78.bank.cz", v.domain_suffixes)
    assert not _fqdn_like("corp.example.txt", v.domain_suffixes)
    harvest_page("the primary is dbsrv77.corp.example", v)
    assert {"dbsrv77.corp.example", "dbsrv77"} <= v.hosts
    out = Redactor(v).redact("dbsrv77.corp.example and dbsrv78.bank.cz")
    assert out == "HOST_A and HOST_B"


# ---- de-mapping ------------------------------------------------------------------

def test_demap_restores_glued_pseudonyms_but_not_parameter_names():
    red = Redactor(vocab(), "r")
    out = red.redact("cdb1 on lab-dg1")
    assert out == "DB_A on HOST_A"
    assert red.demap("DB_Astby and HOST_Avip, xDB_A") == "cdb1stby and lab-dg1vip, xcdb1"
    # an Oracle parameter that starts with a pseudonym's letters is left alone
    red.pseudonym("DB", "other")
    assert red.demap("DB_BLOCK_SIZE and DB_A_tt00_1.trc") == "DB_BLOCK_SIZE and cdb1_tt00_1.trc"


def test_glued_names_redact_whole_and_round_trip():
    red = Redactor(vocab(), "r")
    out = red.redact("cdb1stby on lab-dg1vip")
    assert "cdb1" not in out and "lab-dg1" not in out
    assert red.demap(out) == "cdb1stby on lab-dg1vip"


# ---- the fuzz acceptance -----------------------------------------------------------

def test_fuzz_corpus_is_clean_against_a_vocabulary():
    report = redact.fuzz(vocab())
    bad = [r for r in report if not r["ok"]]
    assert report and not bad, bad
    # the live terms were plugged into the templates
    assert any("prodfin9" in r["input"] for r in report)


def test_fuzz_reports_a_leak_when_the_redactor_is_broken(monkeypatch):
    monkeypatch.setattr(Redactor, "redact", lambda self, text: text)
    report = redact.fuzz(vocab())
    assert any(not r["ok"] and r["kind"] == "leak" for r in report)


def test_cli_redact_fuzz_runs_against_the_configured_vocabulary(monkeypatch, capsys):
    from pathlib import Path
    from types import SimpleNamespace

    from dbwiki import cli
    fix = Path(__file__).parent / "fixtures" / "redact"
    monkeypatch.setattr(cli, "load_config", lambda: make_config(
        wiki_repo=fix / "wiki", state_dir=fix / "state", redact={}))
    assert cli.cmd_redact(SimpleNamespace(fuzz=True)) == 0
    assert "probes clean" in capsys.readouterr().out
    monkeypatch.setattr(Redactor, "redact", lambda self, text: text)
    assert cli.cmd_redact(SimpleNamespace(fuzz=True)) == 2
    assert "FAIL [leak]" in capsys.readouterr().out


def test_fuzz_corpus_is_clean_against_a_harvested_vocabulary(tmp_path):
    report = redact.fuzz(harvested_vocab(tmp_path))
    bad = [r for r in report if not r["ok"]]
    assert report and not bad, bad
    assert any("dbsrv77" in r["input"] for r in report)


def test_regression_tables_have_no_duplicates():
    names = [_name(c) for c in LEAKS] + [h[0] for h in HARVESTED]
    assert len(names) == len(set(names))
    assert len({k[0] for k in KEEPS}) == len(KEEPS)
    assert all(re.search(r"\S", t) for t in UNREDACTED)
