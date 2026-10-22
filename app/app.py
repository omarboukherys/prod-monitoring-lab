"""
Service "orders-api" : petite application simulant un service en production.

- Expose des métriques Prometheus sur /metrics
- Écrit des logs JSON (stdout + /var/log/app/app.log) collectés par Filebeat -> Elasticsearch -> Kibana
- Endpoints /chaos/* pour injecter des incidents (latence, erreurs, fuite mémoire)
"""
import json
import logging
import os
import random
import time
import uuid

from flask import Flask, g, jsonify, request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

app = Flask(__name__)

# ---------------------------------------------------------------- logs JSON
LOG_DIR = os.environ.get("LOG_DIR", "/var/log/app")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "@timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": "orders-api",
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "extra_fields", {}))
        return json.dumps(payload, ensure_ascii=False)


logger = logging.getLogger("orders-api")
logger.setLevel(logging.INFO)
_stream = logging.StreamHandler()
_stream.setFormatter(JsonFormatter())
logger.addHandler(_stream)
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    _file = logging.FileHandler(os.path.join(LOG_DIR, "app.log"))
    _file.setFormatter(JsonFormatter())
    logger.addHandler(_file)
except OSError:
    pass  # en local sans droits sur /var/log : stdout uniquement


def log(level, message, **fields):
    logger.log(level, message, extra={"extra_fields": fields})


# ---------------------------------------------------------------- métriques
REQUESTS = Counter(
    "http_requests_total", "Nombre de requêtes HTTP", ["method", "endpoint", "status"]
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "Durée des requêtes HTTP",
    ["endpoint"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
ORDERS = Counter("orders_created_total", "Commandes créées")
CHAOS = Gauge("chaos_mode_active", "Mode chaos actif (1 = oui)", ["kind"])
LEAK_MB = Gauge("app_leaked_memory_mb", "Mémoire retenue par la fuite simulée (Mo)")

# ---------------------------------------------------------------- état chaos
chaos = {"latency": 0.0, "error_rate": 0.0}
_leak = []


@app.before_request
def _start():
    g.start = time.time()
    g.request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    if request.path.startswith("/api") and chaos["latency"]:
        time.sleep(random.uniform(0.5, 1.0) * chaos["latency"])


@app.after_request
def _end(response):
    if request.path == "/metrics":
        return response
    duration = time.time() - g.start
    endpoint = request.url_rule.rule if request.url_rule else "unknown"
    REQUESTS.labels(request.method, endpoint, response.status_code).inc()
    LATENCY.labels(endpoint).observe(duration)
    level = logging.ERROR if response.status_code >= 500 else logging.INFO
    log(
        level,
        f"{request.method} {request.path} -> {response.status_code}",
        request_id=g.request_id,
        method=request.method,
        path=request.path,
        status=response.status_code,
        duration_ms=round(duration * 1000, 1),
    )
    return response


# ---------------------------------------------------------------- endpoints métier
@app.get("/health")
def health():
    return jsonify(status="UP")


@app.get("/api/orders")
def list_orders():
    if random.random() < chaos["error_rate"]:
        log(logging.ERROR, "Timeout base de données", request_id=g.request_id, error="DB_TIMEOUT")
        return jsonify(error="database timeout"), 503
    time.sleep(random.uniform(0.01, 0.08))
    return jsonify(orders=[{"id": i, "amount": random.randint(50, 900)} for i in range(5)])


@app.post("/api/orders")
def create_order():
    if random.random() < chaos["error_rate"]:
        log(logging.ERROR, "Échec écriture commande", request_id=g.request_id, error="WRITE_FAILED")
        return jsonify(error="write failed"), 500
    ORDERS.inc()
    return jsonify(id=str(uuid.uuid4())[:8], status="CREATED"), 201


# ---------------------------------------------------------------- injection d'incidents
@app.post("/chaos/latency/<float:factor>")
def chaos_latency(factor):
    chaos["latency"] = factor
    CHAOS.labels("latency").set(1 if factor else 0)
    log(logging.WARNING, f"CHAOS latence={factor}")
    return jsonify(chaos)


@app.post("/chaos/errors/<float:rate>")
def chaos_errors(rate):
    chaos["error_rate"] = max(0.0, min(rate, 1.0))
    CHAOS.labels("errors").set(1 if rate else 0)
    log(logging.WARNING, f"CHAOS taux_erreur={chaos['error_rate']}")
    return jsonify(chaos)


@app.post("/chaos/memory/<int:mb>")
def chaos_memory(mb):
    _leak.extend(bytearray(1024 * 1024) for _ in range(mb))
    LEAK_MB.set(len(_leak))
    CHAOS.labels("memory").set(1)
    log(logging.WARNING, f"CHAOS fuite mémoire +{mb} Mo (total {len(_leak)} Mo)")
    return jsonify(leaked_mb=len(_leak))


@app.post("/chaos/reset")
def chaos_reset():
    chaos.update(latency=0.0, error_rate=0.0)
    _leak.clear()
    LEAK_MB.set(0)
    for kind in ("latency", "errors", "memory"):
        CHAOS.labels(kind).set(0)
    log(logging.INFO, "CHAOS reset : retour à la normale")
    return jsonify(chaos)


@app.get("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
