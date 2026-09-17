"""Hand-laid diagrams for the docs site.

Each figure is a function returning HTML — inline SVG, or plain markup where a
grid of cards says it better. They are styled by the site's CSS classes, so
they follow the theme instead of carrying baked-in colours:

    b / b.accent / b.ok / b.bad   box fills
    t  title text     s  small mono text     n  number badge text
    l  connector line (arrowheads via the shared #fig-arw marker)

The markdown stays canonical: a figure is opted into with an HTML comment,

    <!-- figure: name | optional caption -->

which build_site.py replaces the following fenced block or table with (and
which GitHub ignores, leaving the ASCII or table fallback in place). A marker
followed by anything else just inserts the figure.
"""

from __future__ import annotations

import html

# shared arrowhead, emitted once per page
DEFS = ('<svg class="figdefs" width="0" height="0" aria-hidden="true">'
        '<defs><marker id="fig-arw" viewBox="0 0 10 10" refX="9" refY="5"'
        ' markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0 0 L10 5 L0 10 z"/></marker></defs></svg>')


def _n(v: float) -> str:
    return f"{v:g}"


def _esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def _t(x: float, y: float, s: str, cls: str = "t", anchor: str = "middle") -> str:
    return (f'<text x="{_n(x)}" y="{_n(y)}" class="{cls}" '
            f'text-anchor="{anchor}">{_esc(s)}</text>')


def _box(x: float, y: float, w: float, h: float, title: str = "",
         sub: str = "", cls: str = "b", rx: float = 7) -> str:
    out = [f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" '
           f'rx="{_n(rx)}" class="{cls}"/>']
    cx, cy = x + w / 2, y + h / 2
    if title and sub:
        out += [_t(cx, cy - 3, title), _t(cx, cy + 14, sub, "s")]
    elif title:
        out.append(_t(cx, cy + 4.5, title))
    return "".join(out)


def _line(x1: float, y1: float, x2: float, y2: float, cls: str = "l",
          head: bool = True) -> str:
    m = ' marker-end="url(#fig-arw)"' if head else ""
    return (f'<line x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}" '
            f'class="{cls}"{m}/>')


def _path(d: str, cls: str = "l", head: bool = True) -> str:
    m = ' marker-end="url(#fig-arw)"' if head else ""
    return f'<path d="{d}" class="{cls}"{m}/>'


def _badge(cx: float, cy: float, text: str, r: float, cls: str) -> str:
    return (f'<circle cx="{_n(cx)}" cy="{_n(cy)}" r="{_n(r)}" class="{cls}"/>'
            + _t(cx, cy + 4, text, "n"))


def _svg(w: float, h: float, body: str, min_px: float) -> str:
    """Wide figures scroll on a narrow screen instead of shrinking to nothing."""
    return (f'<div class="scroll"><svg viewBox="0 0 {_n(w)} {_n(h)}" '
            f'role="img" style="min-width:{_n(min_px)}px">{body}</svg></div>')


# ---- the figures ------------------------------------------------------------

def pipeline() -> str:
    """Logs in, a page you read out — with the skip branch that makes it cheap."""
    W, X, BW, BH, STEP = 880, 180, 300, 54, 82
    rows = [
        ("Elasticsearch", "~1M raw docs a day", "b"),
        ("Compactor", "python, no model, no cost", "b"),
        ("Digest", "~30 lines for a boring day", "b"),
        ("Trigger", "pure function: wake or skip", "b"),
        ("Agent", "cheap or strong tier", "b accent"),
        ("Rails", "validate · lint · commit or roll back", "b"),
        ("Wiki (git)", "journals · incidents · errors · reports", "b"),
        ("html/index.html", "the page you actually read", "b ok"),
    ]
    out = []
    for i, (title, sub, cls) in enumerate(rows):
        y = 16 + i * STEP
        out.append(_box(X, y, BW, BH, title, sub, cls))
        if i:
            out.append(_line(X + BW / 2, y - STEP + BH, X + BW / 2, y - 5))
    ty = 16 + 3 * STEP                      # the trigger row
    out += [
        _line(X + BW, ty + BH / 2, 526, ty + BH / 2),
        _box(530, ty + 3, 300, 48, "most windows stop here",
             "nothing written, nothing spent", "b muted"),
        _t(500, ty + BH / 2 - 6, "skip", "s"),
        _t(X + BW / 2 + 8, 16 + 4 * STEP - 8, "wake — notable", "s", "start"),
    ]
    # who is in charge of which stretch
    m_end = 16 + 3 * STEP + BH
    a_top, a_end = 16 + 4 * STEP, 16 + 4 * STEP + BH
    out += [
        _path(f"M170 20 H162 V{_n(m_end - 4)} H170", "br", False),
        _t(150, (20 + m_end) / 2 - 4, "deterministic", "s mono-strong", "end"),
        _t(150, (20 + m_end) / 2 + 12, "what happened", "s", "end"),
        _path(f"M170 {_n(a_top)} H162 V{_n(a_end)} H170", "br accent", False),
        _t(150, (a_top + a_end) / 2 - 4, "the model", "s mono-strong accent", "end"),
        _t(150, (a_top + a_end) / 2 + 12, "what it means", "s accent", "end"),
    ]
    return _svg(W, 16 + 8 * STEP - 28 + BH, "".join(out), 620)


