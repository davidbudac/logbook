#!/usr/bin/env bash
# Chaos injector for the lab primary (cdb1 / PDB1). Periodically breaks one
# thing so the alert log carries a real fault, then repairs it later, so the
# logbook pipeline has incidents to detect, narrate and see recover.
#
#   oracle_chaos.sh tick            cron entry point (start / repair by state)
#   oracle_chaos.sh break <name>    start one scenario now
#   oracle_chaos.sh fix             repair the active scenario now
#   oracle_chaos.sh status | list
#
# Env: CHAOS_ODDS (1-in-N idle ticks start a scenario, default 4),
#      CHAOS_COOLDOWN_MIN (idle time after a repair, default 30).
set -uo pipefail

ORACLE_SID=cdb1
export ORACLE_SID
export ORAENV_ASK=NO
ORACLE_HOME=${ORACLE_HOME:-/u01/app/oracle/product/19.0.0/dbhome_1}; export ORACLE_HOME
PATH=$ORACLE_HOME/bin:/usr/local/bin:/usr/bin:/bin; export PATH
. /usr/local/bin/oraenv >/dev/null 2>&1

HOME_DIR=${CHAOS_HOME:-$HOME/chaos}
STATE=$HOME_DIR/state
LOG=$HOME_DIR/chaos.log
ODDS=${CHAOS_ODDS:-4}
COOLDOWN_MIN=${CHAOS_COOLDOWN_MIN:-30}
DATAFILE=/u01/oradata/CDB1/pdb1/chaos_ts01.dbf
mkdir -p "$HOME_DIR"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"; echo "$*"; }

