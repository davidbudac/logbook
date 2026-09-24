---
type: database
db: cdb1
aliases: [CDB1, cdb1.world]
updated: 2026-08-14T12:03:00Z
---

# cdb1

Host `lab-dg1.localdomain` (192.0.2.121). Oracle Database 19c Enterprise
Edition 19.0.0.0.0, Version 19.27.0.0.0, RU 19.27.0.0.250415 (37642901).
`db_name` / `db_unique_name` = `cdb1`, `db_domain = "world"`.

## Data Guard

- `log_archive_dest_2 = service="cdb1_stby.world"`, `db_unique_name="cdb1_stby"`.
- Standby: [[databases/cdb1_stby]] on `lab-dg2.localdomain` (192.0.2.122).
- `control_files` includes `"/u01/oradata/CDB1/control01.ctl"`.

(observed — digests/cdb1/2026-07-28.md)
