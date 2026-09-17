"""Render the markdown guides in this folder into one standalone HTML site.

    python3 docs/build_site.py                 # -> docs/site.html (standalone)
    python3 docs/build_site.py --fragment      # -> stdout, no <html>/<head>/<body>

Deterministic and dependency-free: the markdown files stay the canonical source
and this only presents them. `dbwiki.markdown` renders the subset; everything
the guides alone need — the figure markers, the `[R]` pill, and routing between
the guides — lives here as a block hook and a `Links` policy.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path
from typing import NamedTuple

from figures import DEFS, FIGURES

HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE.parent / "src"))

from dbwiki import markdown  # noqa: E402

Doc = NamedTuple("Doc", [("group", str), ("slug", str), ("file", str),
                         ("eyebrow", str), ("hook", str)])

DOCS = [
    Doc("Get going", "start-here", "start-here.md", "Start",
        "What it does for you, the jobs you will do, and the day it runs."),
    Doc("Get going", "user-guide", "user-guide.md", "Operation",
        "Setup, config, commands, recipes, and the schedule."),
    Doc("Keep it alive", "monitoring", "monitoring.md", "Recovery",
        "Health output, failure categories, alerts, and the runbook."),
    Doc("Understand it", "overview", "overview.md", "Orientation",
        "What it is, why it is shaped this way, and the words used precisely."),
    Doc("Understand it", "how-it-works", "how-it-works.md", "Mechanism",
        "Logs in, wiki out — every stage, and the rails around them."),
]
DOC_SLUGS = {d.file: d.slug for d in DOCS}

SITE_TITLE = "Logbook Manual"
SITE_TAG = "An evolving knowledge base built from database logs."
BRAND = ('<a class="brand-logo" href="../index.html" aria-label="Logbook home">'
         '<img src="../logo-transparent.png" alt="Logbook" width="1774" height="887"></a>')


_FIGURE = re.compile(r"^<!--\s*figure:\s*([a-z0-9-]+)\s*(?:\|\s*(.*?))?\s*-->\s*$")
_FENCE = re.compile(r"^```(\w*)\s*$")


class DocsLinks:
    """Site-internal links become routes; every other repo path becomes a plain
    file reference, which stays honest whether the page is opened from the repo
    or from a published copy. The guides carry no wiki links."""

    def wikilink(self, target: str, label: str) -> str:
        return label

    def href(self, href: str, label: str) -> str:
        target = href.split("/")[-1]
        if target in DOC_SLUGS and "/" not in href.rstrip("/"):
            slug = DOC_SLUGS[target]
            return f'<a href="#{slug}" data-route="{slug}">{label}</a>'
        if href.startswith(("http://", "https://")):
            return (f'<a href="{markdown.attr(href)}" target="_blank" '
                    f'rel="noopener">{label}</a>')
        return f'<code class="path">{label}</code>'


LINKS = DocsLinks()


def pill(text: str) -> str:
    """The guides' one status marker: retryable failure categories."""
    return text.replace("<code>[R]</code>",
                        '<span class="pill" title="a plain rerun can fix it">R</span>')


def figure_block(lines, i: int):
    """A `<!-- figure: name | caption -->` marker. The drawn figure replaces the
    fenced block or table that follows it — the plain-markdown fallback GitHub
    renders — and is simply inserted when the marker stands alone."""
    m = _FIGURE.match(lines[i])
    if not m:
        return None
    name, caption = m.group(1), (m.group(2) or "").strip()
    if name not in FIGURES:
        raise SystemExit(f"unknown figure: {name}")
    body = FIGURES[name]()
    cap = ""
    if caption:
        cap = f'<figcaption>{markdown.inline(caption, links=LINKS)}</figcaption>'
    out = f'<figure class="fig" data-figure="{name}">{body}{cap}</figure>'
    i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and _FENCE.match(lines[i]):
        i += 1
        while i < len(lines) and not _FENCE.match(lines[i]):
            i += 1
        return out, i + 1
    if i < len(lines) and lines[i].lstrip().startswith("|") and i + 1 < len(lines) \
            and set(lines[i + 1].strip()) <= set("|-: "):
        i += 2
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            i += 1
    return out, i


def drop_rule_before_heading(body: str) -> str:
    """The guides separate sections with `---`, and the site's h1/h2 styles
    carry their own top rule; keeping both would draw a double line."""
    return re.sub(r"<hr>(?=<h[12])", "", body)


