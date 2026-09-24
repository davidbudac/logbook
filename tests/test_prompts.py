"""`dbwiki.prompts`: the loader for `config/prompts/`, and the generated
AGENTS.md section that keeps the shared rules in the same words in both
modes. Byte-identity of what the builders send is `test_prompt_snapshots`."""

from pathlib import Path

import pytest

from dbwiki import prompts, structured

REPO = Path(__file__).resolve().parent.parent
TEMPLATE_AGENTS = REPO / "wiki-template" / "AGENTS.md"


@pytest.fixture
def prompt_dir(tmp_path, monkeypatch):
    """A prompt root nobody else reads, with the cache cleared around it."""
    monkeypatch.setattr(prompts, "root", lambda: tmp_path)
    prompts.load.cache_clear()
    yield tmp_path
    prompts.load.cache_clear()


def test_load_drops_exactly_one_trailing_newline(prompt_dir):
    (prompt_dir / "a.md").write_text("line\n\n")
    (prompt_dir / "b.md").write_text("no newline")
    assert prompts.load("a") == "line\n"
    assert prompts.load("b") == "no newline"


def test_a_missing_prompt_file_fails_loudly_naming_it(prompt_dir):
    with pytest.raises(FileNotFoundError, match=r"prompt file missing: .*nope\.md"):
        prompts.load("nope")


def test_the_checkout_holds_every_block_the_modules_load():
    assert prompts.root() == REPO / "config" / "prompts"
    for name in ("ingest", "report", "report-contract", "research",
                 "research-caveats", "review-synthesis", "advisory/rules",
                 *prompts.SHARED):
        assert prompts.load(name)


def test_the_ingest_template_ends_with_the_shared_evidence_rules():
    assert structured.INGEST_TEMPLATE.endswith(
        "\n" + prompts.load("shared/evidence-rules"))


# ---- the generated AGENTS.md section ---------------------------------------------

def test_the_wiki_template_carries_every_shared_block_without_drift():
    text = TEMPLATE_AGENTS.read_text()
    for name in prompts.SHARED:
        assert prompts.section(name) in text
    assert prompts.drift(text) == []


def test_a_wiki_without_markers_has_no_finding():
    """The live wiki is not broken until its operator adopts the section."""
    assert prompts.drift("# Logbook — Agent Schema\n\nno markers here\n") == []


def test_an_edited_section_is_drift_and_sync_repairs_it():
    good = "# A\n\n" + prompts.section("shared/evidence-rules") + "\n\n## B\n"
    bad = good.replace("never count it as one", "count it twice")
    assert len(prompts.drift(bad)) == 1
    assert "shared/evidence-rules" in prompts.drift(bad)[0]
    assert prompts.sync(bad) == good
    assert prompts.sync(good) == good


def test_an_unknown_block_and_an_unclosed_marker_are_findings():
    unknown = ("<!-- BEGIN generated: shared/nothing -->\nx\n"
               "<!-- END generated -->\n")
    assert "not a shared prompt block" in prompts.drift(unknown)[0]
    unclosed = "<!-- BEGIN generated: shared/evidence-rules -->\nx\n"
    assert "no matching" in prompts.drift(unclosed)[0]


def test_the_module_entry_checks_and_syncs_a_file(tmp_path, capsys):
    page = tmp_path / "AGENTS.md"
    good = "# A\n\n" + prompts.section("shared/evidence-rules") + "\n"
    page.write_text(good.replace("never count it as one", "count it twice"))
    assert prompts.main(["check", str(page)]) == 1
    assert prompts.main(["sync", str(page)]) == 0
    assert page.read_text() == good
    assert prompts.main(["check", str(page)]) == 0
    page.write_text("# no markers\n")
    assert prompts.main(["check", str(page)]) == 0
    assert prompts.main(["sync", str(page)]) == 2
    assert prompts.main(["render"]) == 0
    assert prompts.section("shared/evidence-rules") in capsys.readouterr().out
