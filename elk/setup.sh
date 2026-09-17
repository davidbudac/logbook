#!/usr/bin/env bash
# One-shot, idempotent setup of the ES/Kibana side of the dbwiki telemetry
# pipeline. Safe to re-run any time (everything is PUT/overwrite).
#
#   1. ingest pipelines   elasticsearch/pipelines/*.json  -> _ingest/pipeline/<name>
#   2. index template     elasticsearch/index-template.json -> _index_template/dbwiki-telemetry
#   3. kibana objects     kibana/build_dashboards.py -> generated ndjson -> _import
#   4. first run of scripts/emit_derived.py (creates .state/elk/*.jsonl)
#
# Run this BEFORE `docker compose up -d`, otherwise filebeat's first docs are
# indexed without the template/pipelines. Credentials come from ./.env
# (copy .env.example).
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a; source ./.env; set +a
fi
ES_HOST="${ES_HOST:-http://localhost:9200}"
KIBANA_HOST="${KIBANA_HOST:-http://localhost:5601}"
ES_USER="${ES_USER:-elastic}"
ES_PASSWORD="${ES_PASSWORD:-changeme}"

es() { curl -fsS -u "$ES_USER:$ES_PASSWORD" "$@"; }

echo "== waiting for Elasticsearch at $ES_HOST"
for i in $(seq 1 30); do
  es "$ES_HOST/_cluster/health" >/dev/null 2>&1 && break
  [[ $i == 30 ]] && { echo "ES not reachable"; exit 1; }
  sleep 2
done

echo "== installing ingest pipelines"
for f in elasticsearch/pipelines/*.json; do
  name="$(basename "$f" .json)"
  # dbwiki-router references the others; install it last.
  [[ "$name" == "dbwiki-router" ]] && continue
  es -XPUT "$ES_HOST/_ingest/pipeline/$name" \
     -H 'Content-Type: application/json' --data-binary "@$f" >/dev/null
  echo "   $name"
done
es -XPUT "$ES_HOST/_ingest/pipeline/dbwiki-router" \
   -H 'Content-Type: application/json' \
   --data-binary @elasticsearch/pipelines/dbwiki-router.json >/dev/null
echo "   dbwiki-router"

echo "== installing index template dbwiki-telemetry"
es -XPUT "$ES_HOST/_index_template/dbwiki-telemetry" \
   -H 'Content-Type: application/json' \
   --data-binary @elasticsearch/index-template.json >/dev/null

echo "== generating + importing Kibana saved objects"
python3 kibana/build_dashboards.py > kibana/dbwiki-dashboards.json
for i in $(seq 1 30); do
  curl -fsS -o /dev/null "$KIBANA_HOST/api/status" 2>/dev/null && break
  [[ $i == 30 ]] && { echo "Kibana not reachable"; exit 1; }
  sleep 2
done
# _bulk_create, not _import: import runs legacy migrations on unstamped
# objects and 500s on modern Lens state; bulk_create stores them as-is.
import_out="$(curl -fsS -u "$ES_USER:$ES_PASSWORD" \
  -XPOST "$KIBANA_HOST/api/saved_objects/_bulk_create?overwrite=true" \
  -H 'kbn-xsrf: true' -H 'Content-Type: application/json' \
  --data-binary @kibana/dbwiki-dashboards.json)"
echo "$import_out" | python3 -c '
import json, sys
r = json.load(sys.stdin)
objs = r.get("saved_objects", [])
errs = [o for o in objs if o.get("error")]
if errs:
    print(json.dumps(errs, indent=2)); sys.exit(1)
print("   created/updated %d saved objects" % len(objs))
'

echo "== first run of emit_derived.py"
python3 scripts/emit_derived.py || true

cat <<EOF

Done. Next steps (see README.md):
  docker compose up -d          # start the filebeat shipper
  crontab: add the emit_derived.py entry from README.md
Dashboard: $KIBANA_HOST/app/dashboards#/view/dbwiki-pipeline
EOF
