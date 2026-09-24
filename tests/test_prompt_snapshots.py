"""Every prompt builder, rendered over fixed fixture inputs, pinned byte for
byte against `tests/fixtures/prompt_snapshots/`.

A net under the prompt-text extraction: moving the
instruction blocks out of the modules into `config/prompts/` must not move a
single byte of what a model is sent, nor of what `promptreg` registers with
Langfuse (a changed text would create a new prompt version). Any wording
change is therefore a deliberate snapshot update, reviewed as a diff:

    UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest tests/test_prompt_snapshots.py

No model, no network, no git: builders only.
"""

import json
import os
from pathlib import Path

import pytest
from fixtures import make_config
from fixtures.incident_pages import incident_page

from dbwiki import (advisory, orchestrate, promptreg, research_caveats,
                    research_offload, research_structured, review, structured)
from dbwiki.digest_md import render_md
from dbwiki.incidents import read_incident
from dbwiki.transaction import Tree
from dbwiki_researcher import cli as researcher

SNAPSHOTS = Path(__file__).parent / "fixtures" / "prompt_snapshots"
UPDATE = os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1"

DAY = "2026-07-12"
WINDOW = (f"{DAY}T00:00:00Z", f"{DAY}T20:19:00Z")

DIGEST = {
    "db": "cdb1",
    "window": {"from": f"{DAY}T00:00:00Z", "to": "2026-07-13T00:00:00Z",
               "day": DAY},
    "generated_by": "dbwiki-compactor/test",
    "pattern_versions": {"alert": 1},
    "sources": {"alert": {
        "total_events": 12, "by_class": {"error": 12}, "routine_counters": {},
        "notable": [{"rule": "ora_error", "class": "error", "count": 12,
                     "first_ts": f"{DAY}T01:00:00Z", "last_ts": f"{DAY}T04:00:00Z",
                     "codes": ["ORA-00600"], "message": "ORA-00600: internal error",
                     "template": "ORA-#: internal error", "es_samples": []}]}},
    "deltas": [{"type": "first_ever_code", "value": "TNS-12543",
                "source": "listener", "first_seen": f"{DAY}T00:00:32Z"}],
    "totals": {"events": 12, "notable_events": 12, "notable_groups": 1},
    "notable": True,
}

QUIET_DIGEST = {**DIGEST,
                "sources": {"alert": {**DIGEST["sources"]["alert"],
                                      "notable": []}},
                "deltas": []}

NOTABLE_DIGEST_MD = (
    f"# Digest: cdb1 — {DAY}\n\n"
    "## Deltas (never seen before / anomalies)\n\n"
    "- **first-ever error code** `TNS-12543` (listener, first seen "
    f"{DAY}T00:00:32Z)\n\n"
    "## alert — 500 events\n\n"
    "### Notable\n\n"
    f"- **[error] ora_error** ×500 (`ORA-00600`) — {DAY}T01:00:00Z\n"
    "  > ORA-00600: internal error, arguments: [x]\n\n"
    "### Routine (counters)\n\n"
    "- `ORA-nothing`: 10\n"
)

HEALTH = ["- sources: alert=ok(0.0h) listener=ok(0.0h)",
          "- recovery evidence: none — no recovery event in the window"]

INGESTED = [{"db": "cdb1", "notable": True, "summary": "ORA-00600 storm",
             "trigger": "notable: ora_error"},
            {"db": "cdb2", "notable": False, "summary": "quiet day on cdb2"}]


