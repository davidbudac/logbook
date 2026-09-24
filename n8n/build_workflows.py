#!/usr/bin/env python3
"""Generate n8n workflow JSON from config/schedule.json. Stdlib only.

An *exploration* of n8n as the trigger/watchdog layer next to cron — nothing
here replaces the crontab. Same idea as elk/kibana/build_dashboards.py: the
generator is the source of truth, `workflows/*.json` is regenerated output,
fixed ids so a re-import overwrites in place.

    ./build_workflows.py            # dry mode (default): side-effect-free commands
    ./build_workflows.py --live     # the real crontab command lines
    ./build_workflows.py --print    # list what would be written

What is generated (one file per workflow, all imported INACTIVE):

    dbwiki-<entry>      one per on-prem entry in config/schedule.json:
                        Schedule Trigger (same cron expr) -> SSH to the host
                        (same command line as the crontab) -> IF exit != 0
                        -> Stop and Error (which fires the error workflow)
    dbwiki-errors       Error Trigger -> append a JSON line to
                        .state/n8n/events.jsonl on the host. Round-level
                        alerting: fires when a round could not run at all
                        (host unreachable, uv missing, non-zero exit) — the
                        gap dbwiki's own alerts (which need a running round)
                        cannot cover.
    dbwiki-watchdog     every 15 min: n8n/scripts/watchdog.py on the host —
                        model-server liveness, tick staleness, crontab drift.

Dry mode: only `dbwiki run --explain` runs for real (documented as no agent
call, no watermark/ledger writes, no run-health event). Every other entry is
an `echo` of the command it would run — `lint --deterministic-only` and
`research --dry-run` still write run_health events and would pollute the
loop-overview dashboard with n8n-triggered rounds.

Why SSH: n8n runs in Docker (~/n8n/compose.yml); the host has uv, codex,
lms and the git working tree. Alternatives (bind-mounting the repo into the
n8n image, a host-side HTTP runner) mean a second runtime for the same
Python — SSH keeps exactly one place where dbwiki executes.
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCHEDULE = ROOT / "config" / "schedule.json"
OUT = HERE / "workflows"

# Mirrors the PATH= line of the installed crontab (docs/scheduling.md): an
# ssh exec is a non-interactive shell and sees none of uv, lms, codex.
CRON_PATH = ("/opt/logbook/.lmstudio/bin:"
             "/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin")
REPO_ON_HOST = "/opt/logbook"
TIMEZONE = "Europe/Berlin"           # host cron runs in local time; match it

# Fixed ids -> re-import overwrites. n8n accepts any string id on import.
CRED_ID = "dbwiki-host-ssh"
CRED_NAME = "dbwiki host (ssh)"
ERR_WF_ID = "dbwiki-errors"
WATCHDOG_WF_ID = "dbwiki-watchdog"

# entry -> command that is genuinely side-effect-free. Anything not listed
# is echoed in dry mode.
DRY_COMMANDS = {
    "tick": "uv run dbwiki run --explain",
    "consolidate": "uv run dbwiki run --explain --consolidate",
}

# Long enough for the 1800 s agent timeout plus retries; n8n aborts after this.
EXECUTION_TIMEOUT_S = 3 * 3600


def load_schedule() -> list[dict]:
    return json.loads(SCHEDULE.read_text())["entries"]


# ---- node builders ------------------------------------------------------------

def schedule_trigger(cron: str, pos=(0, 0)) -> dict:
    return {
        "parameters": {"rule": {"interval": [
            {"field": "cronExpression", "expression": cron}]}},
        "name": "Schedule",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": list(pos),
    }


def ssh_exec(name: str, command: str, pos, stdin_devnull: bool = True) -> dict:
    """Run `command` on the host as the crontab user. The SSH node returns
    {stdout, stderr, code, signal} and does not throw on non-zero exit —
    only on connection failure — so exit status is checked by an IF node.

    stdin is redirected from /dev/null: `pi -p` reads a non-TTY stdin to EOF
    before it starts, so a remote exec that leaves stdin open would hang a
    pi round until the execution timeout (verified 2026-08-15; cron gives
    /dev/null, which is why the crontab never sees this)."""
    if stdin_devnull:
        command = f"{command} </dev/null"
    return {
        "parameters": {
            "authentication": "privateKey",
            "command": f"export PATH={CRON_PATH}; cd {REPO_ON_HOST} && {command}",
            "cwd": REPO_ON_HOST,
        },
        "name": name,
        "type": "n8n-nodes-base.ssh",
        "typeVersion": 1,
        "position": list(pos),
        "credentials": {"sshPrivateKey": {"id": CRED_ID, "name": CRED_NAME}},
    }


def if_nonzero(pos) -> dict:
    return {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": "exit-code",
                    "leftValue": "={{ $json.code }}",
                    "rightValue": 0,
                    "operator": {"type": "number", "operation": "notEquals"},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "name": "Exit != 0?",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": list(pos),
    }


def stop_and_error(label: str, pos) -> dict:
    msg = ("=" + label + " exited {{ $json.code }}: "
           "{{ String($json.stderr || $json.stdout || '').slice(-800) }}")
    return {
        "parameters": {"errorMessage": msg},
        "name": "Fail the round",
        "type": "n8n-nodes-base.stopAndError",
        "typeVersion": 1,
        "position": list(pos),
    }


def noop(name: str, pos) -> dict:
    return {"parameters": {}, "name": name, "type": "n8n-nodes-base.noOp",
            "typeVersion": 1, "position": list(pos)}


def wire(*names: str) -> dict:
    """Linear main-output connections a -> b -> c."""
    conns = {}
    for a, b in zip(names, names[1:]):
        conns[a] = {"main": [[{"node": b, "type": "main", "index": 0}]]}
    return conns


def workflow(wf_id: str, name: str, nodes: list, connections: dict,
             error_workflow: str | None = ERR_WF_ID, tags=("dbwiki",)) -> dict:
    settings = {
        "executionOrder": "v1",
        "timezone": TIMEZONE,
        "saveManualExecutions": True,
        "saveDataErrorExecution": "all",
        "saveDataSuccessExecution": "all",
        "executionTimeout": EXECUTION_TIMEOUT_S,
    }
    if error_workflow:
        settings["errorWorkflow"] = error_workflow
    return {
        "id": wf_id,
        "name": name,
        "active": False,          # imported inactive — the operator flips it
        "nodes": nodes,
        "connections": connections,
        "settings": settings,
        "tags": [{"id": t, "name": t} for t in tags],
        "meta": {"templateCredsSetupCompleted": True},
    }


# ---- the workflows --------------------------------------------------------------

def entry_workflow(entry: dict, live: bool) -> dict:
    key = entry["entry"]
    real_cmd = f"uv run {entry['command']}" \
        if entry["command"].startswith("dbwiki ") else entry["command"]
    if live:
        cmd = real_cmd
    elif key in DRY_COMMANDS:
        cmd = DRY_COMMANDS[key]
    else:
        cmd = f"echo '[n8n dry-run] would run: {real_cmd}'"
    label = f"dbwiki {key}"
    nodes = [
        schedule_trigger(entry["cron"], (0, 0)),
        ssh_exec("Run on host", cmd, (240, 0)),
        if_nonzero((480, 0)),
        stop_and_error(label, (720, -120)),
        noop("Round OK", (720, 120)),
    ]
    conns = wire("Schedule", "Run on host", "Exit != 0?")
    conns["Exit != 0?"] = {"main": [
        [{"node": "Fail the round", "type": "main", "index": 0}],
        [{"node": "Round OK", "type": "main", "index": 0}],
    ]}
    mode = "" if live else " [dry]"
    return workflow(f"dbwiki-{key}", f"dbwiki {key}{mode} — {entry['description']}",
                    nodes, conns)


def error_workflow() -> dict:
    """Error Trigger -> one JSON line per failed execution, appended on the
    host. Deliberately NOT .state/alerts.jsonl (dbwiki's own sink, parsed by
    an ES ingest pipeline with its own schema)."""
    line = ("={{ JSON.stringify({"
            "ts: $now.toISO(), "
            "source: 'n8n', "
            "workflow: $json.workflow.name, "
            "workflow_id: $json.workflow.id, "
            "execution_id: $json.execution.id, "
            "execution_url: $json.execution.url, "
            "last_node: $json.execution.lastNodeExecuted, "
            "error: $json.execution.error.message"
            "}) }}")
    set_node = {
        "parameters": {
            "assignments": {"assignments": [{
                "id": "line", "name": "line", "type": "string", "value": line}]},
            "options": {},
        },
        "name": "Format event",
        "type": "n8n-nodes-base.set",
        "typeVersion": 3.4,
        "position": [240, 0],
    }
    append = ssh_exec(
        "Append to .state/n8n/events.jsonl",
        "mkdir -p .state/n8n && cat >> .state/n8n/events.jsonl <<'EOF'\n"
        "{{ $json.line }}\nEOF",
        (480, 0), stdin_devnull=False)
    nodes = [
        {"parameters": {}, "name": "Error Trigger",
         "type": "n8n-nodes-base.errorTrigger", "typeVersion": 1,
         "position": [0, 0]},
        set_node,
        append,
        # Placeholder for a real notification channel — see README.
        noop("TODO: notify (Telegram/ntfy/email)", (720, 0)),
    ]
    conns = wire("Error Trigger", "Format event",
                 "Append to .state/n8n/events.jsonl",
                 "TODO: notify (Telegram/ntfy/email)")
    return workflow(ERR_WF_ID, "dbwiki errors — round-level alerting",
                    nodes, conns, error_workflow=None)


def watchdog_workflow() -> dict:
    nodes = [
        schedule_trigger("*/15 * * * *", (0, 0)),
        ssh_exec("Watchdog on host", "python3 n8n/scripts/watchdog.py", (240, 0)),
        if_nonzero((480, 0)),
        {
            "parameters": {"errorMessage":
                           "=dbwiki watchdog: {{ String($json.stdout).slice(-800) }}"},
            "name": "Fail the round",
            "type": "n8n-nodes-base.stopAndError",
            "typeVersion": 1,
            "position": [720, -120],
        },
        noop("Healthy", (720, 120)),
    ]
    conns = wire("Schedule", "Watchdog on host", "Exit != 0?")
    conns["Exit != 0?"] = {"main": [
        [{"node": "Fail the round", "type": "main", "index": 0}],
        [{"node": "Healthy", "type": "main", "index": 0}],
    ]}
    return workflow(WATCHDOG_WF_ID,
                    "dbwiki watchdog — model server, tick staleness, crontab drift",
                    nodes, conns)


def build(live: bool) -> dict[str, dict]:
    out = {}
    for e in load_schedule():
        if e.get("node", "onprem") != "onprem":
            continue            # analyst entry runs on another machine
        wf = entry_workflow(e, live)
        out[wf["id"]] = wf
    out[ERR_WF_ID] = error_workflow()
    out[WATCHDOG_WF_ID] = watchdog_workflow()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--live", action="store_true",
                    help="real crontab command lines (default: dry, side-effect-free)")
    ap.add_argument("--print", action="store_true", help="list, don't write")
    args = ap.parse_args()
    wfs = build(args.live)
    if args.print:
        for wf in wfs.values():
            cmds = [n["parameters"]["command"] for n in wf["nodes"]
                    if n["type"] == "n8n-nodes-base.ssh"]
            print(f"{wf['id']:22} {wf['name']}")
            for c in cmds:
                print(f"{'':22}   $ {c.splitlines()[0]}")
        return 0
    OUT.mkdir(exist_ok=True)
    for wf_id, wf in wfs.items():
        (OUT / f"{wf_id}.json").write_text(json.dumps(wf, indent=1) + "\n")
    mode = "live" if args.live else "dry"
    print(f"wrote {len(wfs)} workflows ({mode}) to {OUT.relative_to(ROOT)}/",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