def render(md: str, prefix: str = "") -> tuple[str, str, str, list[dict]]:
    """-> (title, lede html, body html, h2 headings). `prefix` namespaces every
    heading id to its guide, so two guides may share a section name. A guide
    opens with its title and a lede, which the site places in the page header
    rather than in the body."""
    lines = md.rstrip().split("\n")
    title = lines[0].lstrip("# ").strip()
    rest = lines[1:]
    while rest and not rest[0].strip():
        rest.pop(0)
    lede: list[str] = []
    while rest and rest[0].strip() and not rest[0].startswith(("#", "|", "-", "`")):
        lede.append(rest.pop(0).strip())
    page = markdown.render("\n".join(rest), links=LINKS, heading_prefix=prefix,
                           block_hook=figure_block)
    body = drop_rule_before_heading(page.html)
    return (title, pill(markdown.inline(" ".join(lede), links=LINKS)), pill(body),
            [{"level": h.level, "text": h.text, "slug": h.slug}
             for h in page.headings if h.level == 2])


FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=Archivo:wght@600;700&family=IBM+Plex+Mono:wght@400;500&'
         'family=IBM+Plex+Sans:wght@400;500;600&display=swap">')

CSS = """
:root {
  --ground:#e9ecef; --paper:#ffffff; --ink:#131820; --muted:#59656f;
  --faint:#8a959e; --rule:#dde2e7; --accent:#2a3ea8; --accent-soft:#eceefb;
  --code-bg:#f4f6f9; --ok:#1c7a4d; --warn:#8f5c0a; --fail:#b0281f;
  --shadow:0 1px 2px rgba(19,24,32,.06), 0 12px 32px -18px rgba(19,24,32,.35);
  --sans:"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  --display:"Archivo", "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  --mono:"IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
  --rail:288px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:#0c1015; --paper:#12181f; --ink:#e4e9ee; --muted:#9aa6b1;
    --faint:#6d7a86; --rule:#232c35; --accent:#93a6ff; --accent-soft:#191f33;
    --code-bg:#0e141b; --ok:#4cbf82; --warn:#d8a445; --fail:#ef8074;
    --shadow:0 1px 2px rgba(0,0,0,.5), 0 14px 34px -20px rgba(0,0,0,.8);
  }
}
:root[data-theme="dark"] {
  --ground:#0c1015; --paper:#12181f; --ink:#e4e9ee; --muted:#9aa6b1;
  --faint:#6d7a86; --rule:#232c35; --accent:#93a6ff; --accent-soft:#191f33;
  --code-bg:#0e141b; --ok:#4cbf82; --warn:#d8a445; --fail:#ef8074;
  --shadow:0 1px 2px rgba(0,0,0,.5), 0 14px 34px -20px rgba(0,0,0,.8);
}

* { box-sizing:border-box; }
body {
  margin:0; background:var(--paper); color:var(--ink);
  font-family:var(--sans); font-size:15.5px; line-height:1.65;
  -webkit-font-smoothing:antialiased;
}
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; text-underline-offset:3px; }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:2px; }

.rail {
  position:fixed; inset:0 auto 0 0; width:var(--rail); background:var(--ground);
  border-right:1px solid var(--rule); display:flex; flex-direction:column;
  overflow-y:auto; z-index:40;
}
.brand { padding:26px 24px 18px; border-bottom:1px solid var(--rule); }
.brand-logo {
  display:block; position:relative; width:200px; height:44px;
  overflow:hidden; flex-shrink:0;
}
.brand-logo img { position:absolute; width:269px; height:134.5px; max-width:none; left:-36px; top:-44px; }
.topbar .brand-logo { width:110px; height:24px; }
.topbar .brand-logo img { width:148px; height:74px; left:-20px; top:-24px; }
:root[data-theme="dark"] .brand-logo img { filter:invert(1); }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .brand-logo img { filter:invert(1); }
}
.brand b {
  display:block; font-family:var(--display); font-weight:700; font-size:15px;
  letter-spacing:.13em; text-transform:uppercase;
}
.brand span {
  display:block; margin-top:6px; font-family:var(--mono); font-size:11.5px;
  color:var(--muted); line-height:1.45;
}
nav { padding:12px 12px 8px; flex:1; }
.group {
  margin:16px 12px 4px; font-family:var(--mono); font-size:9.5px;
  letter-spacing:.18em; text-transform:uppercase; color:var(--faint);
}
nav > .group:first-child { margin-top:2px; }
.guide { display:block; padding:11px 12px; border-radius:5px; color:inherit; }
.guide:hover { background:var(--paper); text-decoration:none; }
.guide .eyebrow {
  font-family:var(--mono); font-size:10px; letter-spacing:.16em;
  text-transform:uppercase; color:var(--faint);
}
.guide .name {
  display:block; font-family:var(--display); font-weight:600; font-size:15px;
  margin-top:1px;
}
.guide .hook { display:block; font-size:12.5px; color:var(--muted); line-height:1.45; margin-top:3px; }
.guide[aria-current="page"] { background:var(--accent-soft); }
.guide[aria-current="page"] .eyebrow,
.guide[aria-current="page"] .name { color:var(--accent); }
.sections { margin:2px 0 10px 22px; padding:2px 0 2px 14px; border-left:1px solid var(--rule); display:none; }
.guide[aria-current="page"] + .sections { display:block; }
.sections a {
  display:block; padding:4px 8px; font-size:13px; color:var(--muted);
  border-radius:4px; line-height:1.4;
}
.sections a:hover { color:var(--ink); text-decoration:none; }
.sections a.active { color:var(--accent); font-weight:500; }
.rail footer {
  padding:14px 20px 22px; border-top:1px solid var(--rule);
  display:flex; align-items:center; justify-content:space-between; gap:10px;
  font-family:var(--mono); font-size:11px; color:var(--faint);
}
.tbtn {
  font:inherit; font-family:var(--mono); font-size:11px; letter-spacing:.08em;
  text-transform:uppercase; color:var(--muted); background:transparent;
  border:1px solid var(--rule); border-radius:999px; padding:5px 11px; cursor:pointer;
}
.tbtn:hover { color:var(--ink); border-color:var(--muted); }

.topbar {
  display:none; position:sticky; top:0; z-index:50; background:var(--ground);
  border-bottom:1px solid var(--rule); padding:10px 16px;
  align-items:center; gap:12px;
}
.topbar b { font-family:var(--display); font-size:13px; letter-spacing:.12em; text-transform:uppercase; }
.topbar .tbtn { margin-left:auto; }

main { margin-left:var(--rail); padding:0 40px 120px; }
.doc { max-width:860px; margin:0 auto; }
.doc[hidden] { display:none; }
.dochead { padding:56px 0 30px; border-bottom:1px solid var(--rule); margin-bottom:38px; }
.dochead .eyebrow {
  font-family:var(--mono); font-size:11px; letter-spacing:.18em;
  text-transform:uppercase; color:var(--accent);
}
h1 {
  font-family:var(--display); font-weight:700; font-size:clamp(30px, 4.4vw, 40px);
  line-height:1.12; letter-spacing:-.015em; margin:12px 0 0; text-wrap:balance;
}
.lede { font-size:17px; color:var(--muted); margin:16px 0 0; max-width:64ch; }
h2 {
  font-family:var(--display); font-weight:600; font-size:23px; line-height:1.25;
  letter-spacing:-.01em; margin:56px 0 4px; padding-top:22px;
  border-top:1px solid var(--rule); text-wrap:balance; scroll-margin-top:24px;
}
h3 {
  font-family:var(--display); font-weight:600; font-size:17px; margin:34px 0 2px;
  scroll-margin-top:24px;
}
h1 + *, h2 + *, h3 + * { margin-top:10px; }
.dochead + h2, .thesis + h2 { border-top:0; padding-top:0; margin-top:40px; }
h1 code, h2 code, h3 code {
  font-size:.86em; background:transparent; border:0; padding:0; letter-spacing:-.01em;
}
p { margin:16px 0; max-width:70ch; }
.anchor {
  margin-left:10px; font-family:var(--mono); font-size:.62em; color:var(--faint);
  opacity:0; transition:opacity .12s;
}
h2:hover .anchor, h3:hover .anchor, .anchor:focus-visible { opacity:1; text-decoration:none; }
ul, ol { margin:16px 0; padding-left:22px; max-width:70ch; }
li { margin:7px 0; }
li > ul, li > ol { margin:7px 0; }
li::marker { color:var(--faint); font-variant-numeric:tabular-nums; }
strong { font-weight:600; }
hr { border:0; border-top:1px solid var(--rule); margin:44px 0; }

code {
  font-family:var(--mono); font-size:.875em; background:var(--code-bg);
  border:1px solid var(--rule); border-radius:4px; padding:.08em .32em;
  word-break:break-word;
}
code.path { background:transparent; border:0; padding:0; color:var(--muted); }
pre {
  margin:0; padding:16px 18px; background:var(--code-bg);
  border:1px solid var(--rule); border-left:2px solid var(--accent);
  border-radius:0 6px 6px 0; font-family:var(--mono); font-size:13px;
  line-height:1.6; overflow-x:auto;
}
pre code { background:none; border:0; padding:0; font-size:inherit; }
.scroll { margin:22px 0; overflow-x:auto; }
blockquote {
  margin:22px 0; padding:2px 0 2px 20px; border-left:2px solid var(--accent);
  color:var(--muted);
}
blockquote p { margin:10px 0; }

table {
  border-collapse:collapse; width:100%; font-size:14px;
  font-variant-numeric:tabular-nums;
}
th {
  text-align:left; font-family:var(--mono); font-weight:500; font-size:11px;
  letter-spacing:.12em; text-transform:uppercase; color:var(--muted);
  padding:0 16px 9px 0; border-bottom:1px solid var(--ink);
  white-space:nowrap;
}
td { padding:11px 16px 11px 0; border-bottom:1px solid var(--rule); vertical-align:top; }
tr:last-child td { border-bottom:0; }
td code { white-space:nowrap; }
.pill {
  display:inline-block; font-family:var(--mono); font-size:10px; font-weight:500;
  letter-spacing:.1em; color:var(--ok); border:1px solid currentColor;
  border-radius:999px; padding:1px 6px; vertical-align:1px;
}

.fig { margin:32px 0; }
.fig .scroll { margin:0; }
.fig svg { width:100%; height:auto; display:block; overflow:visible; }
.fig figcaption { margin-top:14px; font-size:13px; color:var(--muted); max-width:70ch; }
.figdefs { position:absolute; width:0; height:0; }
.figdefs path { fill:var(--faint); }
.fig .b { fill:var(--paper); stroke:var(--rule); stroke-width:1; }
.fig .b.muted { fill:var(--ground); stroke-dasharray:4 3; }
.fig .b.pillbox { fill:var(--ground); }
.fig .b.accent { fill:var(--accent-soft); stroke:var(--accent); }
.fig .b.ok { fill:color-mix(in srgb, var(--ok) 9%, var(--paper)); stroke:var(--ok); }
.fig .b.warn { fill:color-mix(in srgb, var(--warn) 9%, var(--paper)); stroke:var(--warn); }
.fig .b.bad { fill:color-mix(in srgb, var(--fail) 9%, var(--paper)); stroke:var(--fail); }
.fig .b.n-badge { fill:var(--accent-soft); stroke:none; }
.fig .t { fill:var(--ink); font-family:var(--display); font-weight:600; font-size:13px; }
.fig .s { fill:var(--muted); font-family:var(--mono); font-size:10.5px; }
.fig .s.accent { fill:var(--accent); }
.fig .s.mono-strong { fill:var(--ink); font-weight:500; }
.fig .n { fill:var(--accent); font-family:var(--mono); font-size:11px; font-weight:500; }
.fig .l { stroke:var(--faint); fill:none; stroke-width:1.3; stroke-linejoin:round; }
.fig .l.axis, .fig .l.tick { stroke:var(--rule); }
.fig .br { stroke:var(--rule); fill:none; stroke-width:1.3; }
.fig .br.accent { stroke:var(--accent); }

.jobs {
  display:grid; grid-template-columns:repeat(auto-fill, minmax(236px, 1fr));
  gap:12px; margin:26px 0;
}
.job {
  display:block; padding:14px 16px; border:1px solid var(--rule);
  border-radius:8px; background:var(--ground); color:inherit;
}
.job:hover { border-color:var(--accent); text-decoration:none; box-shadow:var(--shadow); }
.job .eyebrow {
  display:block; font-family:var(--mono); font-size:9.5px; letter-spacing:.16em;
  text-transform:uppercase; color:var(--faint);
}
.job .jt {
  display:block; font-family:var(--display); font-weight:600; font-size:14.5px;
  line-height:1.3; margin-top:3px;
}
.job code {
  display:block; margin:9px 0 7px; padding:5px 7px; font-size:11.5px;
  color:var(--accent); background:var(--code-bg); border:1px solid var(--rule);
  border-radius:4px; overflow-x:auto; white-space:nowrap;
}
.job .jh { display:block; font-size:12px; color:var(--muted); line-height:1.4; }

.thesis {
  display:block;
  margin:36px 0 8px; padding:22px 26px; background:var(--ground);
  border:1px solid var(--rule); border-radius:8px;
}
.steps { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; }
.step { display:flex; align-items:center; }
.step + .step::before {
  content:"→"; font-family:var(--mono); font-size:13px; color:var(--faint);
  padding:0 10px; flex:none; align-self:center;
}
.step b {
  display:block; font-family:var(--mono); font-size:11px; letter-spacing:.1em;
  text-transform:uppercase; white-space:nowrap;
}
.step span { display:block; font-size:11px; color:var(--faint); white-space:nowrap; }
.step.llm b { color:var(--accent); }
.step.llm i {
  font-style:normal; font-family:var(--mono); font-size:10px; letter-spacing:.08em;
  color:var(--accent); border:1px solid var(--accent); border-radius:999px;
  padding:1px 7px; margin-left:8px; white-space:nowrap;
}
.thesis .cap {
  margin:16px 0 0; max-width:none; padding-top:14px; border-top:1px solid var(--rule);
  font-size:13px; color:var(--muted);
}

@media (max-width: 900px) {
  .topbar { display:flex; }
  main { margin-left:0; padding:0 20px 80px; }
  .rail {
    transform:translateX(-100%); transition:transform .18s ease;
    box-shadow:var(--shadow); width:min(320px, 86vw);
  }
  body.nav-open .rail { transform:none; }
  .rail .brand { display:none; }
  .dochead { padding-top:34px; }
  .step { flex-basis:100%; padding:6px 0; }
  .step + .step::before { display:none; }
}
@media (prefers-reduced-motion: reduce) { * { transition:none !important; } }
@media print {
  .rail, .topbar { display:none; }
  main { margin:0; }
  .doc[hidden] { display:block !important; }
}
"""