def layers() -> str:
    """The five layers, top-down."""
    W, H, GAP = 880, 62, 9
    rows = [
        ("4", "Consumers", "daily HTML page · fleet reports · Q&A · lint agent · Kibana / Langfuse"),
        ("3", "Wiki (git)", "databases/ journals/ incidents/ errors/ reports/ sources/ index.md log.md"),
        ("2", "Agents", "one harness contract · adapters codex | claude | ollama | pi · agentic or structured"),
        ("1", "Compactor", "ES query → classify → group → deltas → digest + md twin, watermark, notable verdict"),
        ("0", "Raw sources", "Elasticsearch — oracle alert / listener / dataguard, immutable truth"),
    ]
    out = []
    for i, (num, name, desc) in enumerate(rows):
        y = i * (H + GAP)
        cls = "b accent" if name == "Agents" else "b"
        out += [_box(0, y, W, H, cls=cls),
                _badge(34, y + H / 2, num, 14, "b n-badge"),
                _t(66, y + H / 2 + 4.5, name, "t", "start"),
                _t(232, y + H / 2 + 4, desc, "s", "start")]
    return _svg(W, 5 * (H + GAP) - GAP, "".join(out), 620)


def trigger() -> str:
    """The six checks, first match wins."""
    W, H, GAP, X2 = 880, 46, 10, 566
    rows = [
        ("1", "this exact window end was already ingested", "skip", "already_ingested", ""),
        ("2", "window moved, content hash identical", "skip", "content_unchanged", ""),
        ("3", "the digest is notable — a delta, or a notable class", "wake", "one reason per delta", "accent"),
        ("4", "routine window, on a --consolidate tick", "force", "force_consolidation", "accent"),
        ("5", "routine window, invoked by hand", "wake", "manual", "accent"),
        ("6", "otherwise", "skip", "routine_only", ""),
    ]
    out = []
    for i, (num, cond, verdict, reason, tone) in enumerate(rows):
        y = i * (H + GAP)
        out += [_box(0, y, W, H, cls="b"),
                _badge(30, y + H / 2, num, 13, "b n-badge"),
                _t(60, y + H / 2 + 4.5, cond, "t", "start"),
                _box(X2, y + 8, 90, H - 16, verdict,
                     cls=f"b pillbox {tone}".strip(), rx=14),
                _t(X2 + 106, y + H / 2 + 4, reason, "s", "start")]
    y = 6 * (H + GAP)
    out += [_t(0, y + 22, "tier", "s", "start"),
            _t(60, y + 22, "strong when the digest carries a first_ever_code or "
                            "silence delta, or any error-class notable group — "
                            "cheap otherwise", "s", "start")]
    return _svg(W, y + 34, "".join(out), 620)


