"""AWR window checks and Kibana deep-link quoting (verification 2026-09-23,
#22, ingest part).

An AWR window's end must be later than its begin as instants, not as
strings; its digest day and file name are UTC; its `db` becomes a directory
name and must be a safe id; and the digest pair is written atomically. A
Kibana link's values are percent-encoded, but the browser decodes them
before Kibana parses the rison/KQL inside, so a quote or `!` in a value must
be escaped for those syntaxes too."""

import json
import re
import urllib.parse

import pytest

import fixtures as fx
from dbwiki import awr, state
from dbwiki.awr import AwrContractError, awr_digest, emit_awr_digest, validate_awr_summary
from dbwiki.deeplink import Resolver
from dbwiki.evidence_ref import Entity, EvidenceRef, Signature, Window

VALID = fx.AWR_DIR / "valid.json"


def summary(**snap) -> dict:
    s = json.loads(VALID.read_text())
    s["snapshot"] = {**s["snapshot"], **snap}
    return s


# ---- AWR -------------------------------------------------------------------------

def test_an_offset_window_that_ends_later_in_utc_is_accepted():
    # 23:30+02:00 is 21:30Z, before 22:00Z
    validate_awr_summary(summary(begin_time="2026-07-12T23:30:00+02:00",
                                 end_time="2026-07-12T22:00:00Z"))


def test_an_offset_window_that_ends_earlier_in_utc_is_rejected():
    # 11:00+02:00 is 09:00Z, before 10:00Z
    with pytest.raises(AwrContractError, match="snapshot.end_time"):
        validate_awr_summary(summary(begin_time="2026-07-12T10:00:00Z",
                                     end_time="2026-07-12T11:00:00+02:00"))


def test_the_digest_day_and_window_are_utc():
    d = awr_digest(summary(begin_time="2026-07-13T01:30:00+02:00",
                           end_time="2026-07-13T02:30:00+02:00"))
    assert d["window"] == {"from": "2026-07-12T23:30:00Z",
                           "to": "2026-07-13T00:30:00Z", "day": "2026-07-12"}
    # the snapshot block keeps what the file said
    assert d["awr"]["snapshot"]["begin_time"] == "2026-07-13T01:30:00+02:00"


def test_a_utc_window_is_unchanged():
    d = awr_digest(summary())
    assert d["window"] == {"from": "2026-07-12T10:00:00Z",
                           "to": "2026-07-12T11:00:00Z", "day": "2026-07-12"}


@pytest.mark.parametrize("db", ["../cdb1", "cdb1/x", ".hidden", "a b", "..", ""])
def test_a_db_that_is_not_a_safe_directory_name_is_rejected(db):
    s = summary()
    s["db"] = db
    with pytest.raises(AwrContractError, match="'db'"):
        validate_awr_summary(s)


def test_emit_writes_both_files_atomically(tmp_path, monkeypatch):
    cfg = fx.fixture_config(tmp_path)
    cfg.wiki_repo.mkdir(parents=True)
    written = []
    real = state.atomic_write_text
    monkeypatch.setattr(state, "atomic_write_text",
                        lambda p, t: written.append(p.name) or real(p, t))
    jp, mp = emit_awr_digest(cfg, awr_digest(summary()))
    assert written == [jp.name, mp.name]
    assert json.loads(jp.read_text())["db"] == "cdb1"
    assert awr.digest_paths(cfg, awr_digest(summary()))[0] == jp


# ---- deep links ------------------------------------------------------------------

LINKS = {"kibana": {"base": "https://kibana.example.invalid",
                    "data_views": {"oracle-logs": "dv-oracle", "dbwiki": "dv-dbwiki"}}}
RESOLVER = Resolver.from_config(LINKS)


def ref(db: str, sig: str) -> EvidenceRef:
    return EvidenceRef(
        schema_version=1, kind="elastic_filter",
        template_id="oracle-error-context-v1", environment="production",
        data_view="oracle-logs", entity=Entity("database", db),
        signature=Signature("oracle_error", sig),
        window=Window("2026-08-30T10:00:00Z", "2026-08-30T10:20:00Z"),
        representative_document_id="x", summary="")


def rison_query(url: str) -> str:
    """The rison string Kibana sees as `query:'...'`, decoded like a browser
    would, with rison's own escapes (`!!`, `!'`) resolved."""
    decoded = urllib.parse.unquote(url)
    m = re.search(r"query:\(language:kuery,query:'((?:[^'!]|!.)*)'\)", decoded)
    assert m, decoded
    return re.sub(r"!(.)", r"\1", m.group(1))


def test_quotes_and_bangs_stay_inside_the_rison_and_kql_strings():
    url = RESOLVER.logs(ref("cdb'1", 'ORA-1"x!y\\z')).url
    assert rison_query(url) == '"cdb\'1" and "ORA-1\\"x!y\\\\z"'


def test_ordinary_values_are_unchanged():
    url = RESOLVER.logs(ref("cdb1", "ORA-12543")).url
    assert rison_query(url) == '"cdb1" and "ORA-12543"'
    assert "query:'%22cdb1%22%20and%20%22ORA-12543%22'" in url


def test_a_run_id_with_a_quote_stays_inside_the_run_history_query():
    url = RESOLVER.run_history(run_id="r'1\"!").url
    assert rison_query(url) == 'run_id:"r\'1\\"!"'
