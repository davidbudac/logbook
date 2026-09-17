"""Practitioner caveats: which researched pages are due a pass, the prompt
that leaves the box, the strict notes contract, and the deterministic writer
that splices notes into `## Reference` without disturbing anything else.
Never hits the network: harness.run_web_text is always monkeypatched."""

import datetime as dt
import json
from types import SimpleNamespace

import pytest
from fixtures.incident_pages import incident_page

from dbwiki import research_caveats as rc
from dbwiki.harness import HarnessError
from dbwiki.lint import blocking, lint_wiki
from dbwiki.research import unapproved_urls
from dbwiki.structured import ProposalError

TODAY = dt.date(2026, 7, 29)

REFERENCE = (
    "## Reference\n\n"
    "**Cause:** the user interrupted an Oracle operation\n"
    "(source: sources/oracle-docs; url: "
    "https://docs.oracle.com/en/error-help/db/ora-01013/; "
    "accessed: 2026-07-28).\n\n"
    "**Action:** continue with the next operation\n"
    "(source: sources/oracle-docs; url: "
    "https://docs.oracle.com/en/error-help/db/ora-01013/; "
    "accessed: 2026-07-28).\n\n"
    "Practitioner caveat: a hand-written note nobody generated, wrapped\n"
    "across two physical lines\n"
    "(source: sources/jonathan-lewis; url: "
    "https://jonathanlewis.wordpress.com/2021/01/13/check-constraints/; "
    "accessed: 2026-07-28).\n")

ERROR_PAGE = ("---\ntype: error-class\nupdated: 2026-07-10T00:00:00Z\n"
              "researched: 2026-07-28\n---\n\n"
              "# ORA-1013\n\n## Occurrences\n\n"
              "| day | db | note | evidence |\n|---|---|---|---|\n"
              "| 2026-07-10 | cdb1 | seen | digests/cdb1/2026-07-10.md |\n\n"
              + REFERENCE)

SOURCES = [("jonathan-lewis", "2", ["jonathanlewis.wordpress.com"]),
           ("oracle-docs", "official", ["docs.oracle.com"])]

NOTE_URL = "https://jonathanlewis.wordpress.com/2021/01/13/check-constraints/"


def source_page(tier="official", domains="[docs.oracle.com]",
                status="approved", fetchable="true") -> str:
    return (f"---\ntype: source\nstatus: {status}\ntier: {tier}\n"
            f"domains: {domains}\nfetchable: {fetchable}\nadded: 2026-01-01\n"
            f"last_reviewed: 2026-07-01\nreview_after_days: 180\n---\n\n"
            f"# a source\n")


def page(path, fm: str, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}\n---\n\n{body}")


def researched_page(code: str, *, updated: str, caveats: str = "") -> str:
    fm = (f"type: error-class\nupdated: {updated}\nresearched: 2026-07-28"
          + (f"\ncaveats_reviewed: {caveats}" if caveats else ""))
    return f"---\n{fm}\n---\n\n# {code}\n\n{REFERENCE}"


@pytest.fixture
def wiki(tmp_path):
    w = tmp_path / "wiki"
    (w / "errors").mkdir(parents=True)
    (w / "sources").mkdir()
    (w / "incidents").mkdir()
    (w / "sources" / "oracle-docs.md").write_text(source_page())
    (w / "sources" / "jonathan-lewis.md").write_text(
        source_page(tier="2", domains="[jonathanlewis.wordpress.com]"))
    return w


def active(wiki, name: str, *codes: str) -> None:
    (wiki / "incidents" / f"2026-07-01-cdb1-{name}.md").write_text(
        incident_page("cdb1", name, error_codes=codes))


# ---- select_caveat_candidates ------------------------------------------------