def modes() -> str:
    """Agentic vs structured, side by side."""
    W, CW, H = 880, 410, 56
    def column(x: float, name: str, steps: list[tuple[str, str]],
               foot: str) -> str:
        out = [_t(x, 14, name, "t", "start"),
               _t(x + 84, 14, foot, "s", "start")]
        for i, (title, sub) in enumerate(steps):
            y = 34 + i * (H + 24)
            cls = "b accent" if i == 1 else "b"
            out.append(_box(x, y, CW, H, title, sub, cls))
            if i:
                out.append(_line(x + CW / 2, y - 24, x + CW / 2, y - 5))
        return "".join(out)

    left = column(0, "agentic", [
        ("the wiki checkout + AGENTS.md + tools", "the model can read and write files"),
        ("the model edits pages itself", "and writes .agent-result.json"),
        ("rails validate what it did", "contract, provenance lint, commit or roll back"),
    ], "notable reports · lint")
    right = column(470, "structured", [
        ("one text prompt, no tools, no files", "pi --no-tools --no-context-files"),
        ("the model returns one JSON proposal", "summary · journal · notes · incident action"),
        ("structured.py writes every file", "idempotent and lint-clean by construction"),
    ], "ingest · routine reports")
    return _svg(W, 34 + 3 * (H + 24) - 24 + 10, left + right, 640)


def rails() -> str:
    """The fence every agent stage runs inside."""
    W, H, GAP = 880, 40, 8
    steps = [
        ("1", "single-flight lock", "flock on .state/orchestrator.lock — default wait 0, fail fast"),
        ("2", "clean-tree check", "uncommitted work outside digests/ and html/ aborts (dirty_tree)"),
        ("3", "digests committed first", "the agent's own edits are then the only non-machine change"),
        ("4", "result validation", "task, keys, pages exist, digests/ untouched, log.md updated"),
        ("5", "stage-specific rails", "research: allowed paths, no new source page, approved domains only"),
        ("6", "provenance lint", "over changed paths — error-severity findings block the commit"),
    ]
    out = []
    for i, (num, name, sub) in enumerate(steps):
        y = i * (H + GAP)
        out += [_box(0, y, W, H, cls="b"),
                _badge(28, y + H / 2, num, 12, "b n-badge"),
                _t(56, y + H / 2 + 4, name, "t", "start"),
                _t(232, y + H / 2 + 4, sub, "s", "start")]
    y = 6 * (H + GAP)
    out += [_box(0, y, W, H, cls="b"),
            _badge(28, y + H / 2, "7", 12, "b n-badge"),
            _t(56, y + H / 2 + 4, "commit or roll back, as a unit", "t", "start"),
            _line(230, y + H, 230, y + H + 26),
            _line(650, y + H, 650, y + H + 26),
            _box(20, y + H + 30, 420, 52, "commit",
                 "one commit, Run-ID trailer, push when report.push", "b ok"),
            _box(460, y + H + 30, 420, 52, "rollback",
                 "checkout -- . + clean -fd, machine dirs excluded", "b bad"),
            _t(0, y + H + 108, "8", "s", "start"),
            _t(56, y + H + 108, "ledger + telemetry either way — content hash, "
               "failure category, run-health event, agent_runs line", "s", "start")]
    return _svg(W, y + H + 122, "".join(out), 640)


def tick() -> str:
    """One `dbwiki run`, in order."""
    W, BW, BH = 880, 196, 56
    top = [("1", "fold analyst results", "no-op when unused"),
           ("2", "discover + compact", "every db, day so far"),
           ("3", "decide per db", "wake · skip · force"),
           ("4", "ingest woken dbs", "through the rails")]
    bot = [("5", "report", "if notable · or consolidation"),
           ("6", "render the daily HTML", "every tick, even all-skip"),
           ("7", "evaluate alerts", "against the tick's end state")]
    out = []
    for i, (num, name, sub) in enumerate(top):
        x = i * (BW + 32)
        out += [_box(x, 20, BW, BH, name, sub), _badge(x + 12, 20, num, 11, "b n-badge")]
        if i:
            out.append(_line(x - 30, 48, x - 5, 48))
    out.append(_path(f"M{_n(3 * (BW + 32) + BW / 2)} 76 V116 H{_n(BW / 2)} V151", "l"))
    for i, (num, name, sub) in enumerate(bot):
        x = i * (BW + 32)
        out += [_box(x, 156, BW, BH, name, sub),
                _badge(x + 12, 156, num, 11, "b n-badge")]
        if i:
            out.append(_line(x - 30, 184, x - 5, 184))
    out += [_box(3 * (BW + 32), 156, BW, BH, "exit 1 on any failure",
                 "cron sees partial failures", "b muted"),
            _line(2 * (BW + 32) + BW + 2, 184, 3 * (BW + 32) - 5, 184),
            _t(0, 244, "steps 6 and 7 run even when every database skipped — "
               "today's operator page is never stale", "s", "start")]
    return _svg(W, 256, "".join(out), 700)


