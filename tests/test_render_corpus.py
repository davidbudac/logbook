"""Every committed wiki page renders, and the renderer terminates on
arbitrary input (issue 23).

`tools/render_corpus.py` is what CI runs over `wiki-template/` and what an
operator points at a real wiki checkout; this file proves the tool itself
(its timer really interrupts a renderer that never returns) and adds a seeded
fuzz over `markdown.render`, each case under the same timer. A hang such as
issue 01's (`"|a"` looped forever) fails one case in two seconds instead of
wedging the suite.
"""

import importlib.util
import random
import time
from pathlib import Path

import pytest

from dbwiki import markdown

TOOL = Path(__file__).resolve().parent.parent / "tools" / "render_corpus.py"
_spec = importlib.util.spec_from_file_location("render_corpus", TOOL)
render_corpus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_corpus)

#: Per-case limit for the fuzz. A real render of these few-dozen-character
#: inputs takes microseconds; two seconds is only ever a hang.
CASE_TIMEOUT_S = 2.0

#: Markdown and HTML fragments the renderer branches on, including the table
#: pipe that issue 01 looped on and the fences, frontmatter and wikilink
#: brackets that start multi-line blocks.
ATOMS = ["|", "| a |", "|---|", "|a", "a|b", "[[", "]]", "[", "]", "(", ")",
         "`", "```", "```yaml\n", "*", "**", "_", "\\", "#", "## ", "- ",
         "1. ", "> ", "---", "---\n", "\n", "\n\n", " ", "\t", "<", ">",
         "&", "&lt;", "<script>", "javascript:alert(1)", "http://a\"x=1",
         "[x](", "[[a|", "errors/ORA-1.md", "\x00", " ", "\r", "a",
         "word", "ORA-00600"]


def test_every_template_page_renders_in_time():
    found = render_corpus.pages(render_corpus.DEFAULT_DIRS)
    assert found, "wiki-template/ holds pages to render"
    assert render_corpus.check(found) == []


def test_a_renderer_that_never_returns_is_reported_not_waited_on(tmp_path):
    """The tool's whole claim: without the timer a hang is a CI job that
    runs until the runner kills it and names nothing."""
    page = tmp_path / "stuck.md"
    page.write_text("| a\n")

    def forever(text):
        while True:
            pass

    started = time.monotonic()
    problems = render_corpus.check([page], 0.2, render=forever)
    assert time.monotonic() - started < 5
    assert problems == [f"{page}: did not render within 0.2s"]


def test_a_renderer_that_raises_is_reported_by_page(tmp_path):
    page = tmp_path / "bad.md"
    page.write_text("x\n")

    def broken(text):
        raise ValueError("no")

    assert render_corpus.check([page], 1.0, render=broken) \
        == [f"{page}: ValueError: no"]


def test_the_command_line_exits_by_what_it_found(tmp_path, capsys):
    (tmp_path / "wiki" / "notes").mkdir(parents=True)
    (tmp_path / "wiki" / "notes" / "a.md").write_text("# A\n\ntext\n")
    (tmp_path / "wiki" / ".git").mkdir()
    (tmp_path / "wiki" / ".git" / "HEAD.md").write_text("| a\n")
    assert render_corpus.main([str(tmp_path / "wiki")]) == 0
    assert "rendered 1/1 pages" in capsys.readouterr().out
    assert render_corpus.main([str(tmp_path / "missing")]) == 2
    (tmp_path / "empty").mkdir()
    assert render_corpus.main([str(tmp_path / "empty")]) == 1, \
        "a walk that finds no page proves nothing and says so"


def _cases(seed: int, count: int):
    rng = random.Random(seed)
    yield "|a"  # issue 01's own input, first, whatever the seed draws
    for _ in range(count):
        yield "".join(rng.choice(ATOMS) for _ in range(rng.randint(1, 16)))


@pytest.mark.parametrize("links", [
    markdown.PLAIN,
    markdown.SnapshotLinks(exists=lambda path: True,
                           resolve=lambda target: target + ".md"),
], ids=["plain", "snapshot"])
def test_markdown_render_terminates_on_arbitrary_input(links):
    """Seeded, so a failure names an input that fails again. Each case must
    return a `Rendered` inside the limit; an exception is a failure too,
    since the portal would answer it with a 500."""
    for text in _cases(seed=23, count=1500):
        try:
            out = render_corpus.within(
                CASE_TIMEOUT_S, lambda: markdown.render(text, links=links))
        except render_corpus.RenderTimeout:
            pytest.fail(f"markdown.render did not return within "
                        f"{CASE_TIMEOUT_S}s on {text!r}")
        assert isinstance(out, markdown.Rendered), text
