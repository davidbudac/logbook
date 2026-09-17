"""The subset renderer's first tests: what it emits, and what it refuses.

Two things are proved here. The first is conformance — every block and inline
construct the project's markdown actually uses renders to the markup the docs
site and the portal expect, block by block. The second is that no page can
escape the renderer: a `<` in source text is always `&lt;`, an href with a
scheme outside http/https never becomes an `href=` at all, and an adversarial
corpus drives that through every construct that carries author text.

What is not proved here is the docs site's own byte stability. That belongs to
`docs/build_site.py`, which owns the figure hook, the `[R]` pill and the routing
between guides; this module tests the emitter those sit on top of.

Wikilink tests build their policy out of `lint.resolve_link` rather than a stub,
because the rule that matters is that lint and the renderer agree: a link lint
calls broken must not render as a link that works.
"""

import re

import pytest

from dbwiki import lint, markdown
from dbwiki.markdown import PlainLinks, SnapshotLinks, render


def body(text, **kw):
    return render(text, **kw).html


def test_a_fenced_block_without_an_info_string_carries_no_data_lang():
    out = body("```\nselect 1\n```")
    assert out == ('<div class="scroll"><pre><code>select 1</code></pre></div>'), \
        "a bare fence renders as pre/code inside the scroll wrapper"


def test_a_fence_info_string_becomes_the_data_lang_attribute():
    assert 'data-lang="sql"' in body("```sql\nselect 1\n```"), \
        "the info string is the block's language and rides as data-lang"


def test_a_fenced_block_never_interprets_its_own_body():
    out = body("```\n# not a heading\n**not bold**\n```")
    assert "<h1" not in out and "<strong>" not in out, \
        "nothing inside a fence is markdown"


def test_every_heading_level_gets_an_id_and_an_anchor():
    out = body("# One\n\n## Two words\n\n### Three")
    for level, slug, text in ((1, "one", "One"), (2, "two-words", "Two words"),
                              (3, "three", "Three")):
        assert (f'<h{level} id="{slug}">{text}'
                f'<a class="anchor" href="#{slug}" aria-label="link">#</a>'
                f"</h{level}>") in out, \
            f"an h{level} carries its own id and a self-link to it"


def test_heading_prefix_namespaces_every_heading_id():
    out = body("## Setup", heading_prefix="guide")
    assert 'id="guide--setup"' in out, \
        "a prefix keeps two pages on one screen from sharing a heading id"


def test_headings_are_reported_at_every_level_for_the_caller_to_filter():
    page = render("# Title\n\n## A\n\n### B")
    assert [(h.level, h.text, h.slug) for h in page.headings] == \
        [(1, "Title", "title"), (2, "A", "a"), (3, "B", "b")], \
        "render reports every heading; picking a level is the caller's job"


def test_heading_text_drops_backticks_so_a_table_of_contents_reads_as_text():
    page = render("## The `run` command")
    assert page.headings[0].text == "The run command", \
        "heading text is plain text; the markup stays in the html"


def test_a_pipe_table_with_a_header_row_emits_a_thead():
    out = body("| a | b |\n| --- | --- |\n| 1 | 2 |")
    assert out == ('<div class="scroll"><table>'
                   "<thead><tr><th>a</th><th>b</th></tr></thead>"
                   "<tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>"), \
        "a table rides in the scroll wrapper with head and body separated"


def test_a_pipe_table_with_an_empty_header_row_emits_no_thead():
    out = body("|  |  |\n| --- | --- |\n| 1 | 2 |")
    assert "<thead>" not in out, \
        "an all-empty header row is a headerless table, not an empty one"


def test_an_escaped_pipe_stays_inside_its_table_cell():
    out = body("| a | b |\n| --- | --- |\n| one\\|two | 2 |")
    assert "<td>one|two</td>" in out, \
        r"`\|` is a literal pipe in a cell, not a column boundary"


def test_a_horizontal_rule_becomes_an_hr():
    assert body("a\n\n---\n\nb") == "<p>a</p><hr><p>b</p>", \
        "three or more dashes on their own line is a rule"


