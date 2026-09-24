---
type: incident
status: open
added: 2026-07-13
updated: 2026-07-13T23:50:00Z
db: cdb1
---

# 2026-07-13 cdb1 telemetry blackout

## Status

open — no events from any source; cause not yet established (absence of
events is not evidence of a database outage).

## Evidence

- 2026-07-13: alert, listener and dataguard all silent for the full day
  (digests/cdb1/2026-07-13.md)
- 2026-07-13: the digest carries one `silence` delta per source, each raised
  against three preceding active days (digests/cdb1/2026-07-13.json)

## Next step

Confirm upstream collection health before drawing any database conclusion.
