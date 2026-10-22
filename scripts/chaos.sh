#!/usr/bin/env bash
# Injection d'incidents pour s'entraîner au diagnostic.
# Usage : ./scripts/chaos.sh <scenario>
set -euo pipefail

API="${API:-http://localhost:18000}"

usage() {
  cat <<EOF
Scénarios disponibles :
  errors        30 % de réponses 5xx (simule une base de données instable)
  latency       latence x3 sur l'API
  memory        fuite mémoire progressive (+50 Mo toutes les 10 s, 6 fois)
  down          arrêt brutal du service orders-api
  batch-fail    le job TRANSFORM_ORDERS sort en code retour 2
  batch-slow    le job EXTRACT_ORDERS dépasse son SLA
  disk          remplit 1 Go dans /tmp du conteneur orders-api
  reset         tout remettre à la normale
EOF
}

case "${1:-}" in
  errors)   curl -s -X POST "$API/chaos/errors/0.3"; echo ;;
  latency)  curl -s -X POST "$API/chaos/latency/3.0"; echo ;;
  memory)
    for _ in 1 2 3 4 5 6; do curl -s -X POST "$API/chaos/memory/50"; echo; sleep 10; done ;;
  down)     docker stop orders-api ;;
  batch-fail) docker exec batch-scheduler touch /control/fail_TRANSFORM_ORDERS
              echo "TRANSFORM_ORDERS échouera au prochain run de la chaîne (toutes les 2 min)" ;;
  batch-slow) docker exec batch-scheduler touch /control/slow_EXTRACT_ORDERS
              echo "EXTRACT_ORDERS dépassera son SLA au prochain run" ;;
  disk)     docker exec orders-api sh -c 'dd if=/dev/zero of=/tmp/bigfile bs=1M count=1024 status=none'
            echo "1 Go écrit dans orders-api:/tmp/bigfile" ;;
  reset)
    docker start orders-api >/dev/null 2>&1 || true
    sleep 2
    curl -s -X POST "$API/chaos/reset"; echo
    docker exec batch-scheduler sh -c 'rm -f /control/*' || true
    docker exec orders-api rm -f /tmp/bigfile || true
    echo "Retour à la normale (les alertes se résolvent en 1 à 3 min)" ;;
  *) usage; exit 1 ;;
esac