def test_a_blockquote_renders_its_content_as_blocks():
    assert body("> quoted **text**") == \
        "<blockquote><p>quoted <strong>text</strong></p></blockquote>", \
        "a blockquote's content is markdown, not literal text"


def test_an_unordered_list_collapses_a_single_paragraph_item():
    assert body("- one\n- two") == "<ul><li>one</li><li>two</li></ul>", \
        "a one-paragraph item is bare text, so a list does not grow blank lines"


def test_an_ordered_list_uses_ol():
    assert body("1. one\n2. two") == "<ol><li>one</li><li>two</li></ol>", \
        "a numbered list is an ol; the numbers come from the browser"


def test_a_list_item_carries_indented_block_children():
    out = body("- item\n\n  ```\n  code\n  ```\n")
    assert out == ('<ul><li><p>item</p><div class="scroll"><pre><code>code'
                   "</code></pre></div></li></ul>"), \
        "an indented block under a bullet belongs to that bullet"


def test_a_nested_list_becomes_a_list_inside_the_item():
    out = body("- outer\n  - inner\n")
    assert out == "<ul><li><p>outer</p><ul><li>inner</li></ul></li></ul>", \
        "an indented bullet nests inside the item above it"


def test_a_blockquote_inside_a_list_item_renders_as_a_blockquote():
    out = body("- item\n\n  > quoted\n")
    assert out == ("<ul><li><p>item</p><blockquote><p>quoted</p>"
                   "</blockquote></li></ul>"), \
        "block dispatch inside a list item is the same dispatch, quotes included"


def test_wrapped_lines_join_into_one_paragraph():
    assert body("one\ntwo\nthree") == "<p>one two three</p>", \
        "a hard-wrapped paragraph is one paragraph, joined with spaces"


REPORT = """---
type: report
window_start: 2026-08-11T18:15:00Z
window_end: 2026-08-12T06:15:00Z
generated: 2026-08-12T06:15:04Z
---
# Fleet report — 2026-08-12 06:15 to 18:15

Two databases reported, one notable.

## Summary
"""


def test_a_report_page_takes_its_title_from_the_heading_not_the_frontmatter():
    assert render(REPORT).title == "Fleet report — 2026-08-12 06:15 to 18:15", \
        "the title is the first `# ` heading after the frontmatter"


def test_frontmatter_keys_never_reach_the_rendered_page():
    out = body(REPORT)
    for key in ("type:", "window_start", "window_end", "generated"):
        assert key not in out, f"{key} is metadata, never the page's lede"


def test_the_closing_dashes_of_frontmatter_do_not_become_a_rule():
    assert "<hr>" not in body(REPORT), \
        "frontmatter is consumed before block scanning, so its `---` is not a rule"


def test_a_page_without_frontmatter_still_renders_its_leading_rule():
    assert body("---\n\ntext") == "<hr><p>text</p>", \
        "an unterminated `---` is a rule, not a frontmatter block"


def test_a_code_span_is_escaped_and_never_reinterpreted():
    assert markdown.inline("`a <b> **c**`") == \
        "<code>a &lt;b&gt; **c**</code>", \
        "a code span is literal text, escaped once"


def test_bold_and_italic():
    assert markdown.inline("**a** and *b*") == \
        "<strong>a</strong> and <em>b</em>", \
        "the emphasis subset is `**` for bold and `*` for italic"


@pytest.mark.parametrize("char", list("\\`*_[]()#|"))
def test_a_backslash_escape_emits_the_character_literally(char):
    assert markdown.inline("x\\" + char + "y") == "x" + char + "y", \
        "a backslash before a markup character consumes both and emits the character"


def test_an_escaped_asterisk_does_not_open_emphasis():
    assert markdown.inline(r"\*not italic\*") == "*not italic*", \
        "an escaped `*` is text, so emphasis never opens"


def test_a_less_than_sign_is_always_escaped():
    assert markdown.inline("a < b") == "a &lt; b", \
        "there is no raw-HTML passthrough; `<` is always `&lt;`"


PAGES = ["errors/ORA-600.md", "reports/2026-08-12.md",
         "databases/alpha/journal/2026-08.md", "databases/beta/notes.md",
         "databases/gamma/notes.md"]


