"""
Agent d'investigation : répond à une question en langage naturel en utilisant les outils
du serveur MCP "prod-monitoring" (tool calling via une API compatible OpenAI).

Boucle : le LLM choisit un outil -> appel MCP -> résultat renvoyé au LLM -> ... -> réponse finale.
Garde-fous : outils en lecture seule côté serveur, nombre d'étapes limité, résultats tronqués,
chaque appel d'outil est tracé.
"""
import asyncio
import json
import os
import time

import httpx
import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("MCP_URL", "http://mcp-server:8765/mcp")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "6"))
MAX_TOOL_CHARS = 6000

SYSTEM_PROMPT = """Tu es l'assistant d'astreinte N1 d'une équipe de production informatique.
Tu as accès à des outils de supervision en LECTURE SEULE (métriques Prometheus, alertes, logs
Elasticsearch, runbooks). Méthode obligatoire :
1. Appelle get_prod_status et list_active_alerts.
2. Creuse avec count_errors_by_type, search_logs ou query_metrics selon la question.
3. Si une alerte est active, appelle OBLIGATOIREMENT get_runbook avec le runbook indiqué par l'alerte.
Règles de réponse :
- Français, texte brut (pas de markdown), 12 lignes maximum, en 4 parties :
  CONSTAT (chiffres obtenus par les outils), CAUSE PROBABLE, ACTIONS (issues du runbook), ESCALADE.
- N'invente aucune valeur et ne recommande que des actions présentes dans le runbook ou
  directement justifiées par les données. Ce que les outils ne voient pas (serveur de base de
  données, réseau...) est hors périmètre : dis-le explicitement plutôt que de supposer.
- ESCALADE : pour une alerte critical, applique le délai du runbook (escalade N2 si non résolu
  dans ce délai) ; pour un warning ou sans alerte, pas d'escalade sauf aggravation.
- Tu ne peux exécuter aucune action sur la prod : tu recommandes, un humain décide."""


def llm_enabled():
    return bool(LLM_BASE_URL and LLM_API_KEY)


def _chat(messages, tools=None):
    payload = {"model": LLM_MODEL, "temperature": 0.1, "max_tokens": 700, "messages": messages}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    r = requests.post(f"{LLM_BASE_URL}/chat/completions",
                      headers={"Authorization": f"Bearer {LLM_API_KEY}"}, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def _tool_text(result):
    text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
    if result.isError:
        text = f"ERREUR OUTIL : {text}"
    return text[:MAX_TOOL_CHARS]


async def _investigate(question):
    trace = []
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools = [{"type": "function", "function": {
                "name": t.name, "description": t.description or "", "parameters": t.inputSchema}}
                for t in mcp_tools]
            messages = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": question}]

            for _ in range(MAX_STEPS):
                msg = _chat(messages, tools)
                calls = msg.get("tool_calls") or []
                if not calls:
                    return (msg.get("content") or "").strip(), trace
                messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
                for call in calls:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    started = time.time()
                    result = await session.call_tool(name, args)
                    trace.append({"tool": name, "args": args, "error": bool(result.isError),
                                  "duration_s": round(time.time() - started, 2)})
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": _tool_text(result)})

            # limite d'étapes atteinte : on demande une conclusion avec ce qui a été collecté
            messages.append({"role": "user", "content": "Conclus maintenant avec les données déjà collectées."})
            return (_chat(messages).get("content") or "").strip(), trace


def _root_cause(exc):
    """anyio enveloppe les erreurs dans des ExceptionGroup : on remonte à la vraie cause."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"API LLM HTTP {exc.response.status_code} : {exc.response.text[:300]}"
    if isinstance(exc, (httpx.ConnectError, ConnectionError, OSError)):
        return f"serveur MCP injoignable ({MCP_URL}) : {exc}"
    return f"{type(exc).__name__} : {exc}"


def investigate(question):
    """Point d'entrée synchrone (appelé depuis les threads Flask / Telegram)."""
    try:
        return asyncio.run(_investigate(question))
    except BaseException as exc:  # noqa: BLE001 - on veut un message lisible côté Telegram
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise RuntimeError(_root_cause(exc)) from exc