def test_selects_only_researched_pages_an_active_incident_links(wiki):
    (wiki / "errors" / "ORA-1.md").write_text(
        researched_page("ORA-1", updated="2026-07-10T00:00:00Z"))
    page(wiki / "errors" / "ORA-2.md",
         "type: error-class\nupdated: 2026-07-20T00:00:00Z")
    (wiki / "errors" / "ORA-3.md").write_text(
        researched_page("ORA-3", updated="2026-07-20T00:00:00Z"))
    active(wiki, "a", "ORA-1", "ORA-2")
    assert [p.stem for p in rc.select_caveat_candidates(wiki, 5, TODAY)] == \
        ["ORA-1"]


def test_resolved_incidents_do_not_make_a_page_due(wiki):
    (wiki / "errors" / "ORA-1.md").write_text(
        researched_page("ORA-1", updated="2026-07-10T00:00:00Z"))
    (wiki / "incidents" / "2026-07-01-cdb1-z.md").write_text(
        incident_page("cdb1", "z", status="resolved", error_codes=("ORA-1",)))
    assert rc.select_caveat_candidates(wiki, 5, TODAY) == []


def test_caveats_reviewed_within_the_interval_is_not_due(wiki):
    (wiki / "errors" / "ORA-1.md").write_text(researched_page(
        "ORA-1", updated="2026-07-10T00:00:00Z", caveats="2026-07-01"))
    (wiki / "errors" / "ORA-2.md").write_text(researched_page(
        "ORA-2", updated="2026-07-10T00:00:00Z", caveats="2026-01-01"))
    (wiki / "errors" / "ORA-3.md").write_text(researched_page(
        "ORA-3", updated="2026-07-10T00:00:00Z", caveats="not a date"))
    active(wiki, "a", "ORA-1", "ORA-2", "ORA-3")
    due = [p.stem for p in rc.select_caveat_candidates(wiki, 5, TODAY)]
    assert due == ["ORA-2", "ORA-3"]

    old = TODAY - dt.timedelta(days=1)
    boundary = old - dt.timedelta(days=rc.CAVEATS_REVIEW_DAYS)
    (wiki / "errors" / "ORA-1.md").write_text(researched_page(
        "ORA-1", updated="2026-07-10T00:00:00Z", caveats=boundary.isoformat()))
    assert "ORA-1" in [p.stem for p in rc.select_caveat_candidates(wiki, 5, TODAY)]


def test_active_incident_count_outranks_recency_then_limit(wiki):
    for code, updated in (("ORA-1", "2026-07-01T00:00:00Z"),
                          ("ORA-2", "2026-07-20T00:00:00Z"),
                          ("ORA-3", "2026-07-10T00:00:00Z")):
        (wiki / "errors" / f"{code}.md").write_text(
            researched_page(code, updated=updated))
    active(wiki, "a", "ORA-1")
    active(wiki, "b", "ORA-1")
    active(wiki, "c", "ORA-2", "ORA-3")
    assert [p.stem for p in rc.select_caveat_candidates(wiki, 5, TODAY)] == \
        ["ORA-1", "ORA-2", "ORA-3"]
    assert [p.stem for p in rc.select_caveat_candidates(wiki, 2, TODAY)] == \
        ["ORA-1", "ORA-2"]


# ---- approved_fetchable_sources ----------------------------------------------

def test_approved_fetchable_sources_are_path_sorted_and_lowercased(wiki):
    (wiki / "sources" / "blog.md").write_text(
        source_page(tier="3", domains="[Blog.Example.COM]"))
    (wiki / "sources" / "deprecated.md").write_text(
        source_page(status="deprecated"))
    (wiki / "sources" / "unfetchable.md").write_text(
        source_page(fetchable="false"))
    assert rc.approved_fetchable_sources(wiki) == [
        ("blog", "3", ["blog.example.com"]),
        ("jonathan-lewis", "2", ["jonathanlewis.wordpress.com"]),
        ("oracle-docs", "official", ["docs.oracle.com"])]


# ---- build_caveats_prompt ----------------------------------------------------

