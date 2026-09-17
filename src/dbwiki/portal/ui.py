"""The one page, and how it is checked without a browser.

Authored as `portal/workbench.html`, a real HTML file next to this module
(package data; hatchling ships everything under `src/dbwiki`), so it is
edited as HTML and diffed as HTML. Served by `page()`, which substitutes the
single `/*@CSS@*/` placeholder with `daily_html.CSS` followed by
`WORKBENCH_CSS`, and the single `/*@QUIET_DAYS@*/` placeholder with
`readmodel.QUIET_DAYS`, and nothing else: no template language, no
per-request data, no build step. Everything dynamic arrives over the API.

Tested statically by `check_page`, which is what CI runs, and behaviourally
in a browser, which is not. The static check is that the HTML is well formed,
loads nothing external, has no inline event handlers, and names only
endpoints `server.ROUTES` holds. The no-handler half of that check is what
keeps the page down to one inline script, which is what lets `script_src`
hand `server.HEADERS` a hash instead of `'unsafe-inline'`.
"""

import base64
import functools
import hashlib
import re
from collections.abc import Iterable, Sequence
from html.parser import HTMLParser
from importlib import resources

from .. import daily_html, readmodel

WORKBENCH_CSS: str = """
/* The one token this page needs that the daily stylesheet does not define:
   a rule fainter than `--line`, for a gridline that has to be there without
   being counted. It sits between `--line` and `--surface` in each theme. */
:root { --line-2: #ecece6; }
@media (prefers-color-scheme: dark) { :root { --line-2: #2a2d33; } }

/* ---- the tile ----
   Every "label above a value" on this console is a `.tile`, and `.tile.stat`
   is the same tile sized for a number. Three tokens carry the shape, so a
   surface that has to be inline instead — a chip in a heading row, a count
   in a strip, a group's own total — borrows the type and the radius rather
   than becoming a third kind of box. The label is twelve pixels on the nose,
   which is the floor: it is set in capitals, and a capital under twelve is
   where the console starts being read at arm's length rather than at a
   glance. */
:root { --tile-radius: 6px; --tile-label: .75rem; --tile-value: .92rem; }

/* Screens, and everything else the page shows and hides, are `[hidden]`.
   Any author `display` rule beats the UA sheet's `[hidden] { display: none }`,
   so this states once that hidden means hidden and no rule below has to
   remember it. */
[hidden] { display: none !important; }

/* ---- the app shell ----
   The daily page is one column of prose read once; this is a console read
   all day, so it takes a wider measure and a bar that stays put while the
   board under it scrolls. */
body { padding: 0 0 1rem; }
main { max-width: 1200px; padding: 0 1.15rem 5rem; }
main > section { scroll-margin-top: 3.4rem; }

.topbar { position: sticky; top: 0; z-index: 40; background: var(--surface);
  border-bottom: 1px solid var(--line); }
.topbar-in { max-width: 1200px; margin: 0 auto; padding: .4rem 1.15rem;
  display: flex; align-items: center; gap: .35rem .85rem; flex-wrap: wrap; }
.brand { display: inline-flex; align-items: baseline; gap: .4rem;
  font-size: .8rem; font-weight: 650; }
.brand-mark { font-family: var(--mono); color: var(--accent);
  font-size: .9rem; }
.who { margin: 0; font-size: .75rem; color: var(--muted);
  overflow-wrap: anywhere; }
.find input { font: inherit; font-size: .8rem; width: 12rem; max-width: 40vw;
  color: var(--text); background: var(--bg); border: 1px solid var(--line);
  border-radius: 4px; padding: .25rem .5rem; }
.vh { position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip-path: inset(50%); white-space: nowrap; }

header.page { border-bottom: 1px solid var(--line); padding: 1.3rem 0 .9rem;
  margin-bottom: 1.15rem; }
/* The daily page sets its h1 in the mono face; here monospace means "a
   machine wrote this string, copy it exactly", so the page title is set in
   the reading face and the slug under it keeps the mono. */
header.page h1 { font-size: 1.5rem; font-family: inherit; font-weight: 600;
  letter-spacing: -.02em; }
header.page .eyebrow { font-family: var(--mono); letter-spacing: 0;
  text-transform: none; font-size: .78rem; }
header.page .chips { margin-top: .7rem; }

body.acting header.page { padding-bottom: .7rem; margin-bottom: 1rem; }
body.acting header.page h1 { font-size: 1.12rem; line-height: 1.3; }
body.acting header.page .chips { margin-top: .5rem; }
body.acting ol.steps { margin-bottom: .8rem; }

a:focus-visible, button:focus-visible, input:focus-visible,
select:focus-visible, textarea:focus-visible, summary:focus-visible,
[tabindex]:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px;
}
.skip { position: absolute; left: -9999px; }
.skip:focus { position: static; display: inline-block; margin-bottom: .5rem; }

.cap { font-size: var(--tile-label); letter-spacing: .08em; text-transform: uppercase;
       color: var(--muted); font-weight: 600; }
.spacer { flex: 1 1 auto; }

nav.nav { display: flex; flex-wrap: wrap; gap: .15rem; margin: 0; }
.tab { font: inherit; font-size: .82rem; line-height: 1.2; cursor: pointer;
  border: 1px solid transparent; background: none;
  color: var(--muted); border-radius: 4px; padding: .28rem .6rem; }
.tab:hover { background: var(--surface-2); color: var(--text); }
.tab[aria-current="page"] { background: var(--surface-2); color: var(--text);
  font-weight: 650; box-shadow: inset 0 -2px 0 var(--accent); }

/* ---- the queue board ----
   A lens bar, the rows themselves, and under them one card that says the
   shape of the queue: what is in each group of the current lens, and when
   each incident opened. */
.lens { display: flex; align-items: center; gap: .5rem .8rem;
  flex-wrap: wrap; margin: 0 0 .4rem; }
.lens-bar { display: inline-flex; flex-wrap: wrap; gap: 1px;
  border: 1px solid var(--line); border-radius: 5px; padding: 1px;
  background: var(--surface); }
.lens-tab { font: inherit; font-size: .82rem; line-height: 1.2;
  cursor: pointer; border: 0; background: none; color: var(--muted);
  border-radius: 4px; padding: .3rem .7rem; }
.lens-tab:hover { background: var(--surface-2); color: var(--text); }
.lens-tab[aria-pressed="true"] { background: var(--accent); color: var(--bg);
  font-weight: 650; }
.toggle { font: inherit; font-size: .8rem; line-height: 1.2; cursor: pointer;
  display: inline-flex; align-items: baseline; gap: .4rem;
  border: 1px solid var(--line); background: var(--surface);
  color: var(--muted); border-radius: 5px; padding: .3rem .7rem; }
.toggle:hover { border-color: var(--muted); color: var(--text); }
.toggle .mk { font-family: var(--mono); }
.toggle[aria-pressed="true"] { color: var(--text); font-weight: 650;
  border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.lens-said { margin: 0 0 1rem; font-size: .82rem; color: var(--muted); }

.board { margin: 0 0 1.3rem; }
.panel { border: 1px solid var(--line); border-radius: 8px;
  background: var(--surface); padding: .8rem .9rem .9rem; min-width: 0; }
.panel .section-cap { margin-bottom: .7rem; }

/* ---- the queue at a glance ----
   One bar, a segment per group, each as wide a share of it as the group is
   of the queue. A colour is the status where the lens has one and a rank in
   the group order where it has not, and no segment is told apart by colour
   alone: the wide ones print their own name and count under themselves, the
   narrow ones are named in the legend row beside them. */
.glance-svg { display: block; height: auto; margin-bottom: .3rem; }
.glance-svg .gseg { cursor: pointer; }
.glance-svg .gname { font-size: 12px; fill: var(--muted);
  font-family: var(--mono); }
.glance-svg .seg { fill: var(--mhue); }
.glance-svg .seg.s-open { fill: var(--red-line); }
.glance-svg .seg.s-monitoring { fill: var(--amber-line); }
.glance-svg .seg.s-resolved { fill: var(--green-line); }
.glance-svg .gseg:hover .seg { stroke: var(--text); stroke-width: 1.5; }
.glance-svg .gseg.picked .seg { stroke: var(--accent); stroke-width: 2.5; }
.glance-svg .gseg.picked .gname { fill: var(--accent); font-weight: 650; }
.glance-svg .gseg:focus-visible { outline: 2px solid var(--accent);
  outline-offset: 2px; }
ul.glance-legend .sw.s-open { background: var(--red-line); }
ul.glance-legend .sw.s-monitoring { background: var(--amber-line); }
ul.glance-legend .sw.s-resolved { background: var(--green-line); }

/* Drawn at one user unit to the pixel, so every size here is the size the
   reader measures. Two rules under the axis and never one: the weeks are
   what the eye counts a gap in, the months are what it names a date by. */
.when-svg { display: block; height: auto; }
.when-svg .axis { stroke: var(--line); stroke-width: 1.5; }
.when-svg .wgrid { stroke: var(--line-2); stroke-width: 1; }
.when-svg .mgrid { stroke: var(--line); stroke-width: 1; }
.when-svg .mname { fill: var(--muted); font-size: 12px;
  font-family: var(--mono); }
.when-svg .tick-text { fill: var(--muted); font-size: 12px;
  font-family: var(--mono); }
.when-svg .mark { cursor: pointer; }
.when-svg .mark-glyph { font-size: 14px; }
.when-svg .mlabel { font-size: 12px; fill: var(--text);
  dominant-baseline: middle; }
.when-svg .stem { stroke-width: 1.5; }
.when-svg .mark.s-open .mark-glyph { fill: var(--red); }
.when-svg .mark.s-monitoring .mark-glyph { fill: var(--amber); }
.when-svg .mark.s-resolved .mark-glyph { fill: var(--green); }
.when-svg .mark.s-open .stem { stroke: var(--red-line); }
.when-svg .mark.s-monitoring .stem { stroke: var(--amber-line); }
.when-svg .mark.s-resolved .stem { stroke: var(--green-line); }
.when-svg .mark:hover .mark-glyph { fill: var(--accent); }
.when-svg .mark:hover .mlabel { fill: var(--accent); }
.when-svg .mark:focus-visible { outline: 2px solid var(--accent);
  outline-offset: 2px; }
p.when-note { margin: .3rem 0 0; color: var(--muted); font-size: .78rem; }
ul.when-list { list-style: none; margin: .5rem 0 0; padding: 0;
  font-size: .78rem; }
ul.when-list li { display: flex; gap: .45rem; align-items: baseline;
  padding: .07rem 0; min-width: 0; }
ul.when-list .mk { font-family: var(--mono); flex: none; }
ul.when-list .g-open { color: var(--red); }
ul.when-list .g-monitoring { color: var(--amber); }
ul.when-list .g-resolved { color: var(--green); }
ul.when-list .day { font-family: var(--mono); color: var(--muted);
  flex: none; }
ul.when-list .what { min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
ul.when-list .more { color: var(--muted); }

/* ---- message heat ----
   One card, and one row per database in it made of three bands: errors,
   warnings, the lines nothing matched. The three share one axis of calendar
   days, so a column is one day of one database read three ways and the eye
   does not carry it across the screen. The hue is a custom property set by
   one class, so a cell's `fill` and its legend swatch's `background` are the
   same colour by construction; a step is an opacity of it, five of them, and
   the faintest is still a fill so that a day counted at zero reads as
   observed. A day with no digest is the one cell with no fill at all: a
   dashed outline, which cannot be mistaken for the quiet end of a ramp
   however the monitor is calibrated. The five opacities are spaced widest at
   the bottom, where a step is nearly a doubling, because that is the end
   where the eye separates them worst.

   The map is drawn 1120 units wide in a box about that many pixels across,
   so a size declared here is the size on the screen. */
.heat { margin: 0 0 1.3rem; }
.heat-svg { display: block; height: auto; }
.heat-svg .hband { fill: var(--text); font-size: 12px; font-weight: 650; }
.heat-svg .hsep { stroke: var(--line); stroke-width: 1; }
/* `all`, so the label answers a pointer over its whole line box and not only
   where a glyph is inked: a four-letter name is a small target already, and
   half of it is the gaps between the letters. */
.heat-svg .hlabel { fill: var(--muted); font-size: 12px;
  font-family: var(--mono); pointer-events: all;
  dominant-baseline: middle; }
.heat-svg .hopen { cursor: pointer; }
.heat-svg .hopen:hover .hlabel { fill: var(--accent); }
.heat-svg .hopen:focus-visible { outline: 2px solid var(--accent);
  outline-offset: 1px; }
.heat-svg .tick-text { fill: var(--muted); font-size: 12px;
  font-family: var(--mono); }
.h-red { --hue: var(--red-line); }
.h-amber { --hue: var(--amber-line); }
.h-slate { --hue: var(--accent); }
.hcell { fill: var(--hue); cursor: pointer; }
.hcell.blank { fill: none; stroke: var(--line); stroke-width: 1;
  stroke-dasharray: 1.5 1.5; }
.hcell:hover { stroke: var(--text); stroke-width: 1; }
.hcell.s0 { fill-opacity: .10; }
.hcell.s1 { fill-opacity: .28; }
.hcell.s2 { fill-opacity: .48; }
.hcell.s3 { fill-opacity: .72; }
.hcell.s4 { fill-opacity: 1; }
ul.heat-legend { list-style: none; margin: .5rem 0 0; padding: 0;
  display: flex; flex-wrap: wrap; align-items: center; gap: .2rem .6rem;
  font-size: .75rem; color: var(--muted); }
ul.heat-legend li { display: inline-flex; align-items: center; gap: .3rem; }
.hsw { display: inline-block; width: 14px; height: 14px; border-radius: 2px;
  background: var(--hue); }
.hsw.blank { background: none; border: 1px dashed var(--line); }
.hsw.ramp { background: var(--text); }
.hsw.s0 { opacity: .10; }
.hsw.s1 { opacity: .28; }
.hsw.s2 { opacity: .48; }
.hsw.s3 { opacity: .72; }
.hsw.s4 { opacity: 1; }


/* ---- what the agent loops did ----
   The colour is a custom property set by one class, the heat maps' rule: a
   bar's `fill` and its legend swatch's `background` are the same colour by
   construction rather than two literals that could drift. Six hues, and none
   of them red — red is the outline a rolled-back stage carries, and a model
   that happened to draw red would read as a failure. The three borrowed from
   the palette are the `-line` tokens, which are one value in both themes on
   purpose; the three literals are picked to sit at the same weight. */
.m-0 { --mhue: var(--accent); }
.m-1 { --mhue: var(--green-line); }
.m-2 { --mhue: var(--amber-line); }
.m-3 { --mhue: #8f6fd0; }
.m-4 { --mhue: #3f9aad; }
.m-5 { --mhue: #c96fa8; }

ul.legend { list-style: none; margin: .2rem 0 .5rem; padding: 0;
  display: flex; flex-wrap: wrap; align-items: center; gap: .2rem .8rem;
  font-size: .75rem; color: var(--muted); }
ul.legend li { display: inline-flex; align-items: center; gap: .35rem; }
.sw { display: inline-block; width: 14px; height: 14px; border-radius: 2px;
  background: var(--mhue); flex: none; margin-right: .35rem; }

/* ---- every tick, by how long it ran ----
   Drawn at 1120 units across, which is what the card is wide at the
   console's own measure, so a `font-size` declared in the SVG is the size on
   the screen. Nothing on it is under twelve: the chart is read at arm's
   length beside a table set at thirteen. */
.card { background: var(--surface); border: 1px solid var(--line);
  border-radius: 6px; padding: .7rem .9rem .4rem; }
.tick-svg { display: block; height: auto; }
.tick-svg text { font-family: var(--mono); fill: var(--text); }
.tick-svg .mu { fill: var(--muted); }
.tick-svg .turned { font-weight: 650; }
.tick-svg .grid { stroke: var(--line-2); }
.tick-svg .dgrid { stroke: var(--line); }
.tick-svg .axis { stroke: var(--muted); }
.tick-svg .mbar { fill: var(--mhue); }
/* The outline is the tick's, not the stage's: a failed stage keeps its
   model's colour, because recolouring it would answer "which model" with
   "it went wrong". */
.tick-svg .edge { fill: none; stroke: var(--red-line); stroke-width: 2; }
.tick-svg .tbar { cursor: pointer; }
.tick-svg .tbar:hover .mbar, .tick-svg .tbar:focus-visible .mbar {
  stroke: var(--text); stroke-width: 1; }
.tick-svg .tbar:focus { outline: none; }
ul.legend .sw.edge { background: none; border: 2px solid var(--red-line); }

/* ---- which model did the work ----
   The name column is the one that will not fit a 1280 screen beside ten
   measures, so it is two lines rather than one wrapped one: the name a
   person says, and the address it was downloaded from under it, wrapping
   inside its own cell and never squeezing the table. */
#agents-models td.model { white-space: normal; max-width: 22rem; }
#agents-models td.model .nm { font-family: var(--mono); font-weight: 650; }
#agents-models td.model .full { display: block; font-size: var(--tile-label);
  color: var(--muted); font-family: var(--mono); overflow-wrap: anywhere; }
#agents-models td.num.bad { color: var(--red); font-weight: 650; }
#agents-models td.num.warn { color: var(--amber); font-weight: 650; }
#agents-models td.num.quiet { color: var(--muted); }

/* ---- a table folded by day ----
   One row per record under the day it happened. The day is a row and not a
   repeated column, because ninety-five ticks in a week are read as five days
   of nineteen and never as ninety-five dates. */
tr.day td { background: var(--surface-2); font-weight: 650;
  font-size: var(--tile-label); letter-spacing: .05em; text-transform: uppercase;
  color: var(--muted); padding: .3rem .75rem; }
/* The disclosure on a folded day carries the day's own header type, so an
   open day and a shut one read as the same row; the counts after the name
   drop out of capitals, because a sentence set in them at twelve pixels is
   a sentence nobody reads. */
tr.day .dayx { font: inherit; color: inherit;
  letter-spacing: inherit; text-transform: inherit; font-weight: inherit;
  cursor: pointer; border: 0; background: none; padding: 0; text-align: left;
  display: inline-flex; flex-wrap: wrap; align-items: baseline; gap: .55rem; }
tr.day .dayx:hover { color: var(--text); }
tr.day .dayx .tw { font-family: var(--mono); }
tr.day .dayx .sum { text-transform: none; letter-spacing: 0;
  font-weight: 500; font-family: var(--mono); }

#agents-ledger td.when { font-family: var(--mono); color: var(--muted); }
#agents-ledger .tid { font-family: var(--mono); font-weight: 650;
  color: var(--accent); text-decoration: none; }
#agents-ledger .tid:hover { text-decoration: underline; }
#agents-ledger td.wrote { white-space: normal; display: flex;
  flex-wrap: wrap; gap: .25rem; }
.t-ok { color: var(--green); font-weight: 650; }
.t-bad { color: var(--red); font-weight: 650; }
.t-warn { color: var(--amber); font-weight: 650; }
p.cov { font-size: .78rem; color: var(--muted); margin: .8rem 0 0; }

.ichip { font-size: .76rem; text-decoration: none; color: var(--text);
  border: 1px solid var(--line); border-left-width: 4px;
  border-radius: 4px; padding: .1rem .45rem; }
.ichip:hover { border-color: var(--accent); border-left-color: var(--accent); }
.ichip.db { font-family: var(--mono); font-size: var(--tile-label); padding: 0 .35rem;
  background: var(--surface-2); }
.ichip.s-open { border-left-color: var(--red-line); }
.ichip.s-monitoring { border-left-color: var(--amber-line); }
.ichip.s-resolved { border-left-color: var(--green-line); }

/* One row per stage rather than a run-on line per tick: duration, model and
   outcome are each a column, so a failed stage is found by reading down one
   column instead of across every sentence. */
.touches { margin: 0 0 1rem; }
.touches .tid { font-family: var(--mono); font-weight: 650;
  color: var(--accent); text-decoration: none; }
.touches .tid:hover { text-decoration: underline; }
.touches td.when { font-family: var(--mono); color: var(--muted); }
.touches td.tstage { font-weight: 650; }
.touches tr.same td.db { color: var(--accent); }
.touches td.said { color: var(--muted); white-space: normal; }
/* The model name is the one cell long enough to push the table into a
   horizontal scroller at 1280, so it wraps rather than the reader scrolling
   to reach the outcome column. */
.touches td.model { white-space: normal; overflow-wrap: anywhere;
  min-width: 11rem; }
.touches td.measure { width: 120px; }
.touches td.measure .bar { display: block; height: 16px; border-radius: 2px;
  background: var(--accent); opacity: .8; }
.touches td.measure .bar.t-red { background: var(--red-line); }
.touches td.measure .bar.t-amber { background: var(--amber-line); }
.touches td.measure .bar.t-green { background: var(--green-line); }
.touches td.measure .bar.t-quiet { background: var(--muted);
  opacity: .45; }
.touches td.outcome { font-weight: 650; }
.touches td.o-green { color: var(--green); }
.touches td.o-red { color: var(--red); }
.touches td.o-amber { color: var(--amber); }
.touches td.o-quiet { color: var(--muted); }

li.group-head { display: flex; align-items: baseline; gap: .5rem .7rem;
  flex-wrap: wrap; margin: 1.1rem 0 .4rem; padding-bottom: .25rem;
  border-bottom: 1px solid var(--line); }
ul.board-rows > li.group-head:first-child { margin-top: 0; }
li.group-head .gname { font-size: .88rem; font-weight: 650;
  overflow-wrap: anywhere; }
li.group-head .gcount { font-family: var(--mono);
  font-variant-numeric: tabular-nums; font-size: var(--tile-value);
  color: var(--muted); border: 1px solid var(--line);
  border-radius: var(--tile-radius); padding: 0 .45rem; }
li.group-head .gsplit { font-size: .76rem; color: var(--muted); }

/* The card is the list item and the button is its top half: the code chips
   below the button are links to their error pages, and an anchor nested in a
   button is neither valid markup nor reachable by a click. */
li.qitem { display: grid; gap: .22rem; background: var(--surface);
  color: var(--text); border: 1px solid var(--line);
  border-left: 5px solid var(--line); border-radius: 6px;
  padding: .6rem .85rem; }
li.qitem:hover { background: var(--surface-2); border-color: var(--muted); }
li.qitem.s-open { border-left-color: var(--red-line); }
li.qitem.s-monitoring { border-left-color: var(--amber-line); }
li.qitem.s-resolved { border-left-color: var(--green-line); }
.qrow { display: grid; width: 100%; text-align: left; font: inherit;
  cursor: pointer; background: none; color: inherit; border: 0;
  padding: 0; gap: .22rem; }
.qrow .qhead { display: flex; align-items: baseline; gap: .5rem;
  flex-wrap: wrap; min-width: 0; }
.qrow .name { font-weight: 650; min-width: 0; overflow-wrap: anywhere;
  flex: 1 1 18rem; }
.qrow .qmeta { display: flex; align-items: baseline; gap: .3rem .8rem;
  flex-wrap: wrap; font-size: .8rem; color: var(--muted); }
.qrow .db { font-family: var(--mono); font-weight: 650; color: var(--text); }
.qrow .when, .qrow .verdict { font-family: var(--mono); }
.qrow .age { color: var(--muted); }
.qitem .codes { display: flex; flex-wrap: wrap; gap: .25rem;
  margin-top: .1rem; }
.qitem .code { font-family: var(--mono); font-size: .75rem;
  color: var(--muted); border: 1px solid var(--line); border-radius: 3px;
  padding: 0 .3rem; text-decoration: none; }
.qitem a.code:hover { color: var(--text); border-color: var(--accent); }
.qrow .dirty { font-size: var(--tile-label); letter-spacing: .06em;
  text-transform: uppercase; font-weight: 700; color: var(--amber);
  border: 1px dashed currentColor; border-radius: 3px; padding: 0 .32rem; }
.qrow .quiet, #incident-quiet { font-size: var(--tile-label); letter-spacing: .06em;
  text-transform: uppercase; font-weight: 650; color: var(--muted);
  border: 1px solid var(--line); border-radius: 3px; padding: 0 .32rem; }

.owner { display: inline-flex; align-items: baseline; gap: .4rem;
  flex-wrap: wrap; min-width: 0; }
.owner .sha { font-family: var(--mono); font-size: .78rem;
  color: var(--muted); overflow-wrap: anywhere; }
.strip { display: flex; flex-wrap: wrap; align-items: baseline;
  gap: .35rem .9rem; font-size: .82rem; color: var(--muted);
  min-width: 0; overflow-wrap: anywhere; }
.strip b { font-family: var(--mono); color: var(--text); font-weight: 650;
  font-size: var(--tile-value); }
.section-cap { display: flex; align-items: baseline; gap: .5rem;
  flex-wrap: wrap; margin: 0 0 .45rem; min-width: 0; overflow-wrap: anywhere; }

.btn { font: inherit; font-size: .88rem; line-height: 1.2; cursor: pointer;
  border: 1px solid var(--line); background: var(--surface);
  color: var(--text); border-radius: 4px; padding: .42rem .8rem; }
.btn:hover:not(:disabled) { background: var(--surface-2);
  border-color: var(--muted); }
.btn:disabled { cursor: not-allowed; color: var(--muted);
  background: var(--surface-2); }
/* `--bg` on `--accent`, never white: in dark mode `--accent` is #8ab4f8 and
   white on it is about 2.1:1. `--bg` reads 5.9:1 light and 8.7:1 dark, and
   it is a token the daily page already defines. */
.btn.primary { background: var(--accent); border-color: var(--accent);
  color: var(--bg); font-weight: 650; }
.btn.primary:hover:not(:disabled) { background: var(--accent);
  border-color: var(--accent); filter: brightness(1.08); }
.btn.primary:disabled { background: var(--surface-2);
  border-color: var(--line); color: var(--muted); font-weight: 500; }
.btn.small { font-size: .78rem; padding: .22rem .55rem; }
.actions { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; }

.notice { border: 1px solid var(--line); border-left: 5px solid var(--line);
  border-radius: 6px; background: var(--surface); padding: .7rem .9rem;
  margin: 0 0 1rem; font-size: .88rem; }
.notice .top { display: flex; align-items: baseline; gap: .5rem;
  flex-wrap: wrap; margin: 0 0 .25rem; }
.notice p { margin: .25rem 0 0; }
.notice ul { margin: .35rem 0 0; padding-left: 1.1rem;
  font-family: var(--mono); font-size: .8rem; overflow-wrap: anywhere; }
.notice .recipe { margin: .45rem 0 0; padding: .4rem .55rem;
  background: var(--surface-2); border: 1px solid var(--line);
  border-radius: 4px; font-family: var(--mono); font-size: .78rem;
  white-space: pre-wrap; overflow-wrap: anywhere; }
.notice.n-stop { border-left-color: var(--red-line); }
.notice.n-wait { border-left-color: var(--amber-line); }
.notice.n-done { border-left-color: var(--green-line); }
.notice .actions { margin-top: .55rem; }

ul.rows { list-style: none; margin: 0; padding: 0; }
ul.rows li { margin: 0 0 .5rem; }
button.row { display: flex; width: 100%; text-align: left; gap: .5rem .8rem;
  flex-wrap: wrap; align-items: baseline; font: inherit; cursor: pointer;
  background: var(--surface); color: var(--text);
  border: 1px solid var(--line); border-left: 5px solid var(--line);
  border-radius: 6px; padding: .7rem .9rem; }
button.row:hover { background: var(--surface-2); border-color: var(--muted); }
button.row.s-open { border-left-color: var(--red-line); }
button.row.s-monitoring { border-left-color: var(--amber-line); }
button.row.s-resolved { border-left-color: var(--green-line); }
button.row .name { font-weight: 650; flex: 1 1 22rem; min-width: 0;
  overflow-wrap: anywhere; }
button.row .db { font-family: var(--mono); font-weight: 650;
  font-size: .85rem; }
button.row .when, button.row .verdict { color: var(--muted);
  font-size: .82rem; font-family: var(--mono); }
button.row .dirty { font-size: var(--tile-label); letter-spacing: .06em;
  text-transform: uppercase; font-weight: 700; color: var(--amber);
  border: 1px dashed currentColor; border-radius: 3px; padding: .02rem .34rem; }

details.context { border: 1px solid var(--line);
  border-left: 5px solid var(--amber-line); border-radius: 6px;
  background: var(--surface); margin: 0 0 1.1rem; }
details.context > summary { cursor: pointer; padding: .65rem .9rem;
  font-size: .92rem; display: flex; align-items: baseline; gap: .5rem;
  flex-wrap: wrap; list-style: none; }
details.context > summary::-webkit-details-marker { display: none; }
details.context > summary::marker { content: ""; }
details.context > summary .tw { font-family: var(--mono);
  color: var(--muted); flex: none; }
details.context[open] > summary .tw::after { content: "\\2212"; }
details.context:not([open]) > summary .tw::after { content: "+"; }
details.context[open] > summary { border-bottom: 1px solid var(--line); }
details.context .in { padding: .85rem .95rem; }

ul.forag { list-style: none; margin: .35rem 0 0; padding: 0;
  font-size: .86rem; }
ul.forag li { display: flex; gap: .5rem; padding: .12rem 0; }
ul.forag li .mk { font-family: var(--mono); font-weight: 700; flex: none;
  width: 1.5rem; text-align: center; border-radius: 3px; }
ul.forag.for li .mk { color: var(--green); background: var(--green-bg); }
ul.forag.against li .mk { color: var(--red); background: var(--red-bg); }

/* The action bar. A verb the principal holds the role for is a solid button
   with an accent underline; one they do not is dotted and says which role it
   wants, rather than vanishing — an operator has to be able to see what this
   incident could take and who could take it. */
ul.kinds { display: flex; flex-wrap: wrap; gap: .4rem; margin: 0 0 .4rem;
  padding: 0; list-style: none; }
.kind { display: inline-flex; align-items: center; gap: .45rem; font: inherit;
  font-size: .88rem; font-weight: 650; cursor: pointer;
  border: 1px solid var(--line); background: var(--surface);
  color: var(--text); border-radius: 5px;
  padding: .42rem .85rem .38rem; border-bottom-width: 3px;
  border-bottom-color: var(--accent); }
.kind:hover:not(:disabled) { border-color: var(--muted);
  border-bottom-color: var(--accent); background: var(--surface-2); }
.kind .mark { font-family: var(--mono); color: var(--muted); }
.kind:disabled { cursor: not-allowed; color: var(--muted); font-weight: 500;
  background: var(--surface-2); border-bottom-style: dotted;
  border-bottom-color: var(--line); }
.kind .role { font-size: var(--tile-label); letter-spacing: .06em;
  text-transform: uppercase; color: var(--muted); }

ol.steps { display: flex; gap: 0; margin: 0 0 1.1rem; padding: 0;
  list-style: none; border: 1px solid var(--line); border-radius: 6px;
  background: var(--surface); overflow: hidden; }
ol.steps li { flex: 1 1 0; display: flex; align-items: center; gap: .55rem;
  padding: .6rem .8rem; border-right: 1px solid var(--line); min-width: 0; }
ol.steps li:last-child { border-right: 0; }
ol.steps li .disc { flex: none; width: 1.55rem; height: 1.55rem;
  border-radius: 50%; border: 1px solid var(--line); display: grid;
  place-items: center; font-family: var(--mono); font-size: .82rem;
  font-weight: 650; color: var(--muted); background: var(--surface-2); }
ol.steps li .nm { font-size: .86rem; color: var(--muted); }
ol.steps li .sub { display: block; font-size: .75rem; color: var(--muted); }
ol.steps li.done { background: var(--green-bg); }
ol.steps li.done .disc { border-color: var(--green-line);
  color: var(--green); background: var(--surface); }
ol.steps li.done .nm { color: var(--text); }
ol.steps li.now { background: var(--surface-2);
  box-shadow: inset 0 -3px 0 var(--accent); }
ol.steps li.now .disc { border-color: var(--accent); color: var(--bg);
  background: var(--accent); }
ol.steps li.now .nm { color: var(--text); font-weight: 650; }
ol.steps li.ahead .disc { border-style: dashed; }
@media (max-width: 620px) {
  ol.steps { flex-wrap: wrap; }
  ol.steps li { flex-basis: 100%; border-right: 0;
    border-bottom: 1px solid var(--line); }
}

/* ---- the incident case file ----
   Five facts, the window as a length of time, and every act in the order
   they were taken. The ribbon down the left is the status, and the badge in
   the bar says the same thing in a word, so neither is load-bearing alone. */
dl.tiles { display: grid; gap: .5rem; margin: 0 0 .8rem;
  grid-template-columns: repeat(auto-fit, minmax(8.5rem, 1fr)); }
.tile { border: 1px solid var(--line); border-radius: var(--tile-radius);
  background: var(--surface-2); padding: .45rem .6rem; min-width: 0; }
.tile dt { font-size: var(--tile-label); letter-spacing: .07em;
  text-transform: uppercase; color: var(--muted); font-weight: 650; }
.tile dd { margin: .1rem 0 0; font-size: var(--tile-value); font-weight: 650;
  overflow-wrap: anywhere; }

/* The same tile, sized for a number instead of a phrase: the value is the
   thing read across the row, so it takes the mono face at twice the label's
   size and the sample under it stays a whisper. Seven across at the console's
   width, three when the window is narrow enough that seven would be columns
   of two characters. */
dl.tiles.stats { grid-template-columns: repeat(7, minmax(0, 1fr)); }
/* A screen that opens with five answers gets five columns rather than seven
   with two of them empty: the row is read as one shape, and a gap in it
   reads as a measure that failed to load. */
dl.tiles.stats.five { grid-template-columns: repeat(5, minmax(0, 1fr)); }
@media (max-width: 980px) {
  dl.tiles.stats, dl.tiles.stats.five {
    grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
.tile.stat { background: var(--surface); }
.tile.stat dd { font-weight: 400; }
.tile.stat .v { display: block; font-family: var(--mono); font-size: 1.35rem;
  font-weight: 650; line-height: 1.2; font-variant-numeric: tabular-nums; }
.tile.stat .s { display: block; font-size: var(--tile-label); color: var(--muted); }
/* A count nobody wants above zero is red only while it is above zero, so the
   colour is a reading of the number and never a permanent label on the
   tile. The word in the tile says the same thing with the colour off. */
.tile.stat.hot { border-color: var(--red-line); background: var(--red-bg); }
.tile.stat.hot .v { color: var(--red); }

/* The daily page draws a chip as a pill in a heading. Here a chip stands
   beside tiles that answer the same kind of question, so it takes the tile's
   corner and the tile's two type sizes and reads as one of them laid on its
   side, value first. */
ul.chips .chip { border-radius: var(--tile-radius);
  background: var(--surface-2); padding: .2rem .6rem;
  font-size: var(--tile-label); }
ul.chips .chip b { font-size: var(--tile-value); }
#chips .chip > span { letter-spacing: .07em; text-transform: uppercase;
  font-weight: 650; }

.chipline { display: flex; flex-wrap: wrap; align-items: baseline;
  gap: .3rem .5rem; margin: 0 0 1.3rem; }
.chipline .code { font-family: var(--mono); font-size: .78rem;
  border: 1px solid var(--line); border-radius: 3px; padding: 0 .35rem;
  text-decoration: none; }
.chipline a.code:hover { border-color: var(--accent); }
.chipline .code.nolink { border-style: dashed; }

/* What the wiki knows about each code the incident names. The unresearched
   card is the point of the region, so its badge is drawn as an absence — a
   dashed outline and no fill — rather than as a fourth status colour
   competing with the three the page already spends on incident state. */
.research { display: grid; gap: .5rem; margin: 0 0 .5rem;
  align-items: start;
  grid-template-columns: repeat(auto-fit, minmax(19rem, 1fr)); }
.rcard { border: 1px solid var(--line); border-radius: 6px;
  background: var(--surface); padding: .55rem .7rem; min-width: 0; }
.rcard.thin { border-style: dashed; background: none; }
.rcard .rtop { display: flex; align-items: baseline; gap: .5rem;
  flex-wrap: wrap; }
.rcard .code { font-family: var(--mono); font-weight: 650;
  font-size: .9rem; text-decoration: none; }
.rcard a.code:hover { text-decoration: underline; }
.rcard .code.nolink { color: var(--muted); text-decoration: line-through; }
.badge.b-unresearched { color: var(--muted); background: none;
  border-style: dashed; }
.rcard dl.rbody { margin: .45rem 0 0; }
.rcard dl.rbody dt { font-size: var(--tile-label); letter-spacing: .07em;
  text-transform: uppercase; color: var(--muted); font-weight: 650; }
.rcard dl.rbody dd { margin: .1rem 0 .45rem; font-size: .85rem;
  overflow-wrap: anywhere; }
.rcard dl.rbody dd:last-child { margin-bottom: 0; }
.rcard dl.rbody dd::first-letter { text-transform: uppercase; }
.rcard .rsrc { margin: .45rem 0 0; font-size: .75rem; color: var(--muted);
  overflow-wrap: anywhere; }
.rcard .rsrc .src { font-family: var(--mono); }
.rcard .rnotes { margin: .45rem 0 0; font-size: .75rem;
  overflow-wrap: anywhere; }
.rcard .rnotes .cap { color: var(--muted); }
.rcard .rnotes ul { margin: .2rem 0 0; padding-left: 1.1rem; }
.rcard .rnotes li { margin: .1rem 0 0; }
/* The note is prose and its provenance is not, so the source drops to its
   own muted line under the note rather than running on as the end of the
   sentence, and it is led the way the card's own source line is led. The
   slug keeps the link colour, so it still reads as somewhere to go. */
.rcard .rnotes .nsrc { display: block; color: var(--muted); }
.rcard .rnotes .src { font-family: var(--mono); }
.rcard .rfix { margin: .45rem 0 0; font-size: .75rem;
  overflow-wrap: anywhere; }
.rcard .rfix .cap { color: var(--muted); }
.rcard .rfix ul { margin: .2rem 0 0; padding-left: 1.1rem; }
.rcard .rfix li { margin: .1rem 0 0; }

/* The reading above the drawing: what an operator would conclude, in a size
   that is read before the cells are counted. */
.win-said { margin: 0 0 .6rem; font-size: 1.1rem; line-height: 1.35;
  max-width: 62ch; text-wrap: balance; }
.window { margin: 0 0 1.4rem; }
.win-svg { display: block; height: auto; }
/* `-line` and not the theme's `--amber`: the two `-line` tokens are one
   value in both themes, which is what keeps a filled day legible on the
   dark surface as well as the light one. */
.win-run { fill: var(--amber-line); }
.win-todo { fill: url(#win-hatch); stroke: var(--line); stroke-width: 1; }
.win-hatch { stroke: var(--line); stroke-width: 2; }
.win-daytick { stroke: var(--bg); stroke-width: 1; }
.win-now { stroke: var(--text); stroke-width: 1.5; stroke-dasharray: 3 2; }
.win-nowsaid { fill: var(--text); font-size: 12px; font-weight: 650;
  font-family: var(--mono); }
.win-day { fill: var(--muted); font-size: 12px; font-family: var(--mono); }
.win-end { fill: var(--muted); font-size: 12px; font-family: var(--mono); }

ol.acts { list-style: none; margin: 0 0 1.4rem; padding: 0; }
li.act { display: flex; gap: .7rem; padding: 0 0 .8rem; position: relative; }
li.act:last-child { padding-bottom: 0; }
/* The rail is the thing that makes a list of records a sequence; it stops at
   the last disc rather than trailing off under nothing. */
li.act:not(:last-child)::before { content: ""; position: absolute;
  left: .87rem; top: 1.9rem; bottom: .2rem; width: 1px;
  background: var(--line); }
li.act .disc { flex: none; width: 1.75rem; height: 1.75rem;
  border-radius: 50%; border: 1px solid var(--line);
  background: var(--surface-2); display: grid; place-items: center;
  font-family: var(--mono); font-size: .85rem; color: var(--muted);
  position: relative; z-index: 1; }
li.act .act-in { min-width: 0; flex: 1 1 auto; }
li.act .act-top { display: flex; align-items: baseline; gap: .45rem;
  flex-wrap: wrap; }
li.act .verb { font-weight: 650; font-size: .9rem; }
li.act .when { font-family: var(--mono); font-size: .76rem;
  color: var(--muted); }
li.act .headline { margin: .2rem 0 0; }
li.act p.meta { font-size: .8rem; overflow-wrap: anywhere; }

.stage { background: var(--surface); border: 1px solid var(--line);
  border-radius: 6px; }
.stage.s-open { border-left: 5px solid var(--red-line); }
.stage.s-monitoring { border-left: 5px solid var(--amber-line); }
.stage.s-resolved { border-left: 5px solid var(--green-line); }
.stage > .bar { display: flex; align-items: baseline; gap: .5rem;
  flex-wrap: wrap; padding: .55rem .95rem; background: var(--surface-2);
  border-bottom: 1px solid var(--line); border-radius: 6px 6px 0 0; }
.stage > .in { padding: 1rem 1.05rem; }
.stage > .foot { position: sticky; bottom: 0; z-index: 10;
  border-top: 1px solid var(--line); padding: .7rem .95rem;
  background: var(--surface-2); border-radius: 0 0 6px 6px; }

.field { margin: 0 0 .9rem; }
.field > .head { display: flex; align-items: baseline; gap: .45rem;
  flex-wrap: wrap; margin: 0 0 .22rem; }
.field > .head label { font-size: var(--tile-label); letter-spacing: .08em;
  text-transform: uppercase; font-weight: 650; color: var(--text); }
.field .req { font-size: var(--tile-label); letter-spacing: .06em;
  text-transform: uppercase; font-weight: 700; color: var(--red);
  border: 1px solid currentColor; background: var(--red-bg);
  border-radius: 3px; padding: .02rem .3rem; }
.field .opt { font-size: .75rem; color: var(--muted); }
.field .flag { font-family: var(--mono); font-size: .75rem;
  color: var(--muted); }
.field .help { margin: 0 0 .3rem; font-size: .8rem; color: var(--muted); }
.field input, .field textarea, .field select {
  font: inherit; font-size: .9rem; width: 100%; color: var(--text);
  background: var(--surface); border: 1px solid var(--line);
  border-radius: 4px; padding: .38rem .5rem; }
.field textarea { resize: vertical; line-height: 1.45; }
.field textarea.mono, .field input.mono { font-family: var(--mono);
  font-size: .84rem; }
.field .pair { display: flex; gap: .5rem; flex-wrap: wrap; }
.field .pair > .kindwrap { flex: 0 1 14rem; }
.field .pair > .payloadwrap { flex: 1 1 16rem; min-width: 0; }
.field .sub { margin: .15rem 0 0; font-size: var(--tile-label); color: var(--muted);
  letter-spacing: .06em; text-transform: uppercase; }
.field.needed input, .field.needed textarea {
  border-color: var(--red-line); border-left-width: 3px;
  background: var(--red-bg); }
.field .why { margin: .3rem 0 0; font-size: .8rem; color: var(--red);
  display: flex; gap: .45rem; align-items: baseline; flex-wrap: wrap; }
.field .why .mk { font-family: var(--mono); font-weight: 700; }

.proposal { margin: .35rem 0 0; border: 1px solid var(--line);
  border-left: 3px solid var(--accent); border-radius: 4px;
  background: var(--surface-2); padding: .5rem .65rem; font-size: .82rem; }
.proposal .top { display: flex; align-items: baseline; gap: .5rem;
  flex-wrap: wrap; }
.proposal p { margin: .3rem 0 0; }
.proposal .says { color: var(--muted); white-space: pre-wrap; }
.proposal .links, .proposal .cost { font-family: var(--mono);
  font-size: .78rem; color: var(--muted); }

/* ---- what an agent can be asked ----
   One card per row of the manifest, priced before it is pressed, and under
   them the interview: a box, and the questions already asked with whatever
   came back under each. */
#tool-rows { display: grid; gap: .5rem;
  grid-template-columns: repeat(auto-fit, minmax(17rem, 1fr)); }
#tool-rows li { margin: 0; }
li.tool { border: 1px solid var(--line); border-radius: 6px;
  background: var(--surface-2); padding: .65rem .8rem; min-width: 0;
  overflow-wrap: anywhere; display: flex; flex-direction: column; }
li.tool > .top { display: flex; align-items: baseline; gap: .35rem .7rem;
  flex-wrap: wrap; }
li.tool .name { font-weight: 650; flex: 1 1 9rem; min-width: 0; }
li.tool p { margin: .25rem 0 0; font-size: .8rem; color: var(--muted); }
li.tool .ceiling { font-family: var(--mono); font-size: .76rem; }
li.tool .why { color: var(--amber); }
li.tool .answer:empty { display: none; }

details.asking { border-left-color: var(--accent); }
#ask-reads, #ask-ceiling { margin-top: 0; }
#ask-ceiling { font-family: var(--mono); font-size: .76rem; }
#ask-why { color: var(--amber); }
#ask .field { margin-top: .7rem; }
#ask .actions { margin-bottom: .9rem; }

ul.thread { list-style: none; margin: 0; padding: 0; }
li.turn { border-top: 1px solid var(--line); padding: .75rem 0 0;
  margin: 0 0 .75rem; min-width: 0; overflow-wrap: anywhere; }
li.turn .q { display: flex; gap: .5rem; margin: 0; font-weight: 650;
  font-size: .92rem; }
li.turn .q .mk { font-family: var(--mono); color: var(--accent);
  flex: none; }
li.turn .a { margin: .4rem 0 0 1.15rem; padding-left: .8rem;
  border-left: 2px solid var(--line); }
li.turn .a-top { display: flex; align-items: baseline; gap: .5rem;
  flex-wrap: wrap; }
li.turn .says { margin: .3rem 0 0; white-space: pre-wrap; }
li.turn .says.waiting { color: var(--muted); font-style: italic; }
li.turn .cost, li.turn .links { font-family: var(--mono); font-size: .76rem;
  color: var(--muted); }
li.turn .links { margin: .35rem 0 0; display: flex; flex-wrap: wrap;
  gap: .1rem .55rem; }

.check { display: flex; gap: .6rem; align-items: flex-start;
  border: 1px solid var(--line); border-left: 5px solid var(--green-line);
  border-radius: 6px; background: var(--surface-2); padding: .65rem .8rem; }
.check.off { border-left-color: var(--line); }
.check input { margin: .25rem 0 0; width: 1.05rem; height: 1.05rem;
  flex: none; accent-color: var(--accent); }
.check .txt { min-width: 0; }
.check .txt > label { font-weight: 650; font-size: .92rem; }
.check .count { font-family: var(--mono); font-weight: 700; }
.check .pages { margin: .25rem 0 0; font-family: var(--mono);
  font-size: .78rem; color: var(--muted); overflow-wrap: anywhere; }
.check .effect { margin: .3rem 0 0; font-size: .8rem; color: var(--muted); }

ul.findings { margin: 0; padding: 0; list-style: none; }
.finding { border: 1px solid var(--line); border-left: 5px solid var(--line);
  border-radius: 6px; background: var(--surface); padding: .55rem .75rem;
  margin: 0 0 .45rem; font-size: .86rem; }
.finding.f-error { border-left-color: var(--red-line); }
.finding.f-warning { border-left-color: var(--amber-line); }
.finding.suppressed { border-left-style: dashed; }
.finding .top { display: flex; align-items: baseline; gap: .45rem;
  flex-wrap: wrap; }
.finding .rule { font-family: var(--mono); font-size: .8rem;
  font-weight: 650; }
.finding .where { font-family: var(--mono); font-size: .76rem;
  color: var(--muted); overflow-wrap: anywhere; }
.finding p { margin: .28rem 0 0; }
.finding .hint { color: var(--muted); font-size: .82rem; }
.badge { font-size: var(--tile-label); }
.badge.b-quiet { color: var(--muted); background: var(--surface-2); }

.diff { border: 1px solid var(--line); border-radius: 6px;
  background: var(--surface); overflow-x: auto; min-width: 0;
  max-height: 60vh; overflow-y: auto; }
.diff .filerow { display: flex; align-items: baseline; gap: .5rem;
  flex-wrap: wrap; padding: .42rem .7rem; background: var(--surface-2);
  border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
  font-family: var(--mono); font-size: .8rem; font-weight: 650;
  position: sticky; left: 0; }
.diff .filerow:first-child { border-top: 0; }
.diff .filerow .path { overflow-wrap: anywhere; min-width: 0; }
.diff .filerow .stat { font-size: .75rem; font-weight: 500;
  color: var(--muted); }
.diff .filerow .stat .p { color: var(--green); font-weight: 700; }
.diff .filerow .stat .m { color: var(--red); font-weight: 700; }
.diff pre { margin: 0; font-family: var(--mono); font-size: .775rem;
  line-height: 1.5; min-width: max-content; }
.diff ins, .diff del, .diff .ln { display: block; padding: 0 .7rem 0 0;
  white-space: pre; text-decoration: none; }
.diff .g { display: inline-block; width: 1.35rem; padding-left: .45rem;
  color: var(--muted); user-select: none; }
.diff ins { background: var(--green-bg);
  box-shadow: inset 3px 0 0 var(--green-line); }
.diff ins .g { color: var(--green); font-weight: 700; }
.diff del { background: var(--red-bg);
  box-shadow: inset 3px 0 0 var(--red-line); }
.diff del .g { color: var(--red); font-weight: 700; }
.diff .ln.hunk { background: var(--surface-2); color: var(--muted);
  border-top: 1px dashed var(--line); border-bottom: 1px dashed var(--line); }
.diff.empty { padding: 1rem; color: var(--muted); font-size: .88rem; }

.cli { border: 1px solid var(--line); border-radius: 6px;
  background: var(--surface-2); margin: .8rem 0 0; }
.cli .top { display: flex; align-items: baseline; gap: .5rem;
  flex-wrap: wrap; padding: .4rem .7rem; border-bottom: 1px solid var(--line); }
/* The command wraps rather than scrolling: a scrollable box is a tab stop
   in Chrome, and an unlabelled one announcing `dbwiki incident resolve` is
   worse than a wrapped line the operator can read whole. */
.cli pre { margin: 0; padding: .55rem .7rem .6rem 1.9rem;
  font-family: var(--mono); font-size: .765rem; line-height: 1.5;
  text-indent: -1.2rem; white-space: pre-wrap; overflow-wrap: anywhere; }
.cli pre::before { content: "$ "; color: var(--muted); }

.confirm { border: 1px solid var(--line);
  border-left: 5px solid var(--accent); border-radius: 6px;
  background: var(--surface); padding: .95rem 1.1rem; }
.confirm h3 { margin: 0 0 .5rem; font-size: 1.02rem; }
.confirm .msg { font-family: var(--mono); font-size: .82rem;
  background: var(--surface-2); border: 1px solid var(--line);
  border-radius: 4px; padding: .45rem .6rem; margin: 0 0 .6rem;
  overflow-wrap: anywhere; }
.confirm dl { display: grid; grid-template-columns: max-content 1fr;
  gap: .18rem .9rem; margin: 0 0 .7rem; font-size: .84rem; }
.confirm dt { font-size: var(--tile-label); letter-spacing: .08em;
  text-transform: uppercase; color: var(--muted); font-weight: 600;
  padding-top: .12rem; }
.confirm dd { margin: 0; font-family: var(--mono); font-size: .82rem;
  overflow-wrap: anywhere; }
.confirm .rule-text { margin: 0; font-size: .83rem; color: var(--muted); }

.result { border: 1px solid var(--green-line);
  border-left: 5px solid var(--green-line); border-radius: 6px;
  background: var(--green-bg); padding: .9rem 1.05rem; color: var(--text); }
.result h3 { margin: 0 0 .4rem; font-size: 1.02rem; }
.result .msg { font-family: var(--mono); font-size: .82rem;
  background: var(--surface); border: 1px solid var(--green-line);
  border-radius: 4px; padding: .45rem .6rem; margin: .5rem 0 .1rem;
  overflow-wrap: anywhere; }
.result p { margin: .3rem 0 0; }
.result ul { margin: .4rem 0 0; padding-left: 1.1rem;
  font-family: var(--mono); font-size: .8rem; overflow-wrap: anywhere; }

.prose { min-width: 0; overflow-wrap: anywhere; }
.prose > :first-child { margin-top: 0; }
.prose h1, .prose h2, .prose h3, .prose h4, .prose h5, .prose h6 {
  margin: 1.3rem 0 .5rem; line-height: 1.3; scroll-margin-top: 1rem; }
.prose h1 { font-size: 1.22rem; }
.prose h2 { font-size: 1.06rem; border-bottom: 1px solid var(--line);
  padding-bottom: .22rem; }
.prose h3 { font-size: .97rem; }
.prose h4, .prose h5, .prose h6 { font-size: .9rem; color: var(--muted); }
.prose p { margin: .6rem 0; }
.prose ul, .prose ol { margin: .6rem 0; padding-left: 1.3rem; }
.prose li { margin: .18rem 0; }
.prose blockquote { margin: .8rem 0; padding: .35rem .9rem;
  border-left: 3px solid var(--line); background: var(--surface-2);
  color: var(--muted); }
.prose hr { border: 0; border-top: 1px solid var(--line); margin: 1.2rem 0; }
.prose pre { margin: 0; padding: .6rem .8rem; background: var(--surface-2);
  border: 1px solid var(--line); border-radius: 6px;
  font-family: var(--mono); font-size: .79rem; line-height: 1.5; }
.prose pre code { padding: 0; border: 0; background: none; font-size: inherit; }
.prose code { font-family: var(--mono); font-size: .85em;
  background: var(--surface-2); border: 1px solid var(--line);
  border-radius: 3px; padding: .02rem .25rem; }
.prose code.path { color: var(--muted); border-style: dashed; }
.prose table { border-collapse: collapse; width: 100%; font-size: .86rem; }
.prose th, .prose td { text-align: left; padding: .36rem .7rem;
  border-bottom: 1px solid var(--line); vertical-align: top; }
.prose thead th { font-size: var(--tile-label); letter-spacing: .06em;
  text-transform: uppercase; color: var(--muted); font-weight: 650;
  background: var(--surface-2); }
.prose .scroll { overflow-x: auto; margin: .8rem 0; }
.prose .anchor { float: right; margin-left: .6rem; font-weight: 400;
  text-decoration: none; color: var(--muted); opacity: 0; }
.prose h1:hover .anchor, .prose h2:hover .anchor, .prose h3:hover .anchor,
.prose h4:hover .anchor, .prose h5:hover .anchor, .prose h6:hover .anchor,
.prose .anchor:focus { opacity: 1; }

nav.outline { display: flex; flex-wrap: wrap; gap: .3rem .9rem;
  margin: 0 0 .9rem; padding: .5rem .75rem; border: 1px solid var(--line);
  border-radius: 6px; background: var(--surface-2); font-size: .84rem; }
nav.outline a { text-decoration: none; }
nav.outline a:hover { text-decoration: underline; }
nav.outline a.h3, nav.outline a.h4, nav.outline a.h5, nav.outline a.h6 {
  font-size: .79rem; color: var(--muted); }

p.backlinks { display: flex; flex-wrap: wrap; gap: .3rem .9rem;
  margin: 0; font-family: var(--mono); font-size: .8rem;
  overflow-wrap: anywhere; }

p.deeplink { display: flex; flex-wrap: wrap; align-items: baseline;
  gap: .1rem .55rem; margin: .45rem 0 0; font-size: .85rem;
  color: var(--muted); }
p.deeplink .state { font-size: var(--tile-label); letter-spacing: .05em;
  text-transform: uppercase; border-radius: 3px;
  border: 1px solid var(--line); padding: .02rem .32rem; margin-left: .35rem; }

.timeline { display: flex; flex-wrap: wrap; gap: .5rem; margin: 0 0 1.1rem; }
.stage-box { flex: 1 1 12rem; min-width: 0; overflow-wrap: anywhere;
  border: 1px solid var(--line); border-radius: 6px; background: var(--surface);
  padding: .55rem .7rem; }
.stage-box.agent { border-style: dashed; }
.stage-box > .top { display: flex; align-items: baseline; gap: .35rem .5rem;
  flex-wrap: wrap; }
.stage-box .nm { font-family: var(--mono); font-weight: 650;
  font-size: .84rem; }
.stage-box .kindmark { font-size: var(--tile-label); letter-spacing: .06em;
  text-transform: uppercase; color: var(--muted); border: 1px solid var(--line);
  border-radius: 3px; padding: .02rem .3rem; }
.stage-box.agent .kindmark { border-style: dashed; }
.stage-box p { margin: .3rem 0 0; font-size: .8rem; color: var(--muted); }
.badge .mk { font-family: var(--mono); margin-right: .3rem; }
.inferred { font-size: var(--tile-label); letter-spacing: .06em; text-transform: uppercase;
  color: var(--muted); border: 1px dashed currentColor; border-radius: 3px;
  padding: .02rem .3rem; }

td.mini { font-family: var(--mono); letter-spacing: .14em; }
/* The rows of a folded table are the control the card used to be. */
tr.runrow { cursor: pointer; }
tr.runrow:hover td { background: var(--surface-2); }
#runs-rows td.when { font-family: var(--mono); color: var(--muted); }
#runs-rows .tid { font-family: var(--mono); color: var(--accent);
  text-decoration: none; }
#runs-rows .tid:hover { text-decoration: underline; }
/* An inline-block is what caps a cell a table would otherwise widen to fit. */
td.msg .clip { display: inline-block; max-width: 48ch; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }
#runs-failures td.fp .id { display: block; font-family: var(--mono);
  font-weight: 650; }
#runs-failures td.fp .cmds { display: block; color: var(--muted);
  font-size: .78rem; }
li.dbrow { border: 1px solid var(--line); border-left: 5px solid var(--line);
  border-radius: 6px; background: var(--surface); padding: .6rem .8rem;
  min-width: 0; overflow-wrap: anywhere; }
li.dbrow > .top { display: flex; align-items: baseline; gap: .35rem .7rem;
  flex-wrap: wrap; }
li.dbrow .db { font-family: var(--mono); font-weight: 650; font-size: .85rem; }
li.dbrow .reasons { display: flex; flex-wrap: wrap; gap: .2rem .9rem; }

/* ---- the week ----
   The covering note against who the week touched, on the database screen's
   own grid. A theme is a row and not a card: three cards inside a card is
   three borders saying nothing, and the title is a title in the body face,
   because a theme is a sentence a model wrote and not an identifier. */
#review-synthesis .says { margin: .3rem 0 .7rem; white-space: pre-wrap; }
ul.themes { margin: 0; padding: 0; list-style: none; }
ul.themes > li { border-top: 1px solid var(--line-2);
  padding: .5rem 0 .55rem; }
ul.themes .tt { display: block; font-weight: 650; }
ul.themes .td { margin: .2rem 0 .4rem; color: var(--muted);
  font-size: .85rem; }
/* A citation is a chip with a gap beside it, because eight addresses set end
   to end read as one address. */
p.chips { margin: 0; display: flex; flex-wrap: wrap; gap: .3rem .4rem; }
.refchip { font-family: var(--mono); font-size: .76rem; color: var(--accent);
  text-decoration: none; padding: .05rem .4rem;
  border: 1px solid var(--line); border-radius: 4px; }
.refchip:hover { border-color: var(--accent); }
#review-synthesis .foot { margin: .7rem 0 0; padding-top: .6rem;
  border-top: 1px solid var(--line-2); display: flex; gap: .7rem;
  align-items: center; flex-wrap: wrap; font-size: var(--tile-label);
  color: var(--muted); }
#review-cited { margin-top: .55rem; }
#review-dbs { margin-bottom: .5rem; }
/* The fleet is a name and not an address: there is no screen for it. */
#review-dbs span.dbchip { color: var(--muted); }
#review-kinds { margin: 0; }
#review-deliveries table.fleet { table-layout: fixed; }
/* Wide enough for the longest status word plus its mark, so a badge never
   spills over the recipient beside it. */
#review-deliveries th.w-st { width: 8rem; }
#review-deliveries th.w-when { width: 7rem; }
#review-deliveries td.st .badge { white-space: nowrap; }
#review-deliveries td.to .nm { display: block; }
#review-deliveries td.to .why { display: block; color: var(--amber);
  font-size: 12px; }
#review-deliveries td.when { color: var(--muted); white-space: nowrap; }

/* Twelve findings were twelve cards saying the same five things, three
   thousand pixels of them. They are a table on fixed columns, where the one
   column with no natural length is the title. The two badges keep columns
   wide enough for their longest word so neither wraps to two lines, and the
   actions keep one wide enough for three controls on one line: a select that
   dropped under its buttons would put the rows on two different rhythms.
   The stripe down the left is the band said again, and a row somebody has
   already answered for keeps the stripe and dashes it. */
#review-findings table.fleet { table-layout: fixed; }
#review-findings th.w-band { width: 6rem; }
#review-findings th.w-moved { width: 7rem; }
#review-findings th.w-db { width: 8rem; }
#review-findings th.w-said { width: 9.5rem; }
#review-findings th.w-acts { width: 20rem; }
#review-findings td .badge { white-space: nowrap; }
#review-findings td.db { font-family: var(--mono); font-size: .85rem;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#review-findings td.what .nm { display: block; font-weight: 650;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#review-findings td.what .why { display: block; font-size: 12px;
  color: var(--muted); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
#review-findings td.said { font-size: 12px; color: var(--muted); }
#review-findings .ctl { display: flex; gap: .35rem; align-items: center;
  flex-wrap: nowrap; }
#review-findings .ctl select { font: inherit; font-size: .78rem;
  line-height: 1.2; color: var(--text); background: var(--surface);
  border: 1px solid var(--line); border-radius: 4px; padding: .22rem .3rem; }
#review-findings tbody tr:not(.day) td:first-child {
  border-left: 3px solid var(--line-2); }
#review-findings tr.b-high td:first-child {
  border-left-color: var(--red-line); }
#review-findings tr.done { color: var(--muted); }
#review-findings tr.done td:first-child { border-left-style: dashed; }
#review-findings tr.done td.what .nm { font-weight: 500; }

/* The measure beside a number, never instead of one: the cell's text carries
   the value and this only gives thirty rows a shape at a glance. */
td .bar { display: block; height: .4em; margin-top: .22em; border-radius: 2px;
  background: currentColor; opacity: .3; }
table.fleet thead th.id { text-transform: none; letter-spacing: 0;
  font-family: var(--mono); }

/* The fleet is laid out on its fixed columns: five of the six hold a name, a
   count or a stamp of known length, and the journal headline takes what they
   leave rather than dragging the table sideways. Fixing them also reserves
   the two cells the screen fills in after the table is already up, so nothing
   under them moves when those answers land. */
#fleet-table table.fleet { table-layout: fixed; }
#fleet-table th.db { width: 8.5rem; }
#fleet-table th.open { width: 5.5rem; }
#fleet-table th.watch { width: 7.5rem; }
#fleet-table th.spark { width: 9.5rem; }
#fleet-table th.tick { width: 13rem; }
#fleet-table td.journal { white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
#fleet-table .tid { font-family: var(--mono); font-weight: 650;
  color: var(--accent); text-decoration: none; }
#fleet-table .tid:hover { text-decoration: underline; }
.spark-svg { display: block; margin: .25em 0 0 auto; }

/* ---- one database ----
   The screen opens with five numbers, and under them what the wiki says on
   the left against how loud the month was on the right. Seven twelfths to
   five: the left card holds prose at a readable measure, the right holds a
   month of thirty cells and a short list, and neither is a column the other
   has to share. One column under 980, where five twelfths is narrower than
   the calendar it would have to draw. */
.dbhost { margin: -.2rem 0 .8rem; color: var(--muted); font-size: .9rem;
  font-family: var(--mono); }
.twocol { display: grid; gap: 1rem; align-items: start; margin: 0 0 1.4rem;
  grid-template-columns: minmax(0, 7fr) minmax(0, 5fr); }
.twoside { display: grid; gap: 1rem; align-items: start; min-width: 0; }
@media (max-width: 980px) { .twocol { grid-template-columns: 1fr; } }
.twocol .card { min-width: 0; padding: .8rem 1rem; }
.twocol .card > .cap { display: block; margin: 0 0 .5rem; }

/* What the wiki says. The lead is open and the sections are disclosures at
   the card's own type, so the heading row reads as a heading whether it is
   open or shut and the count on the right says what asking for it costs. */
#db-about .lead { margin: 0 0 .2rem; }
#db-about .sect { border-top: 1px solid var(--line-2); }
#db-about .sect h3 { margin: 0; font-size: inherit; font-weight: inherit; }
#db-about .sectx { font: inherit; font-size: .95rem; font-weight: 650;
  cursor: pointer; border: 0; background: none; color: var(--text);
  padding: .5rem 0; width: 100%; text-align: left; display: flex;
  gap: .5rem; align-items: baseline; }
#db-about .sectx:hover { color: var(--accent); }
#db-about .sectx .tw { font-family: var(--mono); color: var(--muted);
  width: 1em; flex: none; }
#db-about .sectx .nm { min-width: 0; }
#db-about .sectx .n { margin-left: auto; flex: none; color: var(--muted);
  font-weight: 400; font-size: var(--tile-label); }
#db-about .sect .prose { padding: 0 0 .6rem 1.5em; }
#db-about .prose code { font-size: .8rem; }
#db-about .prose ul { padding-left: 1.1rem; }
#db-about .foot { margin: .7rem 0 0; padding-top: .6rem;
  border-top: 1px solid var(--line-2); display: flex; gap: .6rem;
  align-items: center; flex-wrap: wrap; font-size: var(--tile-label);
  color: var(--muted); }
#db-about .foot .path { font-family: var(--mono); }
#db-about .foot .sha.clip { display: inline-block; max-width: 60ch;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  vertical-align: bottom; }

/* The month, for one database. The gutter and the day labels are HTML at
   twelve pixels flat; only the cells are drawn, and they are the one thing
   allowed to stretch with the column. The box keeps its height before the
   counts land, so nothing under it moves when they do. */
.hbands { display: grid; align-items: center; gap: 2px .6rem;
  grid-template-columns: max-content minmax(0, 1fr); min-height: 66px; }
.hbands .hname { font-size: 12px; font-family: var(--mono);
  color: var(--muted); }
.hband-svg { display: block; width: 100%; }
.hbands .hdays { grid-column: 2; position: relative; height: 1.15rem;
  margin-top: 2px; font-size: 12px; font-family: var(--mono);
  color: var(--muted); }
.hbands .hdays span { position: absolute; top: 0; white-space: nowrap;
  transform: translateX(-50%); }
.hbands .hdays .now { right: 0; left: auto; transform: none; }
.mnote { margin: .5rem 0 0; font-size: var(--tile-label);
  color: var(--muted); }
.linked { margin: 0; display: flex; flex-wrap: wrap; gap: .4rem;
  align-items: baseline; font-size: var(--tile-label); color: var(--muted); }
.linked .dbchip { font-family: var(--mono); font-size: .8rem;
  color: var(--accent); text-decoration: none; padding: .05rem .4rem;
  border: 1px solid var(--line); border-radius: 4px; }
.linked .dbchip:hover { border-color: var(--accent); }

/* The three tables. Fixed columns, because the one column with no natural
   length — the incident's title, the journal's headline — is the one that
   would otherwise drag the table sideways. The status keeps a column wide
   enough for the longest label it can carry, so a badge never wraps to two
   lines; the stripe down the left of a row is the same status said again in
   the queue's own three tones. */
#db-rows table.fleet, #db-journal table.fleet,
#db-errors table.fleet { table-layout: fixed; }
#db-rows, #db-errors, #db-journal { margin: 0 0 1.6rem; }
#db th.w-day { width: 5.75rem; }
#db th.w-st { width: 9.5rem; }
#db th.w-code { width: 7.5rem; }
#db th.w-num { width: 6.5rem; }
#db th.w-bar { width: 8rem; }
#db td.day { color: var(--muted); }
#db td.what { white-space: normal; }
#db td.st .badge { white-space: nowrap; }
#db td.clip a { display: block; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
#db td.cbar i { display: block; height: 6px; border-radius: 3px;
  background: var(--red-line); opacity: .7; }
#db tr.s-open td:first-child { box-shadow: inset 3px 0 0 var(--red-line); }
#db tr.s-monitoring td:first-child {
  box-shadow: inset 3px 0 0 var(--amber-line); }
#db tr.s-resolved td:first-child {
  box-shadow: inset 3px 0 0 var(--green-line); }

/* ---- every week held ----
   Fixed columns, because the thing being asked is whether a week is
   heavier than the one under it, and that
   is a question about a column: a table that sized each column to its own
   longest cell would move the answer sideways between the newest week and
   the oldest. The note keeps a column wide enough for the longest thing it
   can say, which is a failure that names its category, so the badge never
   wraps to a second line and no row is taller than its neighbours. */
#inbox-rows table.fleet { table-layout: fixed; }
#inbox-rows { margin: 0 0 1.6rem; }
#inbox th.w-week { width: 6.25rem; }
#inbox th.w-gen { width: 8rem; }
#inbox th.w-look { width: 5.75rem; }
#inbox th.w-bar { width: 5.5rem; }
#inbox th.w-num { width: 6.25rem; }
#inbox th.w-note { width: 14rem; }
#inbox td.week a { font-weight: 650; }
#inbox td.day { color: var(--muted); }
#inbox td.sha { font-family: var(--mono); color: var(--muted); }
#inbox td.note .badge { white-space: nowrap; }
/* The high band first and the rest of the selection after it, so the length
   is the whole week and the red is the part of it worth opening the week
   for. Both are drawn even when one is nothing: a cell whose parts appear
   and disappear reads as two different charts down the column. */
#inbox td.cbar i { display: inline-block; vertical-align: middle;
  height: 6px; background: var(--line-2); }
#inbox td.cbar i:first-child { background: var(--red-line); opacity: .7;
  border-radius: 3px 0 0 3px; }
#inbox td.cbar i + i { border-radius: 0 3px 3px 0; }
/* The stripe is the same reading as the red on the bar, kept where the eye
   picks one row out of the list rather than where it measures that row. */
#inbox tr.w-hot td:first-child { box-shadow: inset 3px 0 0 var(--red-line); }
#inbox tr.w-calm td:first-child { box-shadow: inset 3px 0 0 var(--line-2); }

/* ---- the link board ----
   Rows, not cards: the board is read down the page looking for one name, and
   a grid of boxes would make the eye scan two directions for it. The url is
   shown in full and in the mono face, because a Kibana dashboard id is the
   part of the row an operator copies, and a shortened one cannot be copied.
   The three tags mark where a link goes, so each takes a colour the page
   already spends on a kind of certainty: tailscale is the amber one, since
   "only from the tailnet" is the tag that predicts a link failing. */
.lintro { margin: 0 0 1.2rem; max-width: 62ch; }
.lnote { margin: 0 0 .6rem; font-size: .85rem; color: var(--muted);
  max-width: 62ch; }
.lintro code, .lnote code, .lwhat code { font-family: var(--mono);
  font-size: .88em; background: var(--surface-2);
  border: 1px solid var(--line); border-radius: 3px; padding: 0 .25rem; }
#links-board ul.rows { margin: 0 0 1.4rem; }
li.lrow { border: 1px solid var(--line); border-left: 5px solid var(--line);
  border-radius: 6px; background: var(--surface); padding: .6rem .8rem;
  min-width: 0; overflow-wrap: anywhere; }
li.lrow > .top { display: flex; align-items: baseline; gap: .35rem .7rem;
  flex-wrap: wrap; }
li.lrow .lname { font-weight: 650; min-width: 0; }
li.lrow .lwhat { margin: .3rem 0 0; font-size: .85rem; color: var(--muted);
  max-width: 68ch; }
li.lrow .lurl { display: inline-block; margin-top: .35rem;
  font-family: var(--mono); font-size: .78rem; overflow-wrap: anywhere; }
.ltag { font-size: var(--tile-label); letter-spacing: .07em; text-transform: uppercase;
  font-weight: 700; border-radius: 3px; padding: .1rem .42rem;
  border: 1px solid currentColor; }
.ltag.t-tailscale { color: var(--amber); background: var(--amber-bg); }
.ltag.t-github { color: var(--accent); background: var(--surface-2); }
.ltag.t-artifact { color: var(--green); background: var(--green-bg); }

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
"""

