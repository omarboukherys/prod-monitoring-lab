"""
incident-receiver + agent de triage IA.

1. Reçoit les webhooks d'Alertmanager et les journalise comme des "tickets" (JSON lu par Filebeat).
2. Pour chaque alerte qui se déclenche, l'agent :
   - collecte le contexte : métriques Prometheus, dernières erreurs dans Elasticsearch, runbook
   - demande un diagnostic à un LLM (API compatible OpenAI : Groq, Mistral, OpenAI, Ollama...)
   - à défaut de LLM configuré, produit un triage par règles
   - envoie le résultat sur Telegram
3. Bot Telegram en lecture seule : /status, /alerts, /help, et questions libres
   -> agent d'investigation qui utilise les outils du serveur MCP "prod-monitoring" (agent.py)

Principe de sécurité : l'agent LIT et RECOMMANDE, il n'exécute aucune action sur la prod.
"""
import json
import os
import threading
import time

import requests
from flask import Flask, jsonify, request

import agent

app = Flask(__name__)

LOG_FILE = os.path.join(os.environ.get("LOG_DIR", "/var/log/app"), "incidents.log")
PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
ELASTIC = os.environ.get("ELASTIC_URL", "http://elasticsearch:9200")
RUNBOOK_DIR = os.environ.get("RUNBOOK_DIR", "/runbooks")

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")  # ex. https://api.groq.com/openai/v1
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)


def write_log(entry):
    entry = {"@timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "service": "incident-receiver", **entry}
    line = json.dumps(entry, ensure_ascii=False)
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ------------------------------------------------------------------ collecte du contexte
def prom(query):
    try:
        r = requests.get(f"{PROMETHEUS}/api/v1/query", params={"query": query}, timeout=5)
        return r.json()["data"]["result"]
    except Exception:
        return []


def prom_value(query, default="n/a"):
    res = prom(query)
    return round(float(res[0]["value"][1]), 3) if res else default


def snapshot():
    batch = {r["metric"].get("job", "?"): {"1": "OK", "0": "KO", "-1": "BLOQUÉ"}.get(r["value"][1], r["value"][1])
             for r in prom("batch_job_last_status")}
    return {
        "service_up": prom_value('up{job="orders-api"}'),
        "req_per_s": prom_value('sum(rate(http_requests_total{job="orders-api"}[2m]))'),
        "error_rate": prom_value('(sum(rate(http_requests_total{job="orders-api",status=~"5.."}[2m])) or vector(0))'
                                 ' / sum(rate(http_requests_total{job="orders-api"}[2m]))'),
        "p95_latency_s": prom_value('histogram_quantile(0.95, sum by (le) '
                                    '(rate(http_request_duration_seconds_bucket{job="orders-api"}[2m])))'),
        "leaked_memory_mb": prom_value("app_leaked_memory_mb"),
        "batch": batch,
    }


def recent_errors(limit=15):
    query = {
        "size": limit,
        "sort": [{"@timestamp": "desc"}],
        "_source": ["@timestamp", "service", "level", "message", "error", "job", "path"],
        "query": {"bool": {"filter": [
            {"range": {"@timestamp": {"gte": "now-10m"}}},
            {"terms": {"level.keyword": ["ERROR", "CRITICAL"]}},
        ]}},
    }
    try:
        r = requests.post(f"{ELASTIC}/prod-logs-*/_search", json=query, timeout=5)
        return [h["_source"] for h in r.json().get("hits", {}).get("hits", [])]
    except Exception:
        return []


def read_runbook(path):
    if not path:
        return ""
    try:
        with open(os.path.join(RUNBOOK_DIR, os.path.basename(path)), encoding="utf-8") as fh:
            return fh.read()[:4000]
    except OSError:
        return ""


# ------------------------------------------------------------------ analyse
SYSTEM_PROMPT = """Tu es un ingénieur de production N1 expérimenté, d'astreinte.
On te donne une alerte, un état des métriques, les dernières erreurs de logs et le runbook.
Réponds en français, en texte brut (pas de markdown), en 4 parties courtes :
RÉSUMÉ : 1 phrase sur l'impact.
CAUSE PROBABLE : appuyée uniquement sur les données fournies. Si les données ne suffisent pas, dis-le.
ACTIONS : 3 étapes concrètes maximum, tirées du runbook fourni (pas d'action hors runbook).
ESCALADE : alerte critical -> escalade N2 si non résolu dans le délai prévu par le runbook ;
warning -> pas d'escalade sauf aggravation. Indique vers qui et dans quel délai.
N'invente aucune donnée absente du contexte."""


def llm_triage(context):
    r = requests.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={
            "model": LLM_MODEL,
            "temperature": 0.1,
            "max_tokens": 500,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=1)},
            ],
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def rule_triage(context):
    """Triage déterministe utilisé sans LLM ou si l'appel échoue."""
    errors = [e.get("error") for e in context["recent_errors"] if e.get("error")]
    top = max(set(errors), key=errors.count) if errors else None
    m = context["metrics"]
    lines = [
        f"RÉSUMÉ : alerte {context['alert']['name']} ({context['alert']['severity']}).",
        f"MÉTRIQUES : up={m['service_up']} erreurs={m['error_rate']} p95={m['p95_latency_s']}s "
        f"mémoire={m['leaked_memory_mb']}Mo batch={m['batch']}",
        f"ERREUR DOMINANTE (10 min) : {top} ({errors.count(top)} occurrences)" if top
        else "ERREUR DOMINANTE : aucune erreur dans les logs récents",
        f"ACTIONS : suivre le runbook {context['alert']['runbook']}",
        "ESCALADE : " + ("oui, N2 si non résolu en 15 min" if context["alert"]["severity"] == "critical"
                         else "non, à surveiller"),
    ]
    return "\n".join(lines)


