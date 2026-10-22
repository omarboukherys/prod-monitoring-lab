"""
Mini-ordonnanceur de batchs (logique type Control-M / VTOM, en simplifié).

Une "chaîne" nocturne enchaîne 3 jobs avec dépendances :
    EXTRACT_ORDERS -> TRANSFORM_ORDERS -> LOAD_DWH

Règles :
- un job ne démarre que si le précédent est OK (sinon la chaîne est bloquée)
- chaque job a une durée max (SLA) : au-delà il est tué et marqué en échec
- 1 relance automatique avant échec définitif (comme un "rerun" d'ordonnanceur)

Métriques poussées sur Prometheus Pushgateway, logs JSON lus par Filebeat.
Pour simuler un incident : créer le fichier /control/fail_<JOB> ou /control/slow_<JOB>
(voir scripts/chaos.sh).
"""
import json
import os
import random
import time
import uuid

from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

PUSHGATEWAY = os.environ.get("PUSHGATEWAY", "pushgateway:9091")
INTERVAL = int(os.environ.get("CHAIN_INTERVAL_SECONDS", "120"))
CONTROL_DIR = os.environ.get("CONTROL_DIR", "/control")
LOG_FILE = os.path.join(os.environ.get("LOG_DIR", "/var/log/batch"), "batch.log")

CHAIN = [
    {"name": "EXTRACT_ORDERS", "duration": (3, 6), "sla": 15},
    {"name": "TRANSFORM_ORDERS", "duration": (4, 8), "sla": 15},
    {"name": "LOAD_DWH", "duration": (2, 5), "sla": 15},
]

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
os.makedirs(CONTROL_DIR, exist_ok=True)


def log(level, message, **fields):
    line = json.dumps(
        {
            "@timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": level,
            "service": "batch-scheduler",
            "message": message,
            **fields,
        },
        ensure_ascii=False,
    )
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def push(job, status, duration, run_id):
    """status : 1 = OK, 0 = KO, -1 = non exécuté (chaîne bloquée)"""
    reg = CollectorRegistry()
    Gauge("batch_job_last_status", "Dernier statut (1 OK, 0 KO, -1 bloqué)", registry=reg).set(status)
    Gauge("batch_job_last_duration_seconds", "Durée du dernier run", registry=reg).set(duration)
    Gauge("batch_job_last_run_timestamp", "Horodatage du dernier run", registry=reg).set(time.time())
    if status == 1:
        Gauge("batch_job_last_success_timestamp", "Horodatage du dernier succès", registry=reg).set(time.time())
    try:
        push_to_gateway(PUSHGATEWAY, job=job, registry=reg, grouping_key={"chain": "nightly_orders"})
    except Exception as exc:  # la supervision ne doit pas casser le batch
        log("WARNING", f"Pushgateway injoignable : {exc}", job=job, run_id=run_id)


def run_job(job, run_id, attempt):
    name = job["name"]
    duration = random.uniform(*job["duration"])
    if os.path.exists(os.path.join(CONTROL_DIR, f"slow_{name}")):
        duration = job["sla"] + 5  # dépassement de SLA
    log("INFO", f"Début {name}", job=name, run_id=run_id, attempt=attempt)

    time.sleep(min(duration, job["sla"]))
    if duration > job["sla"]:
        log("ERROR", f"{name} a dépassé sa durée max ({job['sla']}s) : job tué", job=name,
            run_id=run_id, attempt=attempt, error="SLA_EXCEEDED")
        return False, job["sla"]
    if os.path.exists(os.path.join(CONTROL_DIR, f"fail_{name}")):
        log("ERROR", f"{name} en échec : code retour 2 (fichier source introuvable)", job=name,
            run_id=run_id, attempt=attempt, error="EXIT_CODE_2")
        return False, duration
    log("INFO", f"Fin OK {name}", job=name, run_id=run_id, attempt=attempt,
        duration_s=round(duration, 1))
    return True, duration


def run_chain():
    run_id = str(uuid.uuid4())[:8]
    log("INFO", "Démarrage chaîne nightly_orders", run_id=run_id)
    blocked = False
    for job in CHAIN:
        if blocked:
            log("WARNING", f"{job['name']} non lancé : dépendance en échec", job=job["name"], run_id=run_id)
            push(job["name"], -1, 0, run_id)
            continue
        ok, duration = run_job(job, run_id, attempt=1)
        if not ok:
            log("WARNING", f"Relance automatique de {job['name']}", job=job["name"], run_id=run_id)
            ok, duration = run_job(job, run_id, attempt=2)
        push(job["name"], 1 if ok else 0, duration, run_id)
        if not ok:
            blocked = True
            log("CRITICAL", f"Chaîne bloquée sur {job['name']} : escalade N2 requise",
                job=job["name"], run_id=run_id, error="CHAIN_BLOCKED")
    log("INFO", f"Fin chaîne nightly_orders ({'KO' if blocked else 'OK'})", run_id=run_id)


if __name__ == "__main__":
    while True:
        run_chain()
        time.sleep(INTERVAL)