def drilldown() -> str:
    """Reading the wiki: top-down, each step narrower and closer to the evidence."""
    W, BW, BH, DX, DY = 880, 470, 56, 78, 68
    rows = [
        ("wiki/html/index.html", "what needs attention today"),
        ("reports/<day>.md", "what happened across the fleet, with history"),
        ("databases/<db>/journal/<YYYY-MM>.md", "the day-by-day record for one database"),
        ("incidents/<date>-<db>-<slug>.md", "timeline, evidence, diagnosis, status"),
        ("errors/<CODE>.md", "have we seen this before, and what fixed it"),
        ("digests/<db>/<date>.md", "the evidence every claim above cites"),
    ]
    out = []
    for i, (path, what) in enumerate(rows):
        x, y = i * DX, i * DY
        cls = "b ok" if i == 0 else ("b accent" if i == len(rows) - 1 else "b")
        out += [_box(x, y, BW, BH, cls=cls),
                _t(x + 16, y + 24, path, "s mono-strong", "start"),
                _t(x + 16, y + 42, what, "s", "start")]
        if i:
            out.append(_path(f"M{_n(x - DX + 24)} {_n(y - DY + BH)} "
                             f"V{_n(y + 20)} H{_n(x - 5)}"))
    y = len(rows) * DY
    out += [_path(f"M{_n(5 * DX + 24)} {_n(y - DY + BH)} V{_n(y + 14)} "
                  f"H{_n(5 * DX + 40)}"),
            _t(5 * DX + 50, y + 18, "dbwiki es get --index … --id …  →  the raw "
               "document in Elasticsearch", "s", "start")]
    return _svg(W, y + 30, "".join(out), 640)


def triage() -> str:
    """What `dbwiki health` told you, and what to do about it."""
    W = 880
    out = [_box(320, 0, 240, 46, "dbwiki health", cls="b accent"),
           _line(440, 46, 440, 62, head=False),
           _path("M120 62 H760", "l", False),
           _line(120, 62, 120, 78), _line(440, 62, 440, 78),
           _line(760, 62, 760, 78),
           _box(0, 82, 240, 52, "exit 0", "nothing else needs looking at", "b ok"),
           _box(320, 82, 240, 52, "exit 1", "a failure or a blocker", "b warn"),
           _box(640, 82, 240, 52, "exit 2", "state unusable — read the blockers", "b bad")]
    rows = [
        ("dependencies", "model server down / small ctx / adapter off PATH",
         "unsloth start · fix the crontab PATH"),
        ("failed ingests", "harness_error, agent_timeout, no_result, validation_failed",
         "fix the cause, then dbwiki retry"),
        ("collection", "collection_failure — nothing recent from any source",
         "restart the shipper upstream; the pipeline cannot"),
        ("last success", "stage_stale — a stage that used to run, stopped",
         "read .state/cron.log, run the stage by hand"),
    ]
    y0, RH, GAP, SPINE = 158, 52, 10, 56
    last = y0 + (len(rows) - 1) * (RH + GAP) + RH / 2
    out.append(_path(f"M440 134 V146 H{_n(SPINE)} V{_n(last)}", "l", False))
    for i, (block, symptom, fix) in enumerate(rows):
        y = y0 + i * (RH + GAP)
        out += [_box(110, y, 770, RH, cls="b"),
                _t(126, y + 21, block, "s mono-strong", "start"),
                _t(126, y + 39, symptom, "s", "start"),
                _t(560, y + 31, fix, "s accent", "start"),
                _line(SPINE, y + RH / 2, 105, y + RH / 2)]
    return _svg(W, y0 + len(rows) * (RH + GAP) - GAP + 6, "".join(out), 640)


