# Runbook : MemoryLeakSuspected

**Alerte** : plus de 200 Mo retenus par l'application, en hausse continue
**Sévérité** : warning (devient critical si le conteneur est tué par l'OOM killer)

## 1. Qualifier
- Grafana > *Mémoire retenue* : la courbe monte-t-elle en escalier sans jamais redescendre ? → fuite probable
- À quel rythme ? Estimer le temps avant d'atteindre la limite de 512 Mo

## 2. Diagnostiquer
```bash
docker stats --no-stream orders-api
docker inspect orders-api --format 'OOMKilled={{.State.OOMKilled}} restarts={{.RestartCount}}'
dmesg -T | grep -i -E 'oom|killed process' | tail      # sur un vrai serveur Linux
```
- Kibana : rechercher `CHAOS fuite mémoire` ou un endpoint appelé en boucle au moment où ça monte

## 3. Contourner
- Redémarrage contrôlé **avant** l'OOM (en heure creuse si possible) : `docker restart orders-api`
- Ouvrir un ticket N2 / développement : un redémarrage cache la fuite, il ne la corrige pas