def test_prompt_lists_every_approved_domain_and_the_oracle_text():
    prompt = rc.build_caveats_prompt("ORA-1013", "the user interrupted",
                                     "continue", SOURCES)
    assert "ORA-1013" in prompt
    assert "the user interrupted" in prompt and "continue" in prompt
    for slug, tier, domains in SOURCES:
        assert slug in prompt and f"tier {tier}" in prompt
        for domain in domains:
            assert domain in prompt
    assert '"notes"' in prompt
    assert str(rc.MAX_NOTE_CHARS) in prompt


def test_prompt_carries_no_database_name_or_hostname_from_the_wiki(wiki):
    """ADR-0002's concern: the prompt is built from the error code and the
    page's published Oracle text, never from the page's occurrences."""
    (wiki / "errors" / "ORA-1013.md").write_text(ERROR_PAGE)
    prompt = rc.build_caveats_prompt(
        "ORA-1013", "the user interrupted an Oracle operation",
        "continue with the next operation", rc.approved_fetchable_sources(wiki))
    for secret in ("cdb1", "digests/", "Occurrences", "2026-07-10"):
        assert secret not in prompt


# ---- parse_caveats_proposal --------------------------------------------------

def note(**over) -> dict:
    base = {"text": "A gotcha Oracle does not mention.",
            "source": "jonathan-lewis", "url": NOTE_URL}
    base.update(over)
    return base


def test_parse_accepts_notes_and_an_empty_list():
    got = rc.parse_caveats_proposal(json.dumps({"notes": [note()]}), SOURCES)
    assert got == {"notes": [note()]}
    assert rc.parse_caveats_proposal(json.dumps({"notes": []}), SOURCES) == \
        {"notes": []}
    assert rc.parse_caveats_proposal("{}", SOURCES) == {"notes": []}
    assert rc.parse_caveats_proposal(
        'here you go: {"notes": []} — done', SOURCES) == {"notes": []}


def test_parse_flattens_cosmetic_whitespace_and_pipes():
    got = rc.parse_caveats_proposal(
        json.dumps({"notes": [note(text="line one\n  line  two | three")]}),
        SOURCES)
    assert got["notes"][0]["text"] == "line one line two \\| three"


@pytest.mark.parametrize("bad,message", [
    (json.dumps({"notes": {"a": 1}}), "notes"),
    (json.dumps({"notes": ["a string"]}), "notes[0]"),
    (json.dumps({"notes": [note(source="nobody")]}), "notes[0].source"),
    (json.dumps({"notes": [note(url="https://evil.example.com/x")]}),
     "notes[0].url"),
    (json.dumps({"notes": [note(url="https://docs.oracle.com/x")]}),
     "notes[0].url"),
    (json.dumps({"notes": [note(text="## a heading and prose")]}),
     "notes[0].text"),
    (json.dumps({"notes": [note(text="see [[errors/ORA-600]]")]}),
     "notes[0].text"),
    (json.dumps({"notes": [note(text="x" * (rc.MAX_NOTE_CHARS + 1))]}),
     "notes[0].text"),
    (json.dumps({"notes": [note(text=17)]}), "notes[0].text"),
    (json.dumps({"notes": [note(text="")]}), "notes[0].text"),
    (json.dumps({"notes": [note()] * (rc.MAX_NOTES + 1)}), "notes"),
    ("not json at all", "no JSON object"),
    ("[]", "no JSON object"),
    ('{"notes": [], ', "not closed"),
])
def test_parse_rejects_anything_off_contract(bad, message):
    with pytest.raises(ProposalError, match=message.replace("[", r"\[")):
        rc.parse_caveats_proposal(bad, SOURCES)


# ---- propose_caveats ---------------------------------------------------------

