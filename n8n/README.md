# n8n as the trigger/watchdog layer — an exploration

**Status: side-by-side experiment. The crontab in `docs/scheduling.md` stays
the production scheduler; nothing here replaces it.** This directory answers
"what would it look like if n8n owned the *outer* loop" without touching
`dbwiki` itself.

## What n8n would and would not own

`dbwiki` orchestration today is cron → one synchronous Python process per
round → JSON files in `.state/` → git as the lock. The per-stage rails in
`src/dbwiki/orchestrate.py` (`_require_clean_tree` → agent → validate → lint
→ commit or rollback → ledger → telemetry) assume one process holding one
git working tree. None of that is workflow-shaped, and none of it moves.

What plausibly *is* n8n-shaped is the layer cron provides today, plus what
cron cannot:

| concern | cron today | n8n here |
|---|---|---|
| firing rounds on a schedule | 6 crontab lines, `PATH=` by hand | `dbwiki-<entry>` workflows, generated from `config/schedule.json` |
| "did the round run at all?" | silent; only `cron.log` grows on failure | execution list per workflow, non-zero exit → error workflow |
| host unreachable / `uv` missing / non-zero exit | nothing | `dbwiki-errors` appends to `.state/n8n/events.jsonl` (+ notify hook) |
| model server down, model unloaded, 8k context | invisible (`docs/scheduling.md` "nothing in dbwiki reports this") | `dbwiki-watchdog` every 15 min |
| `schedule.json` ↔ crontab drift | "nothing enforces they stay in sync" | watchdog warns; n8n workflows are *generated from* `schedule.json` |
| single-flight across rounds | none (before `src/dbwiki/lock.py`) | **still none** — n8n does not serialize a manual `dbwiki research` against a scheduled round; the flock lock is needed either way |
| dbwiki-level failure alerts | `.state/alerts.jsonl` (needs a running round) | unchanged; the two sinks are complementary |

## Layout

```
n8n/
  build_workflows.py      config/schedule.json -> workflows/*.json (stdlib)
  workflows/*.json        generated; fixed ids; re-import overwrites in place
  scripts/watchdog.py     model server / tick staleness / crontab drift; JSON + exit code
  setup.sh                ssh key, start n8n, import credential + workflows (all inactive)
```

Same idiom as `elk/kibana/build_dashboards.py` + `elk/setup.sh`: the
generator is the source of truth, output is regenerated, ids are fixed.

### How a container reaches the host

n8n runs in Docker (`~/n8n/compose.yml`). The host has `uv`, `codex`, `lms`
and the wiki working tree, so every workflow runs its command **over SSH to
the host** as the crontab user, with the crontab's `PATH=` exported first.
The SSH node returns `{stdout, stderr, code}` and does not throw on non-zero
exit, so each workflow is:

```
Schedule (same cron expr) -> SSH "cd repo && uv run dbwiki …" -> IF code != 0 -> Stop and Error
                                                                            \-> Round OK
```

`Stop and Error` fires `dbwiki-errors` (Error Trigger → JSON line →
`.state/n8n/events.jsonl` on the host → placeholder notify node). This is
*round-level* alerting: it fires when a round could not run, which
`dbwiki`'s own alerts by construction cannot see.

Rejected alternatives: bind-mounting the repo into the n8n image or a
host-side HTTP runner — both make a second runtime for the same Python.
SSH keeps exactly one place where `dbwiki` executes.

## Dry vs live

`build_workflows.py` defaults to **dry**: only `dbwiki run --explain` runs
for real (documented as no agent call, no watermark/ledger writes, no
run-health event). Every other entry `echo`es the command it would run —
`lint --deterministic-only` and `research --dry-run` still write
`run_health` events and would pollute the loop-overview dashboard with
n8n-triggered rounds. Workflow names carry `[dry]`.

`--live` emits the real crontab command lines. **Activating a live workflow
while its crontab line is installed runs the round twice** (and the two will
collide on the working tree — see the single-flight issue). Live is for
after a crontab line has been removed, not before.

## Try it

```sh
n8n/setup.sh --test        # dry workflows, imported inactive, then runs the watchdog once
```

`setup.sh` (idempotent): generates `~/.ssh/n8n_dbwiki` and appends it to
`authorized_keys` (tagged `n8n-dbwiki`), starts the n8n service, imports the
`sshPrivateKey` credential and the eight workflows via the n8n CLI. Then:

- editor at <http://127.0.0.1:5678/> — tag `dbwiki`
- run `dbwiki watchdog` by hand from the editor, or activate it — it is the
  one workflow that is safe to leave on next to cron (read-only, and its
  15-min cadence does not exist in the crontab)
- activate `dbwiki tick [dry]` to see `--explain` output land in n8n at
  every :15 of even hours, next to the real cron round
- `.state/n8n/events.jsonl` collects round-level failures

The watchdog is also useful without n8n at all:

```sh
python3 n8n/scripts/watchdog.py     # exit 1 + JSON when something is wrong
```

Revoke: `sed -i '/n8n-dbwiki/d' ~/.ssh/authorized_keys && rm ~/.ssh/n8n_dbwiki*`,
delete the `dbwiki` tag's workflows in the editor.

## What to judge after a week next to cron

1. Did the watchdog catch anything the ELK dashboards did not, and sooner?
   (First run already flagged LM Studio down and a stale `@reboot` autostart line,
   since removed from the crontab.)
2. Did `dbwiki-errors` fire for anything real, or only for the dry echoes?
3. Is having a service that must itself stay up (n8n + runners + caddy +
   sandbox — five containers) worth it for six cron lines? The failure mode
   "n8n is down so nothing fires" is new; cron does not have it.
4. Would a `dbwiki doctor` command (the watchdog checks folded into the tick)
   plus generating the crontab *from* `schedule.json` buy the same without
   the service? That is the alternative this exploration should be measured
   against, not cron as it stands.

## Not done here, deliberately

- No notification channel wired (Telegram/ntfy/email) — the `TODO: notify`
  node is where it goes; pick the sink and it is one node.
- No `dbwiki analyst` workflow: it runs on the analyst workstation
  (ADR-0001). A push-triggered claim (GitHub webhook → n8n → analyst) is the
  other genuinely n8n-shaped idea and is a separate experiment.
- No changes to `dbwiki`, the crontab, filebeat inputs or ES pipelines.
  `.state/n8n/` is not shipped to ELK.
