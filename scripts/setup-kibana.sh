#!/usr/bin/env bash
# Crée la data view "prod-logs-*" dans Kibana (à lancer une fois que Kibana répond).
set -euo pipefail
KIBANA="${KIBANA:-http://localhost:5601}"

until curl -s "$KIBANA/api/status" | grep -q '"level":"available"'; do
  echo "Attente de Kibana..."; sleep 5
done

curl -s -X POST "$KIBANA/api/data_views/data_view" \
  -H 'kbn-xsrf: true' -H 'Content-Type: application/json' \
  -d '{"data_view":{"title":"prod-logs-*","name":"Logs production","timeFieldName":"@timestamp"},"override":true}' \
  > /dev/null
echo "Data view 'Logs production' créée : $KIBANA/app/discover"