#: The JS calls every endpoint through one function, `api("METHOD",
#: "/api/...")`, with the path as a string literal that may contain `{slug}`.
_API_CALL_RE = re.compile(r"""api\(\s*["'](GET|POST)["']\s*,\s*["']([^"']+)["']""")

_PLACEHOLDER = "/*@CSS@*/"

#: The one number the page and the server both have to agree on. The page
#: does the day arithmetic, because the span is measured against the reader's
#: own today, but the threshold that span is compared against is
#: `readmodel.QUIET_DAYS` and is substituted in here. A second literal 14 in
#: the script would be a number two files could move apart, and the report
#: and the board would then disagree about which cases are quiet.
_QUIET_PLACEHOLDER = "/*@QUIET_DAYS@*/"

#: An inline `<script>` and its body: one with no `src=`, which is the only
#: kind a browser refuses under a policy that permits neither inline nor its
#: hash.
_INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                               re.S)

#: Route names no `api(...)` call may name, so the dead-route rule does not
#: fire on them: `page` is the document itself, and `health` is `dbwiki
#: health`'s probe.
NOT_CALLED = frozenset({"page", "health"})

#: A slug shaped like the ones `incidents.Incident.slug` produces, used to
#: turn a `{slug}` path template back into a concrete path a route pattern
#: can be matched against.
_SAMPLE_SLUG = "2026-08-11-cdb1-oracle-internal-errors"

