#!/usr/bin/env bash
# One-shot, idempotent import of the dbwiki workflows into the local n8n
# (~/n8n/compose.yml). Safe to re-run: fixed ids overwrite in place. Nothing
# in the crontab is touched and every workflow is imported INACTIVE.
#
#   1. ssh key      ~/.ssh/n8n_dbwiki (+ authorized_keys line) — how the
#                   n8n container reaches the host to run the crontab lines
#   2. n8n up       docker compose up -d for the n8n service (and its deps)
#   3. credential   sshPrivateKey "dbwiki host (ssh)"  -> n8n import:credentials
#   4. workflows    build_workflows.py -> workflows/*.json -> n8n import:workflow
#
#   ./setup.sh              dry-mode workflows (default, side-effect-free)
#   ./setup.sh --live       real crontab command lines (still inactive!)
#   ./setup.sh --test       after import, run the watchdog workflow once
#
# Env overrides: N8N_DIR (~/n8n), N8N_SSH_HOST (host address as seen from the
# container; default = the LAN address in ~/n8n/Caddyfile), N8N_SSH_USER ($USER).
set -euo pipefail
cd "$(dirname "$0")"

N8N_DIR="${N8N_DIR:-$HOME/n8n}"
N8N_SSH_USER="${N8N_SSH_USER:-$USER}"
KEY="$HOME/.ssh/n8n_dbwiki"
MARK="n8n-dbwiki"
LIVE=""; TEST=""
for a in "$@"; do
  case "$a" in
    --live) LIVE="--live" ;;
    --test) TEST=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

if [[ -z "${N8N_SSH_HOST:-}" ]]; then
  # first IPv4 literal in the Caddyfile is the host's LAN address
  N8N_SSH_HOST="$(grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' "$N8N_DIR/Caddyfile" | head -1 || true)"
  [[ -n "$N8N_SSH_HOST" ]] || { echo "set N8N_SSH_HOST (host address reachable from the n8n container)"; exit 1; }
fi

compose() { docker compose -f "$N8N_DIR/compose.yml" "$@"; }
n8n_cli() { compose exec -T n8n n8n "$@"; }

echo "== 1. ssh key for the n8n container ($KEY)"
if [[ ! -f "$KEY" ]]; then
  ssh-keygen -q -t ed25519 -N "" -C "$MARK" -f "$KEY"
  echo "   generated"
fi
touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys
if ! grep -qF "$MARK" ~/.ssh/authorized_keys; then
  cat "$KEY.pub" >> ~/.ssh/authorized_keys
  echo "   authorized_keys: added (remove the line tagged '$MARK' to revoke)"
else
  echo "   authorized_keys: present"
fi

echo "== 2. n8n stack ($N8N_DIR)"
compose up -d n8n runners >/dev/null
for i in $(seq 1 60); do
  n8n_cli --version >/dev/null 2>&1 && break
  [[ $i == 60 ]] && { echo "n8n container did not come up"; exit 1; }
  sleep 2
done
echo "   n8n $(n8n_cli --version 2>/dev/null | tail -1) up; editor http://127.0.0.1:5678/"

echo "== 3. credential '$MARK' -> $N8N_SSH_USER@$N8N_SSH_HOST"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
python3 - "$KEY" "$N8N_SSH_HOST" "$N8N_SSH_USER" > "$TMP/creds.json" <<'EOF'
import json, sys
key, host, user = sys.argv[1:4]
print(json.dumps([{
    "id": "dbwiki-host-ssh",
    "name": "dbwiki host (ssh)",
    "type": "sshPrivateKey",
    "data": {"host": host, "port": 22, "username": user,
             "privateKey": open(key).read(), "passphrase": ""},
}]))
EOF
CID="$(compose ps -q n8n)"
docker cp "$TMP/creds.json" "$CID:/tmp/dbwiki-creds.json"
n8n_cli import:credentials --input=/tmp/dbwiki-creds.json | tail -1
docker exec "$CID" rm -f /tmp/dbwiki-creds.json

echo "== 4. workflows (${LIVE:-dry})"
python3 build_workflows.py $LIVE
docker exec "$CID" rm -rf /tmp/dbwiki-workflows
docker cp workflows "$CID:/tmp/dbwiki-workflows"
n8n_cli import:workflow --separate --input=/tmp/dbwiki-workflows/ | tail -1
docker exec "$CID" rm -rf /tmp/dbwiki-workflows
echo "   imported $(ls workflows/*.json | wc -l) workflows, all inactive"

if [[ -n "$TEST" ]]; then
  echo "== test: execute dbwiki-watchdog once (ssh container -> host -> watchdog.py)"
  n8n_cli execute --id dbwiki-watchdog --rawOutput 2>&1 | tail -40 || true
fi

cat <<EOF

Done. Nothing in the crontab changed; every workflow is inactive.
  editor:      http://127.0.0.1:5678/  (tag: dbwiki)
  activate:    the editor toggle, or:  docker compose -f $N8N_DIR/compose.yml exec n8n n8n update:workflow --id dbwiki-watchdog --active=true
  events:      .state/n8n/events.jsonl  (written by 'dbwiki errors')
  revoke ssh:  sed -i '/$MARK/d' ~/.ssh/authorized_keys && rm -f $KEY $KEY.pub
EOF
