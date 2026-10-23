"""
Serveur MCP "prod-monitoring" : expose la production du lab à n'importe quel client MCP
(Claude Desktop, MCP Inspector, agent Telegram du lab...).

Tous les outils sont en LECTURE SEULE : aucun outil ne peut redémarrer, modifier ou supprimer
quoi que ce soit. Une action sur la prod reste décidée et exécutée par un humain.

Transports :
    python server.py          -> Streamable HTTP sur 0.0.0.0:8765/mcp (utilisé par l'agent Telegram)
    python server.py stdio    -> stdio (Claude Desktop via `docker exec -i lab-mcp-server ...`)
"""
import json
import os
import re
import sys
import time

import requests
from mcp.server.fastmcp import FastMCP

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
ELASTIC = os.environ.get("ELASTIC_URL", "http://elasticsearch:9200")
RUNBOOK_DIR = os.environ.get("RUNBOOK_DIR", "/runbooks")
INDEX = "prod-logs-*"

mcp = FastMCP(
    "prod-monitoring",
    instructions=(
        "Outils de supervision en lecture seule pour la production simulée du lab : service orders-api, "
        "chaîne batch nightly_orders (EXTRACT_ORDERS -> TRANSFORM_ORDERS -> LOAD_DWH), alertes Prometheus, "
        "logs Elasticsearch et runbooks. Commencer par get_prod_status et list_active_alerts, "
        "puis creuser avec les logs et les métriques. Toujours citer les chiffres obtenus."
    ),
    host="0.0.0.0",
    port=int(os.environ.get("MCP_PORT", "8765")),
)


# ------------------------------------------------------------------ helpers
def _prom(query):
    r = requests.get(f"{PROMETHEUS}/api/v1/query", params={"query": query}, timeout=10)
    try:
        body = r.json()
    except ValueError:
        r.raise_for_status()
        raise
    if body.get("status") != "success":
        raise ValueError(f"PromQL refusée par Prometheus : {body.get('error', r.status_code)}")
    return body["data"]["result"]


def _prom_value(query):
    try:
        res = _prom(query)
        return round(float(res[0]["value"][1]), 4) if res else None
    except Exception:
        return None


