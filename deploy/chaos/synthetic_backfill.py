#!/usr/bin/env python3
"""Backdated synthetic alert-log incidents for the demo.

The chaos injector (oracle_chaos.sh) only started on 2026-09-09, so its fault
codes (ORA-1653, ORA-60, ORA-376/1110) have no priors older than a few days.
This seeds a handful of the same faults weeks earlier so "has this happened
before" has a month-old repeat to point at. Everything else in the estate's
history is real and stays untouched.

Docs go to their own index, INDEX, which the alert source's
`oracle-logs-alert-*` pattern already matches. Every doc carries
labels.synthetic=true. Wiping is `delete index`.

  synthetic_backfill.py seed      write the docs (idempotent: fixed _id per line)
  synthetic_backfill.py wipe      drop the index
  synthetic_backfill.py status    doc count per day
  synthetic_backfill.py replay    dbwiki backfill + ingest for each seeded day
                                  (run from the checkout that owns wiki/ and .state/)
  synthetic_backfill.py actions   backdated operator actions on the incidents replay opened
                                  (slugs are what the 2026-09-13 replay named; edit ACTIONS after a fresh replay)

Env: ES_URL (http://localhost:9200), ES_USER (elastic), DBWIKI_ES_PASSWORD (changeme),
     DBWIKI (path to the dbwiki binary, default .venv/bin/dbwiki under cwd).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
ES_USER = os.environ.get("ES_USER", "elastic")
ES_PASS = os.environ.get("DBWIKI_ES_PASSWORD", "changeme")
INDEX = "oracle-logs-alert-synthetic"
DB, INST, HOST = "cdb1", "cdb1", "ol9-19-dg1.localdomain"
TRACE_DIR = "/u01/app/oracle/diag/rdbms/cdb1/cdb1/trace"
ALERT_LOG = f"{TRACE_DIR}/alert_cdb1.log"
DATAFILE = "/u01/oradata/CDB1/pdb1/chaos_ts01.dbf"


@dataclass(frozen=True)
class Line:
    at: str          # ISO UTC, second precision
    message: str     # alert-log text after the timestamp line; may be multi-line
    level: str = "INFO"


def ts_full(day: str, hh: int) -> list[Line]:
    """CHAOS_TS runs full with autoextend off; a DBA turns autoextend on 40 min later."""
    err = (f"Errors in file {TRACE_DIR}/cdb1_ora_{{pid}}.trc  (PDBNAME=PDB1):\n"
           f"ORA-1653: unable to extend table CHAOS.FILL by 1024 in tablespace CHAOS_TS")
    out = [Line(f"{day}T{hh:02d}:{m:02d}:{s:02d}Z", err.format(pid=pid), "ERROR")
           for m, s, pid in ((4, 11, 21833), (4, 12, 21833), (9, 47, 21901), (17, 3, 21977), (28, 30, 22040))]
    out.append(Line(f"{day}T{hh:02d}:44:05Z",
                    f"PDB1(3):alter database datafile '{DATAFILE}' autoextend on next 8m maxsize 256m"))
    out.append(Line(f"{day}T{hh:02d}:44:05Z",
                    f"PDB1(3):Completed: alter database datafile '{DATAFILE}' autoextend on next 8m maxsize 256m"))
    return out


def deadlock(day: str, hh: int) -> list[Line]:
    return [Line(f"{day}T{hh:02d}:22:41Z",
                 f"PDB1(3):ORA-00060: Deadlock detected. See Note 60.1 at My Oracle Support for "
                 f"Troubleshooting ORA-60 Errors. More info in file {TRACE_DIR}/cdb1_ora_18442.trc.",
                 "ERROR")]


def datafile_offline(day: str, hh: int) -> list[Line]:
    """File 12 goes offline, reads fail with ORA-376, media recovery brings it back."""
    return [
        Line(f"{day}T{hh:02d}:02:19Z", f"PDB1(3):alter database datafile '{DATAFILE}' offline"),
        Line(f"{day}T{hh:02d}:02:19Z", f"PDB1(3):Completed: alter database datafile '{DATAFILE}' offline"),
        Line(f"{day}T{hh:02d}:06:52Z",
             f"Errors in file {TRACE_DIR}/cdb1_ora_30117.trc  (PDBNAME=PDB1):\n"
             f"ORA-00376: file 12 cannot be read at this time\n"
             f"ORA-01110: data file 12: '{DATAFILE}'", "ERROR"),
        Line(f"{day}T{hh:02d}:31:10Z", f"PDB1(3):alter database recover datafile 12"),
        Line(f"{day}T{hh:02d}:31:10Z", "PDB1(3):Media Recovery Start\nPDB1(3):Serial Media Recovery started"),
        Line(f"{day}T{hh:02d}:31:12Z", "PDB1(3):Media Recovery Complete (cdb1)"),
        Line(f"{day}T{hh:02d}:31:12Z", "PDB1(3):Completed: alter database recover datafile 12"),
        Line(f"{day}T{hh:02d}:31:15Z", f"PDB1(3):alter database datafile '{DATAFILE}' online"),
        Line(f"{day}T{hh:02d}:31:15Z", f"PDB1(3):Completed: alter database datafile '{DATAFILE}' online"),
    ]


# day -> lines. Four incidents, one repeat (ts_full), all weeks before the
# injector went live on 2026-09-09.
SCENARIOS: dict[str, list[Line]] = {
    "2026-08-03": ts_full("2026-08-03", 3),
    "2026-08-12": datafile_offline("2026-08-12", 9),
    "2026-08-19": deadlock("2026-08-19", 14),
    "2026-08-26": ts_full("2026-08-26", 22),
}

MAPPING = {
    "mappings": {
        "dynamic_templates": [{"strings": {"match_mapping_type": "string",
                                           "mapping": {"type": "keyword", "ignore_above": 1024}}}],
        "properties": {"@timestamp": {"type": "date"},
                       "message": {"type": "text"},
                       "event": {"properties": {"original": {"type": "text"}}}},
    }
}


def es(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"{ES_URL}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    tok = base64.b64encode(f"{ES_USER}:{ES_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {tok}")
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise SystemExit(f"{method} {path}: {e.code} {e.read().decode()[:300]}")


def doc(line: Line) -> dict:
    codes = sorted(set(re.findall(r"ORA-\d{3,5}", line.message)))
    first = next((m for m in line.message.split("\n") if m.removeprefix("PDB1(3):").startswith("ORA-")), None)
    d = {
        "@timestamp": line.at.replace("Z", ".000Z"),
        "@version": "1",
        "message": line.message,
        "log_type": "oracle_alert",
        "labels": {"synthetic": "true", "environment": "prod", "criticality": "tier3"},
        "log": {"level": line.level, "file": {"path": ALERT_LOG},
                "flags": ["multiline"] if "\n" in line.message else []},
        "event": {"original": f"{line.at[:-1]}.000000+00:00\n{line.message}",
                  "dataset": "oracle.alert", "module": "oracle", "kind": "event",
                  "created": line.at, "ingested": line.at},
        "oracle": {"database": {"name": DB, "uid": f"{DB}@{HOST.split('.')[0]}"},
                   "instance": {"name": INST}},
        "host": {"name": HOST, "hostname": HOST},
        "agent": {"type": "filebeat", "name": HOST, "version": "9.4.2"},
        "data_stream": {"type": "logs", "dataset": "oracle.alert", "namespace": "default"},
        "ecs": {"version": "8.0.0"},
    }
    if codes:
        d["oracle"]["error_codes"] = codes
        code, _, msg = first.removeprefix("PDB1(3):").partition(": ")
        d["error"] = {"code": code, "message": msg}
    tr = re.search(r"(/u01/\S+\.trc)", line.message)
    if tr:
        d["oracle"]["trace"] = {"file": tr.group(1)}
    return d


def seed() -> None:
    if not es("GET", f"/{INDEX}"):
        es("PUT", f"/{INDEX}", MAPPING)
    n = 0
    for day, lines in SCENARIOS.items():
        for ln in lines:
            _id = hashlib.sha1(f"{ln.at}|{ln.message}".encode()).hexdigest()[:20]
            es("PUT", f"/{INDEX}/_doc/{_id}?refresh=false", doc(ln))
            n += 1
    es("POST", f"/{INDEX}/_refresh")
    print(f"seeded {n} docs into {INDEX} over {len(SCENARIOS)} days")


def wipe() -> None:
    es("DELETE", f"/{INDEX}")
    print(f"dropped {INDEX}")


def status() -> None:
    r = es("POST", f"/{INDEX}/_search?size=0",
           {"aggs": {"d": {"date_histogram": {"field": "@timestamp", "calendar_interval": "day"},
                           "aggs": {"c": {"terms": {"field": "error.code"}}}}}})
    if not r:
        print(f"{INDEX}: absent")
        return
    for b in r["aggregations"]["d"]["buckets"]:
        if b["doc_count"]:
            print(b["key_as_string"][:10], b["doc_count"], [x["key"] for x in b["c"]["buckets"]])


def replay() -> None:
    dbwiki = os.environ.get("DBWIKI", ".venv/bin/dbwiki")
    for day in SCENARIOS:
        for cmd in (["backfill", "--db", DB, "--from", day, "--to", day],
                    ["ingest", "--db", DB, "--date", day]):
            print("+", dbwiki, *cmd, flush=True)
            rc = subprocess.call([dbwiki, *cmd])
            if rc:
                raise SystemExit(f"{cmd[0]} {day} exited {rc}")



# who the recorded actions are attributed to; override for your own replay
ACTOR = os.environ.get("DBWIKI_CHAOS_ACTOR", "operator@example.com")
# (incident slug, verb, --at, flags). Slugs come from the 2026-09-13 replay.
ACTIONS: list[tuple[str, str, str, list[str]]] = [
    ("2026-08-03-cdb1-ora-1653-table-extension-failure", "record-action", "2026-08-03T04:10:00Z",
     ["--intent", "stop the ORA-1653 storm on CHAOS_TS",
      "--summary", "set autoextend on (next 8m maxsize 256m) for chaos_ts01.dbf and truncated CHAOS.FILL",
      "--outcome", "succeeded", "--rollback", "alter database datafile ... autoextend off",
      "--evidence", "digests/cdb1/2026-08-03.md"]),
    ("2026-08-03-cdb1-ora-1653-table-extension-failure", "resolve", "2026-08-04T08:00:00Z",
     ["--summary", "CHAOS_TS ran full with autoextend off; autoextend enabled, no ORA-1653 since",
      "--evidence", "digests/cdb1/2026-08-04.md"]),
    ("2026-08-12-cdb1-2026-08-12-cdb1-parse-errors", "record-action", "2026-08-12T09:35:00Z",
     ["--intent", "bring data file 12 back after ORA-376",
      "--summary", "recover datafile 12, then alter database datafile 12 online; media recovery took 2s",
      "--outcome", "succeeded", "--evidence", "digests/cdb1/2026-08-12.md"]),
    ("2026-08-19-cdb1-ora-16830-and-ora-60", "record-action", "2026-08-19T15:20:00Z",
     ["--intent", "stop the ORA-60 deadlocks on CHAOS.LOCKS",
      "--summary", "two sessions updated CHAOS.LOCKS ids 1 and 2 in opposite order; the batch now locks rows in id order",
      "--outcome", "succeeded", "--ticket", "APP-4471", "--evidence", "digests/cdb1/2026-08-19.md"]),
    ("2026-08-19-cdb1-ora-16830-and-ora-60", "resolve", "2026-08-20T09:00:00Z",
     ["--summary", "lock-order bug in the batch job; fixed, no ORA-60 for 18h",
      "--evidence", "digests/cdb1/2026-08-20.md"]),
    ("2026-08-26-cdb1-first-ever-oracle-errors", "record-action", "2026-08-26T22:50:00Z",
     ["--intent", "stop the ORA-1653 recurrence on CHAOS_TS",
      "--summary", "maxsize 256m had been reached; raised maxsize to 512m and purged CHAOS.FILL",
      "--outcome", "succeeded", "--rollback", "alter database datafile ... maxsize 256m",
      "--evidence", "digests/cdb1/2026-08-26.md"]),
    ("2026-08-26-cdb1-first-ever-oracle-errors", "resolve", "2026-08-27T08:30:00Z",
     ["--summary", "autoextend cap hit three weeks after the first fill; cap raised, load purged",
      "--residual-risk", "CHAOS.FILL grows unbounded; the next cap will be hit too",
      "--evidence", "digests/cdb1/2026-08-27.md"]),
]


def actions() -> None:
    dbwiki = os.environ.get("DBWIKI", ".venv/bin/dbwiki")
    for slug, verb, at, flags in ACTIONS:
        cmd = [dbwiki, "incident", verb, slug, "--at", at, "--actor", ACTOR, "--commit", "--lock-wait", "1800", *flags]
        print("+", verb, slug, at, flush=True)
        rc = subprocess.call(cmd)
        if rc:
            raise SystemExit(f"{verb} {slug} exited {rc}")
    subprocess.call([dbwiki, "research", "--history", "--lock-wait", "1800"])


if __name__ == "__main__":
    {"seed": seed, "wipe": wipe, "status": status, "replay": replay, "actions": actions}.get(
        sys.argv[1] if len(sys.argv) > 1 else "", lambda: print(__doc__))()