@pytest.fixture
def wiki(tmp_path):
    """A wiki with everything any builder may read: an open incident, an
    error page, a journal (database history), a prior same-day report and a
    notable digest."""
    root = tmp_path / "wiki"
    (root / "digests" / "cdb1").mkdir(parents=True)
    (root / "digests" / "cdb1" / f"{DAY}.json").write_text(json.dumps(DIGEST))
    (root / "incidents").mkdir()
    (root / "incidents" / "2026-07-11-cdb1-standby-gap.md").write_text(
        incident_page("cdb1", "cdb1 loses its standby link",
                      opened="2026-07-11T00:00:00Z", body="Ongoing.",
                      error_codes=("ORA-00600",)))
    (root / "errors").mkdir()
    (root / "errors" / "ORA-00600.md").write_text(
        "---\ntype: error-class\n---\n\n# ORA-00600\n\n## Occurrences\n\n"
        "| day | db | note | evidence |\n|---|---|---|---|\n"
        "| 2026-06-01 | cdb1 | first seen | digests/cdb1/2026-06-01.md |\n")
    (root / "databases" / "cdb1" / "journal").mkdir(parents=True)
    (root / "databases" / "cdb1" / "journal" / "2026-07.md").write_text(
        "---\ntype: journal\ndb: cdb1\n---\n\n# cdb1 journal 2026-07\n\n"
        "## 2026-07-11 — cdb1 2026-07-11: standby transport stalled\n\n"
        "The standby fell behind.\n")
    (root / "reports").mkdir()
    (root / "reports" / f"{DAY}-0600.md").write_text("# morning report\n")
    (root / "index.md").write_text("---\ntype: index\n---\n\n# Logbook\n")
    (root / "log.md").write_text("---\ntype: log\n---\n\n# Log\n")
    (root / "AGENTS.md").write_text("# agents\n")
    return root


def ingest_wiki(root: Path) -> Path:
    """The ingest prompt renders the digest .md from the dict itself; the
    notable-report Material reads the .md on disk. One wiki serves both."""
    (root / "digests" / "cdb1" / f"{DAY}.md").write_text(render_md(DIGEST))
    return root


def report_wiki(root: Path) -> Path:
    (root / "digests" / "cdb1" / f"{DAY}.md").write_text(NOTABLE_DIGEST_MD)
    return root


# ---- advisory --------------------------------------------------------------------

ADVISORY_INCIDENT = read_incident(
    incident_page("cdb1", "ORA-00600 on cdb1", status="monitoring",
                  opened="2026-08-05T00:00:00Z", body="Apply stopped."),
    "incidents/2026-08-05-cdb1-ora-00600.md")

ADVISORY_PACK = advisory.EvidencePack((
    advisory.PackSection(kind=advisory.SourceKind.INCIDENT,
                         path="incidents/2026-08-05-cdb1-ora-00600.md",
                         heading="incidents/2026-08-05-cdb1-ora-00600.md",
                         text="# ORA-00600 on cdb1\n\nApply stopped.",
                         truncated=False),
    advisory.PackSection(kind=advisory.SourceKind.CLOSURE, path="",
                         heading="the monitoring window",
                         text="verdict: not_met", truncated=False),
))


def advisory_prompt(tool_id: str) -> str:
    spec = advisory.TOOLS[tool_id]
    target = advisory.Target.incident(
        ADVISORY_INCIDENT, tree=Tree.of({}), revision="0" * 40,
        question="did the restart fix it?" if advisory.asks(spec) else "")
    return advisory.build_prompt(spec, target, ADVISORY_PACK)


# ---- review ----------------------------------------------------------------------

def review_prompt() -> str:
    return review.EvidencePack((
        review.PackSection(kind="summary", heading="the selection", path="",
                           text="2 findings in the window", truncated=False),
        review.PackSection(kind="finding", heading="high stale_open/ORA-00600",
                           path="incidents/2026-07-11-cdb1-standby-gap.md",
                           text="open for 30 days", truncated=False),
    )).prompt()


# ---- researcher (ADR-0002) -------------------------------------------------------

RESEARCHER_RESEARCH = {
    "kind": "research", "code": "ORA-12154",
    "sources": [{"slug": "oracle-docs", "domains": ["docs.oracle.com"],
                 "status": "approved"},
                {"slug": "oracle-base", "domains": ["oracle-base.com",
                                                   "www.oracle-base.com"],
                 "status": "approved"},
                {"slug": "some-blog", "domains": ["example.org"],
                 "status": "deprecated"}]}

RESEARCHER_REVIEW = {
    "kind": "source-review", "slug": "oracle-docs",
    "url": "https://docs.oracle.com/", "domains": ["docs.oracle.com"],
    "last_reviewed": "2026-01-01", "previous_notes": "fine {{today}} {code}",
    "sources": RESEARCHER_RESEARCH["sources"]}


