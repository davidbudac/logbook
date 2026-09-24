"""The failure vocabulary: exception types, category names, recovery hints.

`health` names failures (`categorize`), `orchestrate` raises them, `alerts`
and `health.format_health` turn a category into the command that clears it.
All three need these definitions and each already imports another of them,
so the definitions live here, below all three.

This module imports nothing from `dbwiki`; `tests/test_imports.py` holds it
to that. The names stay importable from where they used to live
(`orchestrate.ValidationError`, `health.WikiMissingError`/`CATEGORIES`/
`RETRYABLE`, `alerts.recovery_hint`/`COLLECTOR_HOST`).
"""

import os
from collections.abc import Sequence

# stable failure categories; every recorded failure carries exactly one
CATEGORIES = ("es_unreachable", "unsupported_schema", "harness_error",
              "agent_timeout", "no_result", "validation_failed", "dirty_tree",
              "commit_failed", "wiki_missing", "lock_busy", "stage_stale",
              "dependency_failure", "unknown")

# categories a plain rerun can plausibly fix (dirty_tree/wiki_missing need an
# operator; es_unreachable/unsupported_schema need the window recompacted)
RETRYABLE = ("validation_failed", "harness_error", "agent_timeout", "no_result")


class ValidationError(Exception):
    """A stage's result or edits failed the rails. `problems` and
    `lint_findings` ride along for the ledger entry and the telemetry block
    the stage wrapper (`Orchestrator._stage`) records; a plain
    `ValidationError(message)` carries the message as its one problem."""

    def __init__(self, message: str = "", *, problems: Sequence[str] | None = None,
                 lint_findings: int = 0):
        super().__init__(message)
        self.problems = list(problems) if problems is not None else [message]
        self.lint_findings = lint_findings


class WikiMissingError(RuntimeError):
    """The wiki repository this checkout points at does not exist."""


# where the log shippers run; named in the collection-failure recovery hint.
# Deployment-specific, so the environment names it; the default is generic.
COLLECTOR_HOST = os.environ.get("DBWIKI_COLLECTOR_HOST", "the collector host")

_HINTS = {
    "collection_failure":
        f"restart the collector on {COLLECTOR_HOST} (filebeat/logstash ships "
        f"nothing to Elasticsearch), then: dbwiki health",
    "source_silent":
        f"check the {{source}} shipper on {COLLECTOR_HOST}, then: dbwiki health",
    "es_unreachable":
        "check the Elasticsearch endpoint configured in config/dbwiki.yaml, "
        "then: dbwiki health",
    "unsupported_schema":
        "compare sources.*.db_fields in config/dbwiki.yaml with the current ES "
        "mapping, then: dbwiki health",
    "wiki_missing": "restore the wiki/ checkout (clone the wiki repo beside "
                    "config/), then: dbwiki health",
    "dirty_tree": "commit or stash the non-digest changes in wiki/, then: "
                  "dbwiki health",
    "commit_failed": "fix the wiki repository by hand (lock/remote), then: "
                     "dbwiki retry",
    "stale_watermark": "dbwiki run",
    "digest_backlog": "dbwiki run --consolidate",
    "stage_stale": "check .state/cron.log and the crontab; run "
                   "`uv run dbwiki {detail}` by hand",
    "delivery_failed": "check the SMTP relay (delivery.smtp_host in "
                       "config/dbwiki.yaml), then: dbwiki review "
                       "--deliver-only --review-id {detail}",
}

_DEP_HINTS = {
    "model_server": "start the local model server (unsloth studio: `unsloth "
                    "start`); see docs/scheduling.md \"What the local stages "
                    "need to be up\"",
    "adapter_missing": "install/authenticate the adapter and make sure cron's "
                       "PATH includes it (docs/scheduling.md)",
    "git_identity": "git -C wiki config user.email <you@example.com>",
    "wiki_no_upstream": "git -C wiki push -u origin HEAD (or set report.push "
                        "false)",
    "unpushed": "cd wiki && git push",
    "disk": "free space under .state/",
    "portal": "systemctl --user restart dbwiki-portal (or `uv run dbwiki "
              "portal serve`); until then use `dbwiki incident ...`",
}


#: stage names that are not themselves a `dbwiki` subcommand
_STAGE_COMMANDS = {"research_history": "research --history",
                   "research_caveats": "research --caveats"}


def recovery_hint(category: str, db: str = "-", source: str = "-", *,
                  detail: str | None = None) -> str:
    """The exact next action for a category: a command where one exists, the
    physical action otherwise. Retryable failure categories are read from
    `RETRYABLE` rather than re-listed. `detail` names *which* dependency or
    stage the finding is about, so one category can still give one specific
    command."""
    if category in RETRYABLE:
        return f"dbwiki retry --db {db}" if db != "-" else "dbwiki retry"
    if category == "dependency_failure":
        head = (detail or "").split(":")[0]
        return next((h for k, h in _DEP_HINTS.items() if head.startswith(k)),
                    "dbwiki health")
    if category == "stage_stale":
        detail = _STAGE_COMMANDS.get(detail or "", detail)
    return _HINTS.get(category, "dbwiki health").format(source=source,
                                                        detail=detail or "")