#: HTML elements that never close. `html.parser` reports them through
#: `handle_startendtag` only when the author wrote `<br/>`.
_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

_EXTERNAL_URL_RE = re.compile(r"\A\s*(?:[a-z][a-z0-9+.-]*:)?//", re.I)
_CSS_EXTERNAL_RE = re.compile(r"@import|url\(\s*['\"]?(?:[a-z]+:)?//", re.I)


def page() -> str:
    """The served page: `workbench.html` with the CSS and the quiet threshold
    substituted. Deterministic; two calls return identical bytes. Raises
    RuntimeError when either placeholder is missing, because a page served
    without the design language, or with a threshold that no longer matches
    the one the health report prints, is a bug rather than a style choice."""
    template = (resources.files(__package__) / "workbench.html").read_text("utf-8")
    for marker in (_PLACEHOLDER, _QUIET_PLACEHOLDER):
        if template.count(marker) != 1:
            raise RuntimeError(
                f"workbench.html must hold exactly one {marker} marker, "
                f"found {template.count(marker)}")
    return (template
            .replace(_PLACEHOLDER, daily_html.CSS + WORKBENCH_CSS)
            .replace(_QUIET_PLACEHOLDER, str(readmodel.QUIET_DAYS)))


@functools.cache
def script_src() -> str:
    """The `script-src` source list for the page as served: a `'sha256-'`
    of the one inline block `page()` carries.

    That the page carries exactly one script block is what makes a hashed
    policy possible at all, and `check_page`'s no-inline-handler rule is what
    keeps it true: a handler in the markup is a script the hash cannot name.
    Not exactly one block raises, for the reason `page()` raises on a missing
    placeholder: it is a bug rather than a style choice.

    The body is hashed as a browser hashes it, the bytes between the opening
    tag's `>` and `</script>` with nothing stripped. Memoised because `page()`
    is deterministic, so no response re-reads and re-hashes the page."""
    blocks = _INLINE_SCRIPT_RE.findall(page())
    if len(blocks) != 1:
        raise RuntimeError(
            f"the page must carry exactly one inline <script> block for the "
            f"CSP to name, found {len(blocks)}")
    digest = hashlib.sha256(blocks[0].encode()).digest()
    return f"'sha256-{base64.b64encode(digest).decode()}'"


