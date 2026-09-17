# Lab chaos injector

**Status: the author's lab toy.** It breaks a throwaway Oracle database on
purpose so the pipeline has incidents to see; every host, port and path here
is that lab's. Never point it at a database you care about.

`oracle_chaos.sh` runs on the lab primary (`ssh -p 2200 oracle@dbhost`,
`cdb1`) from the oracle user's crontab. It periodically breaks one thing so
the alert log carries a real fault, then repairs it later. The wiki pipeline
sees the fault, an incident opens, and the recovery signal is met once the
repair lands.

## Scenarios

| name | fault | alert-log signal | repair |
|---|---|---|---|
| `ts_full` | fill `CHAOS_TS` (autoextend off) | `ORA-1653 unable to extend` | autoextend on, truncate |
| `datafile_offline` | datafile of `CHAOS_TS` offline | datafile offline / recovered / online | recover + online |
| `dg_defer` | `log_archive_dest_state_2=defer` | dest 2 deferred, standby gap | enable + switch logfile |
| `deadlock` | two sessions cross-update `chaos.locks` | `ORA-00060` + trace | self-resolving |
| `checkpoint_storm` | 12 fast log switches | `Checkpoint not complete` | none |
| `ora600_note` | `dbms_system.ksdwrt` message | synthetic `ORA-00600` line | none |

Fixtures (`CHAOS_TS` tablespace, `CHAOS` user, `FILL` and `LOCKS` tables in
`PDB1`) are created on first use. Timed scenarios pick a random duration in
their window; zero-duration ones repair on the next tick.

## Install

```sh
scp -P 2200 deploy/chaos/oracle_chaos.sh oracle@dbhost:chaos/
ssh -p 2200 oracle@dbhost 'crontab -l 2>/dev/null; echo "*/15 * * * * /home/oracle/chaos/oracle_chaos.sh tick >> /home/oracle/chaos/cron.log 2>&1"' | ssh -p 2200 oracle@dbhost 'crontab -'
```

Each idle tick starts a scenario with probability 1/`CHAOS_ODDS` (default 4),
and a repair is followed by `CHAOS_COOLDOWN_MIN` (default 30) quiet minutes.
Roughly one incident every 1-2 hours.

```sh
./oracle_chaos.sh status         # active scenario + recent log
./oracle_chaos.sh break ts_full  # force one now
./oracle_chaos.sh fix            # repair now
```

State lives in `~/chaos/state`, the log in `~/chaos/chaos.log`. Remove the
crontab line and run `fix` to stop.

## Synthetic priors for the demo (`synthetic_backfill.py`)

The injector went live on 2026-09-09, so its codes had no priors older than a
few days. `synthetic_backfill.py` writes four backdated incidents of the same
faults (ORA-1653 twice, ORA-376/1110 once, ORA-60 once, August 2026) into the
index `oracle-logs-alert-synthetic`, which the alert source pattern already
matches. Every doc carries `labels.synthetic: "true"`; the whole thing is
undone by dropping that index.

```
deploy/chaos/synthetic_backfill.py seed      # write the docs (idempotent)
deploy/chaos/synthetic_backfill.py replay    # dbwiki backfill + ingest per seeded day
deploy/chaos/synthetic_backfill.py status
deploy/chaos/synthetic_backfill.py wipe      # drop the index
```

`replay` must run from the checkout that owns `wiki/` and `.state/`. It
regenerates the four daily digests and ingests each one, so the wiki opens the
incidents dated on those days. Operator actions (resolve, record-action) are
then seeded the way the past-fixes emulation did, against the live
wiki. Real history is never edited.
