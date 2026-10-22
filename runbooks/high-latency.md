# Runbook : HighLatencyP95

**Alerte** : latence p95 de `orders-api` > 500 ms pendant 1 min
**Sévérité** : warning → canal équipe
**Impact** : service lent, risque de timeouts côté clients

## 1. Qualifier
- Grafana : p50 / p95 / p99. Si seul le p99 monte → quelques requêtes lentes ; si le p50 monte → tout le service est lent
- Le trafic (req/s) a-t-il augmenté ? Une hausse de charge explique souvent une hausse de latence

## 2. Diagnostiquer
```bash
./scripts/diag.sh orders-api
docker stats --no-stream orders-api
```
- CPU du conteneur saturé ? mémoire proche de la limite (512 Mo) ?
- Kibana : `service : "orders-api" and duration_ms > 500` → quels chemins sont lents
- Tester soi-même : `curl -w '%{time_total}\n' -o /dev/null -s localhost:18000/api/orders`

## 3. Corriger / contourner
- Saturation ressources → redémarrer, puis demander une analyse de capacité
- Cause applicative (ici le mode chaos `latency`) → `./scripts/chaos.sh reset`
- Si la latence se transforme en erreurs 5xx → passer au runbook `high-error-rate.md`
