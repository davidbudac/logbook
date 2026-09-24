# Documentation

Start here.

## Guides

| | |
|---|---|
| [start-here.md](start-here.md) | what it does for you, your first ten minutes, the jobs you will actually do, the day it runs — **read this first** |
| [overview.md](overview.md) | what the system is, the one big idea, what it produces, vocabulary |
| [how-it-works.md](how-it-works.md) | the pipeline end to end: compaction, the trigger decision, the agent stages, the rails, state and replay semantics |
| [user-guide.md](user-guide.md) | setup, the config blocks that matter, every command, the incident workbench, recipes, reading the wiki, installing the schedule |
| [monitoring.md](monitoring.md) | `dbwiki health` explained, failure categories and their recovery commands, alerts, telemetry, the runbook |

The same five guides read as one browsable page in
[site.html](site.html) — a standalone file, no assets, open it in a browser.
It adds drawn diagrams the plain markdown cannot: a figure is opted into from
the markdown with an HTML comment, `<!-- figure: name | caption -->`, which
replaces the ASCII or table fallback that follows it. The figures live in
[build_site.py](build_site.py)'s companion [figures.py](figures.py).
Regenerate after editing any guide:

```sh
python3 docs/build_site.py            # -> docs/site.html
python3 docs/build_site.py --fragment # same page without the html/head skeleton
```

## References

| | |
|---|---|
| [scheduling.md](scheduling.md) | the installed crontab, the wake/skip decision, which model each stage spends, what the local stages need to be up |
| [provenance.md](provenance.md) | evidence kinds, citation anchors, the deterministic lint rule set |
| [langfuse.md](langfuse.md) | trace export: setup, exported fields, caveats |
| [awr-input.md](awr-input.md) | the AWR summary contract v1 |
| [notes/incident-remediation-workflow.md](notes/incident-remediation-workflow.md) | recording a remediation and resolving an incident: verified behavior, the status drift, the proposed workbench, the decision log |
| [reference.md](reference.md) | every knob, command, state file and telemetry field: the operator reference |
| [adr/](adr/) | decisions: 0001 on-prem/analyst split, 0002 anonymized research offload, 0003 incident lifecycle and action records, 0004 review delivery and email, 0005 practitioner caveats off-box, 0006 ingest reads database history |
| `../DESIGN.md` | architecture rationale, layer model, deferred extensions |
| `../CHANGELOG.md` | what changed when, with the measurements behind current settings |
| `../wiki/AGENTS.md` | the contract every agent writing into the wiki must follow |
| `../deploy/README.md` | running the incident workbench as a systemd user service: install, logs, the first-run check |
| `../elk/README.md` | telemetry shipping and the Kibana dashboards |