def fake_web(monkeypatch, wiki, *answers) -> list[tuple[str, str | None]]:
    seen: list[tuple[str, str | None]] = []

    def _run(prompt, model, timeout, *, telemetry=None):
        seen.append((prompt, model))
        if telemetry is not None:
            telemetry.update(adapter="claude", model=model, duration_s=0.4,
                             exit_code=0, timed_out=False, usage="unknown")
        return answers[min(len(seen) - 1, len(answers) - 1)]

    monkeypatch.setattr(rc, "run_web_text", _run)
    return seen


def cfg_for(wiki, **research):
    return SimpleNamespace(wiki_repo=wiki,
                           agents={"claude": {"cheap": "sonnet"}},
                           research=research)


def test_propose_uses_the_configured_model_and_takes_the_first_answer(
        monkeypatch, wiki):
    seen = fake_web(monkeypatch, wiki, json.dumps({"notes": [note()]}))
    tele = {}
    got = rc.propose_caveats("PROMPT", cfg_for(wiki, caveats={"model": "opus"}),
                             telemetry=tele)
    assert got == {"notes": [note()]}
    assert seen == [("PROMPT", "opus")]
    assert tele["attempts"] == 1 and tele["adapter"] == "claude"


def test_propose_falls_back_to_the_claude_cheap_tier(monkeypatch, wiki):
    seen = fake_web(monkeypatch, wiki, json.dumps({"notes": []}))
    rc.propose_caveats("PROMPT", cfg_for(wiki))
    assert seen[0][1] == "sonnet"


def test_propose_retries_once_with_the_error_appended(monkeypatch, wiki):
    seen = fake_web(monkeypatch, wiki, json.dumps({"notes": [note(source="x")]}),
                    json.dumps({"notes": []}))
    tele = {}
    assert rc.propose_caveats("PROMPT", cfg_for(wiki), telemetry=tele) == \
        {"notes": []}
    assert len(seen) == 2
    assert "rejected" in seen[1][0] and "notes[0].source" in seen[1][0]
    assert tele["attempts"] == 2


def test_propose_gives_up_after_a_second_bad_answer(monkeypatch, wiki):
    fake_web(monkeypatch, wiki, "nope", "still nope")
    with pytest.raises(HarnessError, match="invalid structured proposal"):
        rc.propose_caveats("PROMPT", cfg_for(wiki))


# ---- apply_caveats -----------------------------------------------------------

@pytest.fixture
def caveat_wiki(wiki):
    (wiki / "errors" / "ORA-1013.md").write_text(ERROR_PAGE)
    (wiki / "digests" / "cdb1").mkdir(parents=True)
    (wiki / "digests" / "cdb1" / "2026-07-10.md").write_text("# digest\n")
    return wiki


def apply(wiki, *notes) -> str:
    rc.apply_caveats(wiki, "errors/ORA-1013.md", {"notes": list(notes)}, TODAY)
    return (wiki / "errors" / "ORA-1013.md").read_text()


def test_apply_writes_the_note_paragraph_and_the_frontmatter_date(caveat_wiki):
    text = apply(caveat_wiki, note())
    assert "caveats_reviewed: 2026-07-29" in text
    assert ("**Practitioner note:** A gotcha Oracle does not mention.\n"
            f"(source: sources/jonathan-lewis; url: {NOTE_URL}; "
            "accessed: 2026-07-29).") in text
    assert unapproved_urls(text, {"docs.oracle.com",
                                  "jonathanlewis.wordpress.com"}) == set()
    assert blocking(lint_wiki(caveat_wiki,
                              only_paths=["errors/ORA-1013.md"])) == []


def test_apply_leaves_every_other_paragraph_byte_identical(caveat_wiki):
    text = apply(caveat_wiki, note(), note(text="A second note."))
    for para in REFERENCE.split("\n\n")[1:]:
        assert para.rstrip("\n") in text
    assert "| 2026-07-10 | cdb1 | seen | digests/cdb1/2026-07-10.md |" in text
    assert text.index("**Cause:**") < text.index("**Practitioner note:**")
    assert text.index("Practitioner caveat:") < text.index("**Practitioner note:**")


