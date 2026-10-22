# Runbook : BatchJobFailed / BatchChainBlocked / BatchNotRunRecently

**Chaîne** : `nightly_orders` = EXTRACT_ORDERS → TRANSFORM_ORDERS → LOAD_DWH
**Sévérité** : job KO = critical ; job bloqué ou en retard = warning
**Impact** : les données du datawarehouse ne sont pas à jour (reporting du matin faux)

## 1. Qualifier
- Grafana > *Chaîne batch* : quel job est KO, lesquels sont bloqués derrière ?
- Le job a-t-il déjà été relancé automatiquement ? (logs `attempt: 2`)
- Quelle est l'heure limite de mise à disposition des données ? → urgence réelle

## 2. Diagnostiquer
```bash
docker logs --since 15m batch-scheduler | grep -E 'ERROR|CRITICAL'
```
Kibana : `service : "batch-scheduler" and run_id : "<id>"` pour suivre une exécution de bout en bout.

| Erreur | Signification | Action |
|---|---|---|
| `EXIT_CODE_2` | fichier source absent / entrée invalide | vérifier la disponibilité du fichier amont, contacter le fournisseur |
| `SLA_EXCEEDED` | job tué car trop long | regarder la volumétrie et la charge de l'hôte |
| `CHAIN_BLOCKED` | conséquence d'un job KO | traiter le job KO, ne pas forcer les suivants |

## 3. Corriger et relancer
1. Corriger la cause (ici : `./scripts/chaos.sh reset` supprime le flag d'échec)
2. Relancer la chaîne à partir du job en échec — dans le lab : `docker restart batch-scheduler`
3. Vérifier que LOAD_DWH passe OK et que le statut redevient vert
4. **Ne jamais** forcer un job aval à OK sans validation : les données seraient incohérentes

## 4. Escalade
- Toujours KO après une relance manuelle → N2 applicatif
- Heure limite dépassée → prévenir les utilisateurs métier du retard