def rhythm() -> str:
    """A day of the schedule, and the two lanes that are yours."""
    W, X0, X1, AY = 880, 70, 850, 150
    span = X1 - X0

    def hx(h: float) -> float:
        return X0 + span * h / 24

    out = [_t(0, 44, "every 30 min", "s", "start"),
           _t(0, 62, "telemetry", "s", "start")]
    for i in range(48):
        x = hx(i / 2)
        out.append(_line(x, 46, x, 56, "l tick", False))
    out += [_line(X0, AY, X1, AY, "l axis", False),
            _t(0, AY + 4, "the tick", "s", "start")]
    for h in range(0, 25, 4):
        x = hx(h)
        out += [_line(x, AY, x, AY + 7, "l axis", False),
                _t(x, AY + 24, f"{h:02d}:00", "s")]
    for h in range(0, 24, 2):
        x = hx(h + 0.25)
        out.append(f'<circle cx="{_n(x)}" cy="{_n(AY)}" r="5" class="b accent"/>')
    x = hx(23.5)
    out += [_line(x, AY - 46, x, AY),
            _box(x - 190, AY - 76, 190, 32, "23:30  consolidation", cls="b accent"),
            _t(hx(2), AY - 22, "dbwiki run — every 2h at :15", "s accent", "start"),
            _t(hx(0.2), AY - 40, "compact → decide → ingest → report → render → alerts",
               "s", "start")]
    y = AY + 60
    weekly = [("Mon 08:00", "dbwiki lint"), ("Mon 09:00", "dbwiki research"),
              ("1st 09:00", "research --sources-only")]
    out.append(_t(0, y + 26, "weekly", "s", "start"))
    for i, (when, what) in enumerate(weekly):
        out += [_box(70 + i * 270, y, 250, 44, cls="b"),
                _t(86 + i * 270, y + 19, when, "s mono-strong", "start"),
                _t(86 + i * 270, y + 35, what, "s", "start")]
    y += 62
    yours = [("daily", "glance at wiki/html/index.html — it is the product"),
             ("on noise", "dbwiki health, then the runbook")]
    out.append(_t(0, y + 26, "you", "s accent", "start"))
    for i, (when, what) in enumerate(yours):
        out += [_box(70 + i * 405, y, 385, 44, cls="b ok"),
                _t(86 + i * 405, y + 19, when, "s mono-strong", "start"),
                _t(86 + i * 405, y + 35, what, "s", "start")]
    return _svg(W, y + 56, "".join(out), 640)


# ---- card grids (markup, not drawing) ---------------------------------------

JOBS = [
    ("see what happened overnight", "open wiki/html/index.html",
     "needs attention · worth a look · routine", "user-guide"),
    ("know why a db produced nothing", "dbwiki run --explain",
     "the real decision path, read-only", "user-guide"),
    ("fix a batch of failed ingests", "dbwiki health &amp;&amp; dbwiki retry",
     "fix the cause first, then replay", "monitoring"),
    ("look up an ORA code", "wiki/errors/&lt;CODE&gt;.md",
     "occurrences, contexts, what fixed it", "user-guide"),
    ("re-do one day for one db", "dbwiki compact … &amp;&amp; dbwiki ingest …",
     "rewrites, never duplicates", "user-guide"),
    ("change which model a stage spends", "edit agents.*, then dbwiki stats",
     "measure before you trust it", "user-guide"),
    ("work out why a tick did nothing", ".state/cron.log",
     "silent when healthy", "monitoring"),
    ("bootstrap history", "dbwiki backfill --from … --to …",
     "compaction only, no model, idempotent", "user-guide"),
]


def jobs() -> str:
    cards = []
    for title, cmd, hint, route in JOBS:
        cards.append(
            f'<a class="job" href="#{route}" data-route="{route}">'
            f'<span class="eyebrow">I want to</span>'
            f'<span class="jt">{_esc(title)}</span>'
            f'<code>{cmd}</code>'
            f'<span class="jh">{_esc(hint)}</span></a>')
    return f'<div class="jobs">{"".join(cards)}</div>'


FIGURES = {
    "pipeline": pipeline,
    "layers": layers,
    "trigger": trigger,
    "modes": modes,
    "rails": rails,
    "tick": tick,
    "drilldown": drilldown,
    "triage": triage,
    "rhythm": rhythm,
    "jobs": jobs,
}
