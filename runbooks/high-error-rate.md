# Runbook : HighErrorRate

**Alerte** : taux de réponses 5xx de `orders-api` > 5 % pendant 1 min
**Sévérité** : critical → astreinte
**Impact** : des utilisateurs reçoivent des erreurs sur la prise de commande

## 1. Qualifier (≤ 5 min)
- Grafana > *Production – Vue d'ensemble* : quel taux d'erreurs, depuis quand, sur quels codes (500, 503) ?
- Est-ce que la latence monte aussi ? (erreurs + latence = souvent une dépendance lente)
- Un déploiement ou un changement a-t-il eu lieu juste avant ?

## 2. Diagnostiquer
```bash
./scripts/diag.sh orders-api
```
- Kibana > Discover, data view *Logs production* :
  `service : "orders-api" and level : "ERROR"` → regarder le champ `error`
- `DB_TIMEOUT` → problème côté base de données ; `WRITE_FAILED` → écriture refusée
- Comparer avec `status >= 500` sur la même période pour voir quels endpoints sont touchés

## 3. Corriger / contourner
- Cause applicative identifiée et connue → appliquer la procédure (ici : `./scripts/chaos.sh reset`)
- Redémarrage du service si état dégradé : `docker restart orders-api`
- Cause inconnue ou persistante après 15 min → **escalade N2** (équipe applicative) avec les éléments collectés

## 4. Clôturer
- Vérifier le retour à la normale sur Grafana (taux < 1 % pendant 5 min)
- Rédiger le rapport d'incident : `incidents/TEMPLATE.md`