def snapshot_links():
    return SnapshotLinks(exists=lambda h: h in PAGES,
                         resolve=lambda t: lint.resolve_link(t, PAGES))


def test_plain_links_renders_a_wikilink_as_its_own_label():
    assert markdown.inline("[[errors/ORA-600]]", links=PlainLinks()) == \
        "errors/ORA-600", \
        "a wiki link means nothing outside a wiki, so only its label survives"


def test_snapshot_links_resolves_a_wikilink_to_a_page_route():
    assert markdown.inline("[[errors/ORA-600]]", links=snapshot_links()) == \
        '<a href="#/page/errors/ORA-600.md">errors/ORA-600</a>', \
        "a resolved wikilink is the portal's own hash route"


def test_a_wikilink_label_is_used_and_its_target_is_resolved():
    assert markdown.inline("[[errors/ORA-600|the error page]]",
                           links=snapshot_links()) == \
        '<a href="#/page/errors/ORA-600.md">the error page</a>', \
        "`[[target|label]]` resolves the target and shows the label"


def test_a_section_on_a_wikilink_target_resolves_the_page_it_names():
    assert lint.WIKILINK_RE.findall("[[errors/ORA-600#Occurrences]]") == \
        ["errors/ORA-600"], "lint splits the section off before resolving"
    assert markdown.inline("[[errors/ORA-600#Occurrences]]",
                           links=snapshot_links()) == \
        '<a href="#/page/errors/ORA-600.md">errors/ORA-600#Occurrences</a>', \
        "the renderer splits it off the same way, so a link lint calls sound " \
        "never renders struck through; the label keeps the section, the route " \
        "drops it because a hash route cannot carry a second fragment"


def test_an_unresolved_wikilink_degrades_to_a_nolink_span():
    assert markdown.inline("[[errors/ORA-99999]]", links=snapshot_links()) == \
        '<span class="nolink">errors/ORA-99999</span>', \
        "a target that resolves to nothing is struck through, never a dead link"


def test_an_ambiguous_stem_does_not_resolve():
    assert lint.resolve_link("notes", PAGES) is None, \
        "two pages share the stem, so lint refuses to pick one"
    assert markdown.inline("[[notes]]", links=snapshot_links()) == \
        '<span class="nolink">notes</span>', \
        "the renderer resolves exactly as lint judges, ambiguity included"


def test_snapshot_links_routes_a_relative_href_that_exists():
    assert markdown.inline("[e](errors/ORA-600.md)", links=snapshot_links()) == \
        '<a href="#/page/errors/ORA-600.md">e</a>', \
        "a relative href inside the snapshot is an internal route"


def test_snapshot_links_leaves_a_missing_relative_href_as_a_path():
    assert markdown.inline("[e](src/dbwiki/lint.py)", links=snapshot_links()) == \
        '<code class="path">e</code>', \
        "a path with no page behind it reads as a path, not a link"


def test_snapshot_links_keeps_a_fragment_on_the_page():
    assert markdown.inline("[s](#summary)", links=snapshot_links()) == \
        '<a href="#summary">s</a>', \
        "a fragment addresses this page and needs no route"


def test_an_external_href_opens_in_a_new_tab():
    for links in (PlainLinks(), snapshot_links()):
        assert markdown.inline("[o](https://example.test/x)", links=links) == \
            '<a href="https://example.test/x" target="_blank" rel="noopener">o</a>', \
            "an http(s) link leaves the page, so it opens in a new tab"


def test_plain_links_renders_every_other_href_as_a_path():
    assert markdown.inline("[g](user-guide.md)", links=PlainLinks()) == \
        '<code class="path">g</code>', \
        "with no site behind it, a relative href is a file reference"


REFUSED = ["javascript:alert(1)", "JaVaScript:alert(1)", " javascript:alert(1)",
           "java\tscript:alert(1)", "java\nscript:alert(1)", "data:text/html,x",
           "vbscript:x", "file:///etc/passwd", "//evil.example/x",
           "\x01javascript:alert(1)"]