JS = """
(function () {
  var docs = Array.from(document.querySelectorAll('.doc'));
  var guides = Array.from(document.querySelectorAll('.guide'));
  var body = document.body;

  function show(slug, anchor) {
    docs.forEach(function (d) { d.hidden = d.id !== slug; });
    guides.forEach(function (g) {
      if (g.dataset.slug === slug) { g.setAttribute('aria-current', 'page'); }
      else { g.removeAttribute('aria-current'); }
    });
    var el = anchor && document.getElementById(anchor);
    if (el) { el.scrollIntoView(); } else { window.scrollTo(0, 0); }
    body.classList.remove('nav-open');
    spy();
  }

  function route() {
    var hash = decodeURIComponent(location.hash.replace(/^#/, ''));
    if (!hash) { return show(docs[0].id, null); }
    var host = docs.filter(function (d) {
      return hash === d.id || hash.indexOf(d.id + '--') === 0;
    })[0];
    if (host) { return show(host.id, hash === host.id ? null : hash); }
    var el = document.getElementById(hash);
    var owner = el && el.closest('.doc');
    show(owner ? owner.id : docs[0].id, hash);
  }

  var links = Array.from(document.querySelectorAll('.sections a'));
  function spy() {
    var active = docs.filter(function (d) { return !d.hidden; })[0];
    if (!active) { return; }
    var heads = Array.from(active.querySelectorAll('h2'));
    var y = window.scrollY + 90, current = heads[0];
    heads.forEach(function (h) { if (h.offsetTop <= y) { current = h; } });
    links.forEach(function (a) {
      a.classList.toggle('active', !!current && a.hash === '#' + current.id);
    });
  }

  window.addEventListener('hashchange', route);
  window.addEventListener('scroll', function () {
    if (!spy.queued) {
      spy.queued = true;
      requestAnimationFrame(function () { spy.queued = false; spy(); });
    }
  }, { passive: true });

  document.querySelectorAll('[data-nav-toggle]').forEach(function (b) {
    b.addEventListener('click', function () { body.classList.toggle('nav-open'); });
  });

  var root = document.documentElement;
  function paint(mode) {
    if (mode) { root.setAttribute('data-theme', mode); }
    else { root.removeAttribute('data-theme'); }
  }
  try { paint(localStorage.getItem('dbwiki-theme')); } catch (e) {}
  document.querySelectorAll('[data-theme-toggle]').forEach(function (b) {
    b.addEventListener('click', function () {
      var dark = root.getAttribute('data-theme') === 'dark' ||
        (!root.getAttribute('data-theme') &&
         window.matchMedia('(prefers-color-scheme: dark)').matches);
      var next = dark ? 'light' : 'dark';
      paint(next);
      try { localStorage.setItem('dbwiki-theme', next); } catch (e) {}
    });
  });

  route();
})();
"""