def _es_search(body):
    r = requests.post(f"{ELASTIC}/{INDEX}/_search", json=body, timeout=10)
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------------ outils
@mcp.tool()
def get_prod_status() -> dict:
    """État global de la production : disponibilité, trafic, taux d'erreurs, latence p95,
    mémoire anormale, statut des jobs batch et nombre d'alertes actives."""
    batch = {}
    for r in _safe(lambda: _prom("batch_job_last_status"), []):
        batch[r["metric"].get("job", "?")] = {"1": "OK", "0": "KO", "-1": "BLOQUÉ"}.get(r["value"][1], r["value"][1])
    return {
        "orders_api_up": _prom_value('up{job="orders-api"}'),
        "requests_per_second": _prom_value('sum(rate(http_requests_total{job="orders-api"}[2m]))'),
        "error_rate_5xx": _prom_value(
            '(sum(rate(http_requests_total{job="orders-api",status=~"5.."}[2m])) or vector(0))'
            ' / sum(rate(http_requests_total{job="orders-api"}[2m]))'),
        "latency_p95_seconds": _prom_value(
            'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="orders-api"}[2m])))'),
        "leaked_memory_mb": _prom_value("app_leaked_memory_mb"),
        "batch_jobs": batch,
        "active_alerts": _prom_value('count(ALERTS{alertstate="firing"}) or vector(0)'),
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


@mcp.tool()
def list_active_alerts() -> dict:
    """Alertes Prometheus en cours (firing ou pending) avec sévérité, résumé, runbook et date de début."""
    r = requests.get(f"{PROMETHEUS}/api/v1/alerts", timeout=10)
    r.raise_for_status()
    alerts = [
        {
            "alertname": a["labels"].get("alertname"),
            "state": a["state"],
            "severity": a["labels"].get("severity"),
            "labels": {k: v for k, v in a["labels"].items() if k not in ("alertname", "severity")},
            "summary": a["annotations"].get("summary"),
            "runbook": a["annotations"].get("runbook"),
            "active_since": a.get("activeAt"),
        }
        for a in r.json()["data"]["alerts"]
    ]
    return {"count": len(alerts), "alerts": alerts}


@mcp.tool()
def query_metrics(promql: str) -> dict:
    """Exécute une requête PromQL instantanée (lecture seule) et renvoie au plus 30 séries.
    Métriques utiles : http_requests_total{status}, http_request_duration_seconds_bucket,
    app_leaked_memory_mb, chaos_mode_active{kind}, batch_job_last_status{job},
    batch_job_last_duration_seconds{job}, batch_job_last_success_timestamp{job}, up{job},
    node_cpu_seconds_total, node_memory_MemAvailable_bytes."""
    res = _prom(promql)
    return {
        "query": promql,
        "series": [{"labels": r["metric"], "value": r["value"][1]} for r in res[:30]],
        "truncated": len(res) > 30,
    }


@mcp.tool()
def search_logs(
    service: str = "",
    level: str = "",
    text: str = "",
    minutes: int = 15,
    limit: int = 20,
) -> dict:
    """Cherche dans les logs centralisés (Elasticsearch), du plus récent au plus ancien.
    service : orders-api, batch-scheduler ou incident-receiver (vide = tous).
    level : INFO, WARNING, ERROR ou CRITICAL (vide = tous).
    text : mots à chercher dans message, error, path, job, request_id ou run_id.
    minutes : fenêtre de temps (max 1440). limit : nombre de lignes (max 50)."""
    minutes, limit = max(1, min(int(minutes), 1440)), max(1, min(int(limit), 50))
    filters = [{"range": {"@timestamp": {"gte": f"now-{minutes}m"}}}]
    if service:
        filters.append({"term": {"service.keyword": service}})
    if level:
        filters.append({"term": {"level.keyword": level.upper()}})
    must = []
    if text:
        must.append({"simple_query_string": {
            "query": text, "default_operator": "and",
            "fields": ["message", "error", "path", "job", "request_id", "run_id"],
        }})
    body = {
        "size": limit,
        "sort": [{"@timestamp": "desc"}],
        "track_total_hits": True,
        "_source": ["@timestamp", "service", "level", "message", "error", "request_id",
                    "path", "status", "duration_ms", "job", "run_id", "alertname"],
        "query": {"bool": {"filter": filters, "must": must}},
    }
    res = _es_search(body)
    return {
        "total_matches": res["hits"]["total"]["value"],
        "returned": len(res["hits"]["hits"]),
        "logs": [h["_source"] for h in res["hits"]["hits"]],
    }


@mcp.tool()
def count_errors_by_type(minutes: int = 15) -> dict:
    """Répartition des erreurs (niveau ERROR/CRITICAL) par service et par code d'erreur
    sur la fenêtre donnée, avec l'horodatage de la première et de la dernière occurrence."""
    minutes = max(1, min(int(minutes), 1440))
    body = {
        "size": 0,
        "query": {"bool": {"filter": [
            {"range": {"@timestamp": {"gte": f"now-{minutes}m"}}},
            {"terms": {"level.keyword": ["ERROR", "CRITICAL"]}},
        ]}},
        "aggs": {"by_service": {"terms": {"field": "service.keyword", "size": 10}, "aggs": {
            "by_error": {"terms": {"field": "error.keyword", "size": 10, "missing": "(sans code)"}, "aggs": {
                "first": {"min": {"field": "@timestamp"}},
                "last": {"max": {"field": "@timestamp"}},
            }},
        }}},
    }
    res = _es_search(body)
    return {
        "window_minutes": minutes,
        "services": {
            s["key"]: {
                e["key"]: {"count": e["doc_count"],
                           "first_seen": e["first"].get("value_as_string"),
                           "last_seen": e["last"].get("value_as_string")}
                for e in s["by_error"]["buckets"]
            }
            for s in res.get("aggregations", {}).get("by_service", {}).get("buckets", [])
        },
    }


@mcp.tool()
def list_runbooks() -> dict:
    """Liste des runbooks disponibles (procédures de traitement d'incident)."""
    return {"runbooks": sorted(f for f in os.listdir(RUNBOOK_DIR) if f.endswith(".md"))}


@mcp.tool()
def get_runbook(name: str) -> str:
    """Contenu d'un runbook, par exemple 'high-error-rate.md' ou 'runbooks/batch-failed.md'."""
    filename = os.path.basename(name)
    if not re.fullmatch(r"[a-z0-9-]+\.md", filename):
        raise ValueError("nom de runbook invalide, utiliser list_runbooks")
    path = os.path.join(RUNBOOK_DIR, filename)
    if not os.path.isfile(path):
        raise ValueError(f"runbook introuvable : {filename}. Disponibles : {list_runbooks()['runbooks']}")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