def analyse(alert):
    labels, annotations = alert.get("labels", {}), alert.get("annotations", {})
    context = {
        "alert": {
            "name": labels.get("alertname"),
            "severity": labels.get("severity"),
            "summary": annotations.get("summary"),
            "runbook": annotations.get("runbook"),
            "labels": labels,
            "started_at": alert.get("startsAt"),
        },
        "metrics": snapshot(),
        "recent_errors": recent_errors(),
        "runbook_content": read_runbook(annotations.get("runbook")),
    }
    engine = "rules"
    if LLM_BASE_URL and LLM_API_KEY:
        try:
            return llm_triage(context), "llm"
        except Exception as exc:
            write_log({"level": "WARNING", "message": f"LLM indisponible, triage par règles : {exc}"})
    return rule_triage(context), engine


# ------------------------------------------------------------------ Telegram
def tg_send(text, chat_id=None):
    chat_id = chat_id or TG_CHAT
    if not (TG_TOKEN and chat_id):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": chat_id, "text": text[:4000]}, timeout=10)
    except requests.RequestException as exc:
        write_log({"level": "WARNING", "message": f"Envoi Telegram échoué : {exc}"})


def handle_alert(alert, team):
    labels = alert.get("labels", {})
    icon = "🔴" if labels.get("severity") == "critical" else "🟠"
    if alert.get("status") == "resolved":
        tg_send(f"✅ RÉSOLU : {labels.get('alertname')}\n{alert.get('annotations', {}).get('summary', '')}")
        return
    started = time.time()
    triage, engine = analyse(alert)
    write_log({
        "level": "INFO", "message": f"Triage {engine} terminé pour {labels.get('alertname')}",
        "alertname": labels.get("alertname"), "triage_engine": engine,
        "triage_duration_s": round(time.time() - started, 2), "triage": triage,
    })
    tg_send(f"{icon} {labels.get('alertname')} [{labels.get('severity')}] → {team}\n"
            f"{alert.get('annotations', {}).get('summary', '')}\n\n"
            f"🤖 Analyse ({engine}) :\n{triage}\n\n"
            f"Agent en lecture seule : aucune action exécutée.")


@app.post("/alert")
def alert():
    team = request.args.get("team", "unknown")
    payload = request.get_json(force=True, silent=True) or {}
    for a in payload.get("alerts", []):
        labels = a.get("labels", {})
        write_log({
            "level": "CRITICAL" if labels.get("severity") == "critical" else "WARNING",
            "message": f"[{a.get('status', '?').upper()}] {labels.get('alertname')} -> {team}",
            "alertname": labels.get("alertname"),
            "severity": labels.get("severity"),
            "alert_status": a.get("status"),
            "assigned_to": team,
            "summary": a.get("annotations", {}).get("summary"),
            "runbook": a.get("annotations", {}).get("runbook"),
        })
        # analyse en arrière-plan : Alertmanager attend une réponse rapide
        threading.Thread(target=handle_alert, args=(a, team), daemon=True).start()
    return jsonify(received=len(payload.get("alerts", [])))


