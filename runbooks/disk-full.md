# Runbook : DiskAlmostFull

**Alerte** : moins de 10 % d'espace libre sur un point de montage
**Sévérité** : warning (critical sous 5 %)
**Impact** : à 100 %, les applications ne peuvent plus écrire (logs, fichiers, base de données)

## 1. Trouver ce qui prend la place
```bash
df -h                                   # quel point de montage
sudo du -xh --max-depth=1 / | sort -rh | head       # quel dossier
sudo find / -xdev -type f -size +500M -exec ls -lh {} \; 2>/dev/null
docker system df                        # images / volumes / logs Docker
sudo lsof +L1                           # fichiers supprimés encore ouverts (espace non libéré)
```
Dans le lab : `docker exec orders-api du -sh /tmp/*` après `./scripts/chaos.sh disk`.

## 2. Libérer sans casser
- Logs applicatifs anciens → compresser / purger selon la politique de rétention
- Fichier supprimé mais encore ouvert par un process → redémarrer le process (l'espace se libère)
- Docker : `docker image prune` (images inutilisées). **Jamais** `docker volume prune` sans vérifier : perte de données
- Ne jamais supprimer un fichier de base de données ou de journal transactionnel

## 3. Prévenir
Vérifier la rotation des logs (ici : `max-size` / `max-file` dans `docker-compose.yml`, `logrotate` sur un serveur).
