# INC-EXEMPLE-001 : Échec du job TRANSFORM_ORDERS, chaîne nightly_orders bloquée

> Exemple de rapport rédigé à partir du scénario `./scripts/chaos.sh batch-fail`.
> Rejoue le scénario et remplace les horaires par les tiens.

| Champ | Valeur |
|---|---|
| Sévérité | P2 (données de reporting en retard, pas d'impact client direct) |
| Début (détection) | 02:14 |
| Fin (rétablissement) | 02:31 |
| Durée d'impact | 17 min de retard sur l'alimentation du DWH |
| Détecté par | alertes `BatchJobFailed` (TRANSFORM_ORDERS) puis `BatchChainBlocked` (LOAD_DWH) |
| Escaladé à | Non (résolu en N1 avec le runbook) |

## Impact
Le chargement du datawarehouse n'a pas eu lieu sur un cycle. Aucune donnée perdue, chargement rattrapé au cycle suivant.

## Chronologie
| Heure | Événement |
|---|---|
| 02:14 | Alerte critique `BatchJobFailed` reçue par l'astreinte (Alertmanager → incident-receiver) |
| 02:16 | Grafana : TRANSFORM_ORDERS = KO, LOAD_DWH = BLOQUÉ, EXTRACT_ORDERS = OK |
| 02:18 | Kibana (filtre `run_id`) : 2 tentatives en `EXIT_CODE_2`, relance automatique déjà effectuée |
| 02:22 | Cause identifiée : indicateur d'échec présent dans `/control` (équivalent d'un fichier d'entrée manquant) |
| 02:25 | Correction (`chaos.sh reset`) et relance de la chaîne |
| 02:31 | Les 3 jobs OK, alertes résolues |

## Diagnostic
La relance automatique avait échoué avec le même code retour : la cause était donc persistante et non transitoire. Le job amont étant OK, le problème venait de l'entrée propre à TRANSFORM_ORDERS. Le filtre Kibana par `run_id` a permis de suivre toute l'exécution en une seule vue.

## Cause racine
Condition d'entrée invalide pour TRANSFORM_ORDERS (simulée par le fichier `fail_TRANSFORM_ORDERS`).

## Actions
| Action | Type | Responsable | Statut |
|---|---|---|---|
| Ajouter un contrôle de présence du fichier d'entrée avant le lancement du job | préventif | équipe batch | à faire |
| Alerter dès la 1re tentative en échec (aujourd'hui seulement après la relance) | préventif | exploitation | à faire |

## Ce qu'on retient
Ne pas relancer en boucle un job qui échoue avec le même code : lire l'erreur d'abord. Ne jamais forcer LOAD_DWH à OK pour débloquer la chaîne.