def endpoints_called(html: str) -> frozenset[tuple[str, str]]:
    """Every `(method, path template)` the page's JS calls."""
    return frozenset(_API_CALL_RE.findall(html))


class _Scan(HTMLParser):
    """One pass over the page: tag balance, external loads, inline handlers.

    The stack is a heuristic and says so in `check_page`'s docstring; it
    catches the mistake that actually happens (a `</div>` dropped while
    editing a screen) and does not pretend to be a validator."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self._attrs(tag, attrs)
        if tag not in _VOID:
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._attrs(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if not self.stack:
            self.problems.append(f"line {self.getpos()[0]}: </{tag}> closes nothing")
            return
        open_tag, line = self.stack.pop()
        if open_tag != tag:
            self.problems.append(
                f"line {self.getpos()[0]}: </{tag}> closes <{open_tag}> "
                f"opened on line {line}")

    def handle_data(self, data: str) -> None:
        inside_style = bool(self.stack) and self.stack[-1][0] == "style"
        if inside_style and _CSS_EXTERNAL_RE.search(data):
            self.problems.append(
                f"line {self.getpos()[0]}: the stylesheet loads something "
                f"external")

    def _attrs(self, tag: str, attrs) -> None:
        line = self.getpos()[0]
        for name, value in attrs:
            if name.startswith("on"):
                self.problems.append(
                    f"line {line}: <{tag} {name}=> is an inline handler")
            if name in ("src", "href", "srcset", "data", "action") and value:
                if _EXTERNAL_URL_RE.match(value):
                    self.problems.append(
                        f"line {line}: <{tag} {name}=\"{value}\"> loads "
                        f"something external")
            if name == "style" and value and _CSS_EXTERNAL_RE.search(value):
                self.problems.append(
                    f"line {line}: <{tag} style=> loads something external")


def _routes_or_none(routes) -> Sequence | None:
    """`routes` as given, or `server.ROUTES` when the import works. The
    import is lazy and its failure is not a problem to report: the page is
    checkable before the API lands, and a caller with no routes gets the
    other three checks rather than an ImportError."""
    if routes is not None:
        return routes
    try:
        from .server import ROUTES
    except ImportError:
        return None
    return ROUTES


def _route_problems(called: Iterable[tuple[str, str]], routes: Sequence) -> list[str]:
    problems, hit = [], set()
    for method, path in sorted(called):
        concrete = path.replace("{slug}", _SAMPLE_SLUG)
        matched = [r for r in routes
                   if r.method == method and r.pattern.match(concrete)]
        if not matched:
            problems.append(f'api("{method}", "{path}") names no route')
        hit.update(r.name for r in matched)
    for route in routes:
        if route.name not in hit and route.name not in NOT_CALLED:
            problems.append(f"route {route.name} is dead: the page never calls it")
    return problems


def check_page(html: str, routes=None) -> list[str]:
    """Static problems, empty when the page is sound:

    - a tag opened and not closed (a small stack over `html.parser`, void
      elements excepted);
    - a `<script src=`, `<link href=`, `<img src=http`, `@import` or
      `url(http` anywhere: the page is self-contained, like the daily page;
    - an `on*=` attribute, since handlers are attached in the script so the
      page stays at the one inline block `script_src` hashes, and the CSP
      never has to permit inline script to run it;
    - an endpoint from `endpoints_called` that no `routes` row matches with
      `{slug}` replaced by a valid slug;
    - a route in `routes`, bar `NOT_CALLED`, that the page never calls: dead
      API surface.

    `routes` defaults to `server.ROUTES`, imported lazily; when that import
    fails the last two checks are skipped rather than raising, so the page is
    checkable on its own.

    The tag-balance check is a heuristic, not a validator. A browser run is
    the only proof the page works, and it is not in CI."""
    scan = _Scan()
    scan.feed(html)
    scan.close()
    problems = list(scan.problems)
    problems += [f"<{tag}> opened on line {line} is never closed"
                 for tag, line in scan.stack]
    known = _routes_or_none(routes)
    if known is not None:
        problems += _route_problems(endpoints_called(html), known)
    return problems