sql() {
  sqlplus -s / as sysdba <<EOF
whenever sqlerror continue
set pages 0 feed off lines 200 trimspool on
$1
exit
EOF
}
pdb_sql() { sql "alter session set container=PDB1;
$1"; }

# name | min_minutes | max_minutes | weight
SCENARIOS='
ts_full           20 60 3
datafile_offline  15 45 2
dg_defer          20 45 2
deadlock           0  0 3
checkpoint_storm   0  0 2
ora600_note        0  0 1
'

ensure_fixtures() {
  pdb_sql "
declare n number;
begin
  select count(*) into n from dba_tablespaces where tablespace_name='CHAOS_TS';
  if n = 0 then
    execute immediate 'create tablespace chaos_ts datafile ''$DATAFILE'' size 8m autoextend off';
  end if;
  select count(*) into n from dba_users where username='CHAOS';
  if n = 0 then
    execute immediate 'create user chaos identified by chaos default tablespace chaos_ts quota unlimited on chaos_ts';
    execute immediate 'grant create session, create table to chaos';
  end if;
  select count(*) into n from dba_tables where owner='CHAOS' and table_name='FILL';
  if n = 0 then
    execute immediate 'create table chaos.fill (id number, pad varchar2(4000)) tablespace chaos_ts';
  end if;
  select count(*) into n from dba_tables where owner='CHAOS' and table_name='LOCKS';
  if n = 0 then
    execute immediate 'create table chaos.locks (id number primary key, v number) tablespace chaos_ts';
    execute immediate 'insert into chaos.locks values (1,0)';
    execute immediate 'insert into chaos.locks values (2,0)';
    commit;
  end if;
end;
/" >/dev/null
}

break_ts_full() {
  pdb_sql "alter database datafile '$DATAFILE' autoextend off;
truncate table chaos.fill drop storage;
declare i number := 0;
begin
  loop
    insert into chaos.fill select level, rpad('x',4000,'x') from dual connect by level <= 200;
    commit; i := i + 1;
    exit when i > 500;
  end loop;
exception when others then
  dbms_output.put_line(sqlerrm);
end;
/"
}
fix_ts_full() {
  pdb_sql "alter database datafile '$DATAFILE' autoextend on next 8m maxsize 256m;
truncate table chaos.fill drop storage;"
}

break_datafile_offline() {
  pdb_sql "alter database datafile '$DATAFILE' offline;
select count(*) from chaos.fill;"
}
fix_datafile_offline() {
  pdb_sql "recover datafile '$DATAFILE';
alter database datafile '$DATAFILE' online;"
}

break_dg_defer() {
  sql "alter system set log_archive_dest_state_2=defer scope=memory;
alter system switch logfile;
alter system switch logfile;"
}
fix_dg_defer() {
  sql "alter system set log_archive_dest_state_2=enable scope=memory;
alter system switch logfile;"
}

break_deadlock() {
  local a b
  for pair in "1 2" "2 1"; do
    set -- $pair
    pdb_sql "update chaos.locks set v=v+1 where id=$1;
exec dbms_session.sleep(4);
update chaos.locks set v=v+1 where id=$2;
rollback;" >/dev/null &
    sleep 1
  done
  wait
}
fix_deadlock() { :; }

break_checkpoint_storm() {
  local i stmts=""
  for i in $(seq 1 12); do stmts+="alter system switch logfile;
"; done
  sql "$stmts"
}
fix_checkpoint_storm() { :; }

break_ora600_note() {
  sql "exec sys.dbms_system.ksdwrt(2, 'Errors in file /u01/app/oracle/diag/rdbms/cdb1/cdb1/trace/cdb1_ora_chaos.trc:')
exec sys.dbms_system.ksdwrt(2, 'ORA-00600: internal error code, arguments: [chaos-synthetic], [], [], [], [], [], [], [], [], [], [], []')"
}
fix_ora600_note() { :; }

scenario_row() { echo "$SCENARIOS" | awk -v n="$1" '$1==n'; }

pick_scenario() {
  echo "$SCENARIOS" | awk 'NF==4 {for(i=0;i<$4;i++) print $1}' | shuf -n1
}

active() { [[ -f "$STATE" ]] && grep -q '^scenario=' "$STATE"; }

start() {
  local name=$1 row lo hi minutes due
  row=$(scenario_row "$name")
  [[ -n "$row" ]] || { log "unknown scenario $name"; return 1; }
  set -- $row; lo=$2; hi=$3
  minutes=$(( lo == hi ? lo : lo + RANDOM % (hi - lo + 1) ))
  due=$(( $(date +%s) + minutes * 60 ))
  printf 'scenario=%s\nstarted=%s\ndue=%s\n' "$name" "$(date +%s)" "$due" >"$STATE"
  ensure_fixtures
  log "break $name (repair in ${minutes}m)"
  "break_$name" 2>&1 | sed 's/^/  /' | tee -a "$LOG"
}

repair() {
  local name
  name=$(grep '^scenario=' "$STATE" 2>/dev/null | cut -d= -f2)
  [[ -n "$name" ]] || { log "nothing active"; return 0; }
  log "fix $name"
  "fix_$name" 2>&1 | sed 's/^/  /' | tee -a "$LOG"
  printf 'idle_until=%s\n' "$(( $(date +%s) + COOLDOWN_MIN * 60 ))" >"$STATE"
}

tick() {
  local now due idle_until
  now=$(date +%s)
  if active; then
    due=$(grep '^due=' "$STATE" | cut -d= -f2)
    (( now >= due )) && repair
    return
  fi
  idle_until=$(grep '^idle_until=' "$STATE" 2>/dev/null | cut -d= -f2)
  (( now < ${idle_until:-0} )) && return
  (( RANDOM % ODDS == 0 )) || return
  start "$(pick_scenario)"
}

case ${1:-tick} in
  tick)   tick ;;
  break)  active && { log "already active: $(grep '^scenario=' "$STATE")"; exit 1; }; start "${2:?scenario name}" ;;
  fix)    repair ;;
  status) cat "$STATE" 2>/dev/null || echo idle; tail -5 "$LOG" 2>/dev/null ;;
  list)   echo "$SCENARIOS" | awk 'NF==4 {printf "%-18s %2s-%2s min  weight %s\n",$1,$2,$3,$4}' ;;
  *) echo "usage: $0 tick|break <name>|fix|status|list" >&2; exit 2 ;;
esac