THESIS = """
<div class="thesis">
  <div class="steps">
  <div class="step"><div><b>Elasticsearch</b><span>~1M raw log docs</span></div></div>
  <div class="step"><div><b>Compactor</b><span>python, no model</span></div></div>
  <div class="step"><div><b>Digest</b><span>~30 lines a day</span></div></div>
  <div class="step"><div><b>Trigger</b><span>wake or skip</span></div></div>
  <div class="step llm"><div><b>Agent<i>only if notable</i></b><span>judgment, not bookkeeping</span></div></div>
  <div class="step"><div><b>Wiki</b><span>git, markdown, cited</span></div></div>
  </div>
  <p class="cap">The whole architecture in one line: deterministic compaction decides
  <em>what happened</em>, and only then does a model get asked <em>what it means</em>.</p>
</div>
"""


def build(fragment: bool = False) -> str:
    rail, main = [], []
    rail.append(f'<div class="brand">{BRAND}'
                f"<span>{html.escape(SITE_TAG)}</span></div><nav>")
    group = None
    for doc in DOCS:
        title, lede, body, h2s = render((HERE / doc.file).read_text(), doc.slug)
        if doc.group != group:
            group = doc.group
            rail.append(f'<div class="group">{html.escape(doc.group)}</div>')
        rail.append(f'<a class="guide" href="#{doc.slug}" data-slug="{doc.slug}">'
                    f'<span class="eyebrow">{doc.eyebrow}</span>'
                    f'<span class="name">{html.escape(title)}</span>'
                    f'<span class="hook">{html.escape(doc.hook)}</span></a>')
        rail.append('<div class="sections">' + "".join(
            f'<a href="#{h["slug"]}">{html.escape(h["text"])}</a>' for h in h2s)
            + "</div>")
        main.append(
            f'<article class="doc" id="{doc.slug}" hidden>'
            f'<header class="dochead"><div class="eyebrow">{doc.eyebrow}</div>'
            f"<h1>{html.escape(title)}</h1>"
            + (f'<p class="lede">{lede}</p>' if lede else "")
            + "</header>"
            + (THESIS if doc.slug == "overview" else "")
            + body + "</article>")
    rail.append("</nav><footer><span>built from docs/*.md</span>"
                '<button class="tbtn" data-theme-toggle type="button">theme</button>'
                "</footer>")

    markup = (DEFS
              + '<div class="topbar"><button class="tbtn" data-nav-toggle '
              f'type="button">Contents</button>{BRAND}'
              '<button class="tbtn" data-theme-toggle type="button">theme</button>'
              "</div>"
              f'<aside class="rail">{"".join(rail)}</aside>'
              f'<main>{"".join(main)}</main>'
              f"<script>{JS}</script>")
    if fragment:
        return (f"<title>{SITE_TITLE}</title>\n{FONTS}\n<style>{CSS}</style>\n"
                f"{markup}\n")
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n"
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="color-scheme" content="light dark">\n'
            f"<title>{SITE_TITLE}</title>\n{FONTS}\n<style>{CSS}</style>\n"
            f"</head>\n<body>\n{markup}\n</body>\n</html>\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fragment", action="store_true",
                    help="print the page without the html/head/body skeleton")
    ap.add_argument("--out", default=str(HERE / "site.html"))
    args = ap.parse_args()
    if args.fragment:
        print(build(fragment=True))
        return
    Path(args.out).write_text(build())
    print(args.out)


if __name__ == "__main__":
    main()
