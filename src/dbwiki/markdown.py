"""The one markdown emitter: a small, closed subset rendered to HTML.

The subset is exactly what this project's markdown uses — ATX headings,
fenced code, pipe tables, blockquotes, ordered/unordered lists with indented
block children, horizontal rules, and inline code/bold/italic/links. There are
no images, no raw-HTML passthrough and no autolinks, so a `<` in source text is
always `&lt;` and a page can never smuggle markup through the renderer.

Rendering is pure: `render` reads no file, runs no git and knows no wiki. Where
a link should point is a caller's question, so it is asked through a `Links`
policy — `PlainLinks` for a page with no wiki behind it, `SnapshotLinks` for
the portal, `docs/build_site.DocsLinks` for the manual.

The scheme allowlist is enforced here and never delegated to the policy. A
policy that can be handed a `javascript:` URL is a policy that can ship one,
and there is no reason for three policies to each get that right; an href
carrying a scheme outside http/https never reaches `Links.href` at all and is
emitted as the codebase's "this link cannot be followed" span instead.

Frontmatter is consumed before any block scanning, which is why a wiki page's
YAML block is neither read as its lede nor closed by a stray `<hr>`.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .pagetext import FM_RE

__all__ = ["BlockHook", "Heading", "Links", "PlainLinks", "Rendered",
           "SnapshotLinks", "attr", "inline", "render", "slug"]


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    slug: str


@dataclass(frozen=True)
class Rendered:
    title: str
    html: str
    headings: tuple[Heading, ...]


class Links(Protocol):
    """Where a link points. `label` is already-rendered inline HTML and goes
    into element content as-is; `target` and `href` are the raw source strings,
    so a policy that puts one in an attribute escapes it with `attr`."""

    def wikilink(self, target: str, label: str) -> str: ...

    def href(self, href: str, label: str) -> str: ...


BlockHook = Callable[[Sequence[str], int], "tuple[str, int] | None"]


def attr(value: str) -> str:
    """The one attribute escape. Every policy uses it, so a value is escaped
    the same way wherever it lands."""
    return html.escape(value, quote=True)


def _nolink(label: str) -> str:
    return f'<span class="nolink">{label}</span>'


def _external(href: str, label: str) -> str:
    return f'<a href="{attr(href)}" target="_blank" rel="noopener">{label}</a>'


class PlainLinks:
    """No wiki, no site: an http(s) href opens in a new tab and everything
    else reads as a file path. Wiki links mean nothing outside a wiki, so a
    `[[link]]` degrades to its own label."""

    def wikilink(self, target: str, label: str) -> str:
        return label

    def href(self, href: str, label: str) -> str:
        if href.startswith(("http://", "https://")):
            return _external(href, label)
        return f'<code class="path">{label}</code>'


class SnapshotLinks:
    """Links against one wiki snapshot. `resolve` is `lint.resolve_link`, so a
    `[[link]]` renders exactly as lint judges it and an ambiguous stem does not
    resolve; `exists` answers for a relative href. A target that resolves to
    nothing degrades to the struck-through span, never to a dead link."""

    def __init__(self, exists: Callable[[str], bool],
                 resolve: Callable[[str], str | None]) -> None:
        self._exists = exists
        self._resolve = resolve

    def wikilink(self, target: str, label: str) -> str:
        path = self._resolve(target)
        return self._page(path, label) if path else _nolink(label)

    def href(self, href: str, label: str) -> str:
        if href.startswith(("http://", "https://")):
            return _external(href, label)
        if href.startswith("#"):
            return f'<a href="{attr(href)}">{label}</a>'
        if self._exists(href):
            return self._page(href, label)
        return f'<code class="path">{label}</code>'

    @staticmethod
    def _page(path: str, label: str) -> str:
        route = "#/page/" + urllib.parse.quote(path, safe="/")
        return f'<a href="{attr(route)}">{label}</a>'


PLAIN = PlainLinks()

_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.\-]*:")


def _followable(href: str) -> bool:
    r"""Whether an href may reach a policy at all. Whitespace and control
    characters are dropped before the test because `java\tscript:x` is one
    scheme to a browser and two strings to a naive comparison."""
    probe = "".join(c for c in href
                    if ord(c) > 0x20 and ord(c) != 0x7f).casefold()
    if probe.startswith("//"):
        return False
    if probe.startswith(("http://", "https://")):
        return True
    return not _SCHEME_RE.match(probe)


_CODE = re.compile(r"`([^`]+)`")
_ESCAPE = re.compile(r"\\([\\`*_\[\]()#|])")
_WIKILINK = re.compile(
    r"\[\[([^\[\]|#]+)(#[^\[\]|]*)?(?:\|([^\[\]]*))?\]\]")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_HELD = re.compile("\x00([CEH])(\\d+)\x00")


class _Held:
    """Fragments pulled out of the line so no later rule can re-read them:
    code spans, backslash-escaped characters, and finished link markup."""

    def __init__(self) -> None:
        self.code: list[str] = []
        self.esc: list[str] = []
        self.html: list[str] = []

    def _put(self, bucket: list[str], kind: str, value: str) -> str:
        bucket.append(value)
        return f"\x00{kind}{len(bucket) - 1}\x00"

    def code_span(self, m: re.Match) -> str:
        return self._put(self.code, "C", m.group(1))

    def escaped(self, m: re.Match) -> str:
        return self._put(self.esc, "E", m.group(1))

    def markup(self, value: str) -> str:
        return self._put(self.html, "H", value)

    def source(self, text: str) -> str:
        """A held fragment back as its raw source, for an href a policy must
        see the way the author wrote it."""
        return _HELD.sub(lambda m: self._raw(m.group(1), int(m.group(2))), text)

    def _raw(self, kind: str, n: int) -> str:
        if kind == "C":
            return f"`{self.code[n]}`"
        return self.esc[n] if kind == "E" else self.html[n]

    def restore(self, text: str) -> str:
        def one(m: re.Match) -> str:
            kind, n = m.group(1), int(m.group(2))
            if kind == "C":
                return f"<code>{html.escape(self.code[n], quote=False)}</code>"
            if kind == "E":
                return html.escape(self.esc[n], quote=False)
            return self.html[n]

        while "\x00" in text:
            text = _HELD.sub(one, text)
        return text


def _emphasis(text: str) -> str:
    return _ITALIC.sub(r"<em>\1</em>", _BOLD.sub(r"<strong>\1</strong>", text))


def inline(text: str, *, links: Links = PLAIN) -> str:
    """Escape, then apply the inline subset. Code spans and escapes are pulled
    out first so nothing inside them is re-interpreted, and finished link
    markup is pulled out too so emphasis cannot reach into an attribute."""
    held = _Held()
    out = _CODE.sub(held.code_span, text.replace("\x00", ""))
    out = _ESCAPE.sub(held.escaped, out)
    out = _WIKILINK.sub(lambda m: _wikilink(held, links, m), out)
    out = _LINK.sub(lambda m: _href(held, links, m), out)
    return held.restore(_emphasis(html.escape(out, quote=False)))


def _label(text: str) -> str:
    return _emphasis(html.escape(text, quote=False))


def _wikilink(held: _Held, links: Links, m: re.Match) -> str:
    target = held.source(m.group(1)).strip()
    written = m.group(1) + (m.group(2) or "")
    label = _label(m.group(3) if m.group(3) is not None else written)
    return held.markup(links.wikilink(target, label))


def _href(held: _Held, links: Links, m: re.Match) -> str:
    href = held.source(m.group(2))
    label = _label(m.group(1))
    if not _followable(href):
        return held.markup(_nolink(label))
    return held.markup(links.href(href, label))


_FENCE = re.compile(r"^```(\w*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_ULI = re.compile(r"^([-*])\s+(.*)$")
_OLI = re.compile(r"^(\d+)\.\s+(.*)$")
_RULE = re.compile(r"^-{3,}\s*$")


def slug(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[*_\[\]()]", "", text).strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


@dataclass(frozen=True)
class _Scan:
    links: Links
    prefix: str
    hook: BlockHook | None


def _cells(row: str, ctx: _Scan) -> list[str]:
    row = row.strip().strip("|")
    parts = [c.replace("\x01", "|") for c in row.replace(r"\|", "\x01").split("|")]
    return [inline(c.strip(), links=ctx.links) for c in parts]


def _table(lines: Sequence[str], i: int, out: list[str], ctx: _Scan) -> int:
    head = _cells(lines[i], ctx)
    i += 2
    body = []
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        body.append(_cells(lines[i], ctx))
        i += 1
    out.append('<div class="scroll"><table>')
    if any(head):
        out.append("<thead><tr>" + "".join(f"<th>{c}</th>" for c in head) + "</tr></thead>")
    out.append("<tbody>")
    for row in body:
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
    out.append("</tbody></table></div>")
    return i


def _list(lines: Sequence[str], i: int, out: list[str],
          headings: list[Heading], ctx: _Scan) -> int:
    ordered = bool(_OLI.match(lines[i]))
    pattern = _OLI if ordered else _ULI
    tag = "ol" if ordered else "ul"
    out.append(f"<{tag}>")
    while i < len(lines):
        m = pattern.match(lines[i])
        if not m:
            break
        item = [m.group(2)]
        i += 1
        while i < len(lines):
            if not lines[i].strip():
                nxt = i + 1
                if nxt < len(lines) and lines[nxt].startswith("  ") \
                        and lines[nxt].strip():
                    item.append("")
                    i += 1
                    continue
                break
            if lines[i].startswith("  "):
                item.append(lines[i][3:] if lines[i].startswith("   ")
                            else lines[i][2:])
                i += 1
                continue
            break
        inner: list[str] = []
        _blocks(item, inner, headings, ctx)
        body = "".join(inner)
        if body.startswith("<p>") and body.count("<p>") == 1 and body.endswith("</p>"):
            body = body[3:-4]
        out.append(f"<li>{body}</li>")
        while i < len(lines) and not lines[i].strip() and i + 1 < len(lines) \
                and pattern.match(lines[i + 1]):
            i += 1
    out.append(f"</{tag}>")
    return i


def _blocks(lines: Sequence[str], out: list[str], headings: list[Heading],
            ctx: _Scan) -> None:
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        hooked = ctx.hook(lines, i) if ctx.hook else None
        if hooked is not None:
            out.append(hooked[0])
            i = hooked[1]
            continue
        fence = _FENCE.match(line)
        if fence:
            code = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                code.append(html.escape(lines[i], quote=False))
                i += 1
            i += 1
            lang = f' data-lang="{attr(fence.group(1))}"' if fence.group(1) else ""
            out.append(f'<div class="scroll"><pre{lang}><code>'
                       + "\n".join(code) + "</code></pre></div>")
            continue
        head = _HEADING.match(line)
        if head:
            level = len(head.group(1))
            text = head.group(2)
            sl = f"{ctx.prefix}--{slug(text)}" if ctx.prefix else slug(text)
            headings.append(Heading(level, re.sub(r"`", "", text), sl))
            out.append(f'<h{level} id="{attr(sl)}">{inline(text, links=ctx.links)}'
                       f'<a class="anchor" href="#{attr(sl)}" aria-label="link">#</a>'
                       f"</h{level}>")
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) \
                and set(lines[i + 1].strip()) <= set("|-: "):
            i = _table(lines, i, out, ctx)
            continue
        if _RULE.match(line):
            out.append("<hr>")
            i += 1
            continue
        if line.startswith("> "):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i][2:] if lines[i].startswith("> ") else "")
                i += 1
            inner: list[str] = []
            _blocks(quote, inner, headings, ctx)
            out.append("<blockquote>" + "".join(inner) + "</blockquote>")
            continue
        if _ULI.match(line) or _OLI.match(line):
            i = _list(lines, i, out, headings, ctx)
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not _HEADING.match(lines[i]) \
                and not _FENCE.match(lines[i]) and not _RULE.match(lines[i]) \
                and not lines[i].lstrip().startswith("|") \
                and not _ULI.match(lines[i]) and not _OLI.match(lines[i]) \
                and not lines[i].startswith("> "):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + inline(" ".join(para), links=ctx.links) + "</p>")


def render(text: str, *, links: Links = PLAIN, heading_prefix: str = "",
           block_hook: BlockHook | None = None) -> Rendered:
    """One page to HTML. Frontmatter goes first, before any block sees a line,
    so its keys never read as prose and its closing `---` never becomes a rule.
    `heading_prefix` namespaces every heading id, so two pages on one screen
    may share a section name. `block_hook` is consulted for every non-blank
    line ahead of the built-in rules; it is the only extension point block
    dispatch has."""
    m = FM_RE.match(text)
    body = text[m.end():] if m else text
    ctx = _Scan(links, heading_prefix, block_hook)
    headings: list[Heading] = []
    out: list[str] = []
    _blocks(body.rstrip().split("\n"), out, headings, ctx)
    title = next((h.text for h in headings if h.level == 1), "")
    return Rendered(title, "".join(out), tuple(headings))
