# Runbook : ServiceDown

**Alerte** : Prometheus n'arrive plus à collecter `orders-api` depuis 30 s
**Sévérité** : critical → astreinte
**Impact** : service indisponible

## 1. Qualifier
- `curl -s localhost:8000/health` répond-il ? (vérifie que ce n'est pas seulement la collecte qui est cassée)
- Les autres cibles Prometheus sont-elles UP ? (http://localhost:9090/targets) Si tout est DOWN → problème réseau/hôte

## 2. Diagnostiquer
```bash
docker ps -a --filter name=orders-api      # Exited ? Restarting ?
docker inspect orders-api --format '{{.State.Status}} exit={{.State.ExitCode}} OOM={{.State.OOMKilled}}'
docker logs --tail 50 orders-api
```
- `OOMKilled=true` → le conteneur a dépassé sa limite mémoire (voir `memory.md`)
- exit code 137 sans OOM → arrêt forcé (kill / docker stop)
- exit code 1 → crash applicatif : lire la stack trace dans les logs

## 3. Rétablir
```bash
docker start orders-api      # ou : docker compose up -d app
```
- Vérifier `/health`, puis que le dashboard repasse UP
- Si le service retombe en boucle → ne pas relancer à l'infini, **escalader N2** avec les logs

## 4. Clôturer
Rapport d'incident avec la durée exacte d'indisponibilité (début = première alerte, fin = retour UP).