# ---- the table -------------------------------------------------------------------

SOURCES = [("oracle-docs", "official", ["docs.oracle.com"]),
           ("oracle-base", "community-expert", ["oracle-base.com",
                                                "www.oracle-base.com"])]


def renders(wiki: Path) -> dict[str, str]:
    """snapshot name -> rendered text. Every builder that sends text to a
    model, and every text promptreg registers."""
    out = {
        "ingest": structured.build_prompt("cdb1", DIGEST, ingest_wiki(wiki)),
        "ingest-no-history": structured.build_prompt(
            "cdb1", DIGEST, wiki, history_days=0),
        "ingest-quiet": structured.build_prompt("cdb2", QUIET_DIGEST, wiki),
        "retry": structured._RETRY.format(err="summary: missing"),
    }
    report_wiki(wiki)
    out.update({
        "report": structured.build_report_prompt(DAY, WINDOW, INGESTED, wiki,
                                                 health=HEALTH),
        "report-empty": structured.build_report_prompt(DAY, WINDOW, [], wiki),
        "report-escalated": structured.build_escalated_report_prompt(
            DAY, WINDOW, INGESTED, wiki, health=HEALTH),
        "report-escalated-empty": structured.build_escalated_report_prompt(
            DAY, WINDOW, [], wiki / "nowhere"),
        "research-structured": research_structured.build_research_prompt(
            "ORA-00600", "ORA-00600: internal error code.\nCause: a bug."),
        "research-caveats": research_caveats.build_caveats_prompt(
            "ORA-00600", "An internal bug.", "- Contact support.", SOURCES),
        "research-caveats-no-sources": research_caveats.build_caveats_prompt(
            "ORA-00600", "An internal bug.", "- Contact support.", []),
        "research-offload-instructions":
            research_offload._RESEARCH_INSTRUCTIONS,
        "review-synthesis": review_prompt(),
        "agentic-history-rule": orchestrate.HISTORY_RULE,
        "researcher-research": researcher.build_prompt(RESEARCHER_RESEARCH,
                                                       today=DAY),
        "researcher-source-review": researcher.build_prompt(
            RESEARCHER_REVIEW, today=DAY),
        "researcher-no-sources": researcher.build_prompt(
            {**RESEARCHER_RESEARCH, "sources": []}, today=DAY),
    })
    for tool_id in advisory.TOOLS:
        out[f"advisory-{tool_id}"] = advisory_prompt(tool_id)
    cfg = make_config(wiki_repo=wiki)
    for name, text in promptreg.all_prompts(cfg):
        out["promptreg-" + name.replace("/", "_")] = text
    return out


def test_every_prompt_is_byte_identical_to_its_snapshot(wiki):
    rendered = renders(wiki)
    if UPDATE:
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        for stale in SNAPSHOTS.glob("*.txt"):
            stale.unlink()
        for name, text in rendered.items():
            (SNAPSHOTS / f"{name}.txt").write_bytes(text.encode())
    on_disk = {p.stem for p in SNAPSHOTS.glob("*.txt")}
    assert on_disk == set(rendered), (
        "snapshot set drifted: rerun with UPDATE_PROMPT_SNAPSHOTS=1 and "
        "review the diff")
    changed = [name for name, text in rendered.items()
               if (SNAPSHOTS / f"{name}.txt").read_bytes() != text.encode()]
    assert not changed, (f"prompt text changed: {changed} — a wording change "
                         f"is a reviewed snapshot update, never a side effect")


def test_the_snapshot_set_covers_every_advisory_tool_and_registered_prompt(wiki):
    names = set(renders(wiki))
    assert {f"advisory-{t}" for t in advisory.TOOLS} <= names
    assert {"promptreg-dbwiki_ingest-structured",
            "promptreg-dbwiki_report-structured",
            "promptreg-dbwiki_report-escalated",
            "promptreg-dbwiki_research-structured",
            "promptreg-dbwiki_agents-md"} <= names