@app.get("/health")
def health():
    return jsonify(status="UP", llm=bool(LLM_BASE_URL and LLM_API_KEY), telegram=bool(TG_TOKEN and TG_CHAT),
                   mcp_url=agent.MCP_URL)


def ask(question, channel):
    """Question libre -> agent MCP. Renvoie (réponse, trace) et journalise l'échange."""
    if not agent.llm_enabled():
        return ("L'agent d'investigation a besoin d'un LLM : renseigner LLM_BASE_URL et LLM_API_KEY "
                "dans le fichier .env (voir .env.example)."), []
    started = time.time()
    try:
        answer, trace = agent.investigate(question)
    except Exception as exc:
        answer, trace = f"Investigation impossible : {exc}", []
    write_log({
        "level": "INFO", "message": f"Question agent ({channel}) : {question[:200]}",
        "question": question[:500], "answer": answer[:2000], "channel": channel,
        "tools_used": [t["tool"] for t in trace], "tool_calls": len(trace),
        "agent_duration_s": round(time.time() - started, 2),
    })
    return answer, trace


@app.post("/ask")
def ask_http():
    """Même agent que sur Telegram, utilisable avec curl : {"question": "..."}"""
    question = ((request.get_json(silent=True) or {}).get("question") or "").strip()
    if not question:
        return jsonify(error="champ 'question' manquant"), 400
    answer, trace = ask(question, "http")
    return jsonify(question=question, answer=answer, tool_calls=trace)


# ------------------------------------------------------------------ commandes du bot
def format_status():
    m = snapshot()
    batch = "\n".join(f"  {job} : {st}" for job, st in sorted(m["batch"].items())) or "  aucune donnée"
    return (f"📊 État de la prod\n"
            f"orders-api : {'UP' if m['service_up'] == 1 else 'DOWN'}\n"
            f"Trafic : {m['req_per_s']} req/s\n"
            f"Taux d'erreurs : {m['error_rate']}\n"
            f"Latence p95 : {m['p95_latency_s']} s\n"
            f"Mémoire retenue : {m['leaked_memory_mb']} Mo\n"
            f"Batch nightly_orders :\n{batch}")


def format_alerts():
    try:
        alerts = requests.get(f"{PROMETHEUS}/api/v1/alerts", timeout=5).json()["data"]["alerts"]
    except Exception:
        return "Prometheus injoignable"
    firing = [a for a in alerts if a["state"] == "firing"]
    if not firing:
        return "✅ Aucune alerte active"
    return "🚨 Alertes actives :\n" + "\n".join(
        f"- [{a['labels'].get('severity')}] {a['labels'].get('alertname')} : {a['annotations'].get('summary')}"
        for a in firing)


def telegram_question(question, chat_id):
    if not question:
        tg_send(COMMANDS["/help"](), chat_id)
        return
    tg_send("🔎 J'enquête via les outils MCP...", chat_id)
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction",
                      json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except requests.RequestException:
        pass
    answer, trace = ask(question, "telegram")
    tools = " → ".join(t["tool"] for t in trace) or "aucun"
    tg_send(f"🤖 {answer}\n\n🧰 Outils MCP utilisés : {tools}", chat_id)


COMMANDS = {
    "/status": format_status,
    "/alerts": format_alerts,
    "/help": lambda: ("/status : état de la prod\n/alerts : alertes actives\n"
                      "Ou pose une question libre, par ex. « pourquoi l'API renvoie des 503 ? » : "
                      "l'agent enquête avec les outils MCP.\nLe bot est en lecture seule."),
}


BOT_STARTED_AT = time.time()


def telegram_polling():
    offset = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                             params={"offset": offset, "timeout": 30}, timeout=40)
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if chat_id != str(TG_CHAT):  # seul le chat autorisé peut interroger la prod
                    continue
                if msg.get("date", 0) < BOT_STARTED_AT - 5:  # envoyé pendant que le bot était arrêté
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                command = text.split()[0].split("@")[0]
                if command in COMMANDS:
                    tg_send(COMMANDS[command](), chat_id)
                elif command == "/start":
                    tg_send(COMMANDS["/help"](), chat_id)
                else:
                    question = text[len("/ask"):].strip() if command == "/ask" else text
                    threading.Thread(target=telegram_question, args=(question, chat_id), daemon=True).start()
        except Exception as exc:
            print(f"[telegram] polling: {exc}", flush=True)
            time.sleep(5)


if TG_TOKEN and TG_CHAT:
    threading.Thread(target=telegram_polling, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