def test_apply_replaces_its_own_notes_and_is_idempotent(caveat_wiki):
    first = apply(caveat_wiki, note())
    assert apply(caveat_wiki, note()) == first
    assert first.count("**Practitioner note:**") == 1

    second = apply(caveat_wiki, note(text="A revised note."))
    assert second.count("**Practitioner note:**") == 1
    assert "A revised note." in second
    assert "A gotcha Oracle does not mention." not in second


def test_apply_with_no_notes_still_records_the_review(caveat_wiki):
    text = apply(caveat_wiki)
    assert "caveats_reviewed: 2026-07-29" in text
    assert "**Practitioner note:**" not in text
    assert text.split("## Reference")[1] == \
        ERROR_PAGE.split("## Reference")[1]
    log = (caveat_wiki / "log.md").read_text()
    assert "[2026-07-29] research (caveats) — errors/ORA-1013.md: 0 note(s)" \
        in log


def test_apply_removes_a_note_a_later_run_no_longer_proposes(caveat_wiki):
    apply(caveat_wiki, note())
    text = apply(caveat_wiki)
    assert "**Practitioner note:**" not in text
    assert "Practitioner caveat:" in text


def test_apply_does_not_move_the_sections_after_reference(caveat_wiki):
    page_path = caveat_wiki / "errors" / "ORA-1013.md"
    page_path.write_text(ERROR_PAGE + "\n## Notes\n\n- a human's own list\n")
    text = apply(caveat_wiki, note())
    assert text.endswith("## Notes\n\n- a human's own list\n")
    assert text.index("**Practitioner note:**") < text.index("## Notes")


def test_apply_log_line_counts_the_notes(caveat_wiki):
    (caveat_wiki / "log.md").write_text("# log\n")
    apply(caveat_wiki, note(), note(text="A second note."))
    log = (caveat_wiki / "log.md").read_text()
    assert "[2026-07-29] research (caveats) — errors/ORA-1013.md: 2 note(s)" \
        in log


# ---- the CLI verb ------------------------------------------------------------

def cli_args(**over):
    base = {"limit": None, "dry_run": True}
    base.update(over)
    return SimpleNamespace(**base)


def test_cli_refuses_while_the_config_key_is_off(wiki, capsys):
    from dbwiki.cli import _run_caveats
    cfg = SimpleNamespace(research={"caveats": {"enabled": False}},
                          wiki_repo=wiki, state_dir=wiki / ".state")
    assert _run_caveats(cli_args(), cfg, None, wiki) == 1
    assert "research.caveats.enabled" in capsys.readouterr().err


def test_cli_dry_run_prints_the_candidates_and_the_first_prompt(wiki, capsys):
    """The dry run is the operator's view of exactly what would leave the
    box, and it costs no call and no write."""
    from dbwiki.cli import _run_caveats
    (wiki / "errors" / "ORA-1013.md").write_text(ERROR_PAGE)
    active(wiki, "a", "ORA-1013")
    cfg = SimpleNamespace(research={"caveats": {"enabled": True, "limit": 2}},
                          wiki_repo=wiki, state_dir=wiki / ".state")
    assert _run_caveats(cli_args(), cfg, None, wiki) == 0
    out = capsys.readouterr().out
    assert "- errors/ORA-1013.md" in out
    assert "You are researching Oracle error ORA-1013" in out
    assert "the user interrupted an Oracle operation" in out
    assert "jonathanlewis.wordpress.com" in out and "docs.oracle.com" in out
    assert "cdb1" not in out
    assert not (wiki / "log.md").exists()


def test_cli_says_nothing_is_due_when_no_page_qualifies(wiki, capsys):
    from dbwiki.cli import _run_caveats
    cfg = SimpleNamespace(research={"caveats": {"enabled": True}},
                          wiki_repo=wiki, state_dir=wiki / ".state")
    assert _run_caveats(cli_args(), cfg, None, wiki) == 0
    assert "nothing to review" in capsys.readouterr().out