@pytest.mark.parametrize("href", REFUSED)
@pytest.mark.parametrize("policy", ["plain", "snapshot"])
def test_a_scheme_outside_http_never_becomes_a_link(href, policy):
    links = PlainLinks() if policy == "plain" else snapshot_links()
    out = markdown.inline(f"[label]({href})", links=links)
    assert out.startswith('<span class="nolink">label</span>'), \
        "the renderer refuses the scheme itself, so no policy can ship one"
    assert "href=" not in out, "a refused href produces no href at all"


ALLOWED = ["http://example.test/x", "https://example.test/x", "#section",
           "user-guide.md", "src/dbwiki/lint.py", "errors/ORA-600.md",
           "../sibling.md"]


@pytest.mark.parametrize("href", ALLOWED)
def test_an_allowed_href_reaches_the_policy(href):
    out = markdown.inline(f"[label]({href})", links=PlainLinks())
    assert "label" in out and "nolink" not in out, \
        "http, https, fragments and scheme-less relative references are followable"


def test_an_href_is_attribute_escaped_exactly_once():
    out = markdown.inline('[q](https://example.test/?a=1&b=2)', links=PlainLinks())
    assert 'href="https://example.test/?a=1&amp;b=2"' in out, \
        "the raw source href is escaped once, never an already-escaped string"


@pytest.mark.parametrize("bad", ['"', "&", ">", "<", "'"])
def test_an_href_carrying_a_quoting_character_cannot_break_out(bad):
    out = markdown.inline(f'[l](https://example.test/{bad}x)', links=PlainLinks())
    assert out.startswith('<a href="https://example.test/') and out.count('"') == 6, \
        "the attribute keeps exactly its own quotes; nothing in the value adds one"


PAYLOADS = ["<script>alert(1)</script>", '" onmouseover="x',
            "<img src=x onerror=alert(1)>", "]]><![CDATA[",
            "</code></pre><script>", "'><svg onload=alert(1)>",
            "<!--", "&lt;script&gt;", "javascript:alert(1)"]

CORPUS = ([f"[{p}](https://example.test/)" for p in PAYLOADS]
          + [f"[label]({p})" for p in PAYLOADS]
          + [f"## {p}" for p in PAYLOADS]
          + [f"```\n{p}\n```" for p in PAYLOADS]
          + [f"```{p}\ncode\n```" for p in PAYLOADS]
          + [f"`{p}`" for p in PAYLOADS]
          + [f"| a | b |\n| --- | --- |\n| {p} | 2 |" for p in PAYLOADS]
          + [f"> {p}" for p in PAYLOADS]
          + [f"- {p}" for p in PAYLOADS]
          + [f"[[{p}]]" for p in PAYLOADS]
          + [f"[[errors/ORA-600|{p}]]" for p in PAYLOADS]
          + [f"{p}" for p in PAYLOADS])

EMITTED = ("p|h1|h2|h3|h4|h5|h6|a|code|pre|div|table|thead|tbody|tr|th|td"
           "|ul|ol|li|blockquote|hr|strong|em|span")
OWN_TAG = re.compile(rf"</?(?:{EMITTED})(?:\s[^>]*)?>")
EVENT_ATTR = re.compile(r"\son[a-z]+\s*=")


@pytest.mark.parametrize("source", CORPUS, ids=range(len(CORPUS)))
@pytest.mark.parametrize("policy", ["plain", "snapshot"])
def test_no_payload_survives_as_markup(source, policy):
    links = PlainLinks() if policy == "plain" else snapshot_links()
    out = body(source, links=links)
    for tag in OWN_TAG.findall(out):
        assert not EVENT_ATTR.search(tag), \
            f"an on* handler reached a tag rendered from {source!r}"
    assert "<" not in OWN_TAG.sub("", out), \
        f"a `<` the renderer did not emit survived {source!r}"


def test_render_is_pure():
    text = REPORT + "\n- [[errors/ORA-600]]\n\n> quoted\n"
    first = render(text, links=snapshot_links(), heading_prefix="p")
    second = render(text, links=snapshot_links(), heading_prefix="p")
    assert first == second, \
        "render reads nothing outside its arguments, so two calls agree"
