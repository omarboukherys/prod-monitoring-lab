#!/usr/bin/env bash
# Check-up rapide de premier niveau (N1) : à lancer dès la réception d'une alerte.
# Usage : ./scripts/diag.sh [nom_du_conteneur]
set -uo pipefail

CONTAINER="${1:-orders-api}"
API="${API:-http://localhost:8000}"
hr() { printf '\n==== %s ====\n' "$1"; }

hr "Hôte : charge, mémoire, disque"
uptime
free -h
df -h -x tmpfs -x overlay 2>/dev/null | head -n 15

hr "Top 5 processus CPU"
ps -eo pid,user,%cpu,%mem,etime,comm --sort=-%cpu | head -n 6

hr "Conteneurs (statut / redémarrages)"
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.RunningFor}}'

hr "Consommation des conteneurs"
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}'

hr "Healthcheck HTTP de $API"
curl -s -o /dev/null -w 'code=%{http_code} temps=%{time_total}s\n' --max-time 5 "$API/health" \
  || echo "INJOIGNABLE"

hr "Dernières erreurs de $CONTAINER (10 min)"
docker logs --since 10m "$CONTAINER" 2>&1 | grep -E '"level": "(ERROR|CRITICAL)"' | tail -n 10 \
  || echo "aucune"

hr "Erreurs par type (10 min)"
docker logs --since 10m "$CONTAINER" 2>&1 | grep -o '"error": "[A-Z_0-9]*"' | sort | uniq -c | sort -rn

hr "Batch : derniers événements"
docker logs --tail 20 batch-scheduler 2>&1 | grep -E 'ERROR|CRITICAL|WARNING|Fin chaîne' | tail -n 8

hr "Alertes actives (Prometheus)"
curl -s "http://localhost:9090/api/v1/alerts" \
  | python3 -c 'import json,sys
for a in json.load(sys.stdin)["data"]["alerts"]:
    if a["state"]=="firing": print(f"- [{a[\"labels\"][\"severity\"]}] {a[\"labels\"][\"alertname\"]} : {a[\"annotations\"][\"summary\"]}")' \
  2>/dev/null || echo "Prometheus injoignable"
