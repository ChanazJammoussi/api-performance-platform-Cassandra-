"""
explainer.py -- genere une explication en langage naturel pour chaque alerte FIRING.

Declenche une seule fois par transition OK -> FIRING (appele depuis detector.py).

Pipeline :
  1. build_context  : assemble metriques observees, baseline, correlation, historique 48h
  2. call_llm        : envoie le contexte a Gemini, parse et valide le JSON retourne
  3. generate_explanation : orchestre les deux, fallback template si echec

Contrat de sortie (JSON) :
  {
    "summary": str,            # deux phrases
    "suspected_cause": str,    # cause probable, formulee avec l'incertitude du score
    "checks": [str, str, str]  # 2-3 verifications concretes
  }

Si l'API Gemini est indisponible ou retourne un JSON invalide, l'alerte recoit un
template deterministe -- l'alerting ne doit jamais dependre de la disponibilite du LLM.
"""

import json
import logging
import math
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from google import genai

from baseline_utils import get_baseline, ENDPOINT_SLOS, DEFAULT_SLOS

load_dotenv()

log = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"
HISTORY_WINDOW_HOURS = 48
HISTORY_LIMIT = 5

_client = None


def _get_client():
    """Lazy init du client Gemini -- evite l'echec au chargement du module si la cle est absente."""
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        _client = genai.Client(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# 1. Context assembly
# ---------------------------------------------------------------------------

def _get_recent_alert_history(cur, endpoint_id, now):
    """Historique des alertes sur le meme endpoint dans les HISTORY_WINDOW_HOURS dernieres heures."""
    cur.execute("""
        SELECT signal_type, state, opened_at, severity, suspected_fault
        FROM alerts
        WHERE endpoint_id = %s
          AND opened_at IS NOT NULL
          AND opened_at >= %s - INTERVAL '%s hours'
        ORDER BY opened_at DESC
        LIMIT %s
    """, (endpoint_id, now, HISTORY_WINDOW_HOURS, HISTORY_LIMIT))
    rows = cur.fetchall()
    return [
        {
            "signal_type": r[0],
            "state": r[1],
            "opened_at": r[2].isoformat() if r[2] else None,
            "severity": r[3],
            "suspected_fault": r[4],
        }
        for r in rows
    ]


def _get_current_alert(cur, endpoint_id, signal_type):
    """Recupere l'etat courant de l'alerte (deja en FIRING au moment de l'appel)."""
    cur.execute("""
        SELECT severity, score, raw_value, layer, opened_at, suspected_fault, imputation_score
        FROM alerts
        WHERE endpoint_id = %s AND signal_type = %s
        LIMIT 1
    """, (endpoint_id, signal_type))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "severity":          row[0],
        "score":             row[1],
        "raw_value":         row[2],
        "layer":             row[3],
        "opened_at":         row[4].isoformat() if row[4] else None,
        "suspected_fault":   row[5],
        "imputation_score":  row[6],
    }


def build_context(cur, endpoint_id: str, signal_type: str, now: datetime) -> dict:
    """
    Assemble le payload structure envoye au LLM.

    Reutilise get_baseline() de detector.py pour garantir la coherence avec
    la baseline effectivement utilisee par le detecteur au moment de la decision.
    """
    dow = now.weekday()
    hour = now.hour

    alert = _get_current_alert(cur, endpoint_id, signal_type)
    if alert is None:
        log.warning(f"build_context: no alert row found for {endpoint_id}/{signal_type}")
        alert = {}

    # Baseline -- meme fonction que detector.py, donc meme fallback MIN/AVG/MAX
    metric_key = "p99_ms" if signal_type == "p99_ms" else signal_type
    baseline_row = get_baseline(cur, endpoint_id, metric_key, dow, hour)
    if baseline_row:
        p10, p50, p90 = baseline_row
    else:
        p10, p50, p90 = None, None, None

    def _clean(v):
        if v is None:
            return None
        if isinstance(v, float) and math.isnan(v):
            return None
        return round(v, 2)

    slos = ENDPOINT_SLOS.get(endpoint_id, DEFAULT_SLOS)
    slo_threshold = slos.get(signal_type, slos.get("p99_ms"))

    history = _get_recent_alert_history(cur, endpoint_id, now)

    context = {
        "endpoint_id":      endpoint_id,
        "signal_type":      signal_type,
        "observed_value":   _clean(alert.get("raw_value")),
        "slo_threshold":    slo_threshold,
        "baseline": {
            "p10": _clean(p10),
            "p50": _clean(p50),
            "p90": _clean(p90),
        },
        "detection_layer":  alert.get("layer"),
        "severity":         alert.get("severity"),
        "score":            _clean(alert.get("score")),
        "opened_at":        alert.get("opened_at"),
        "correlation": {
            "suspected_fault":  alert.get("suspected_fault"),
            "imputation_score": _clean(alert.get("imputation_score")),
        },
        "recent_history":   history,
    }
    return context


# ---------------------------------------------------------------------------
# 2. LLM call
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """Tu es un assistant SRE qui explique des alertes de performance API en francais clair et actionnable.

Contexte de l'alerte :
{context_json}

Genere une explication structuree. Reponds UNIQUEMENT avec un objet JSON valide, sans texte avant ou apres, sans balises markdown, au format exact suivant :

{{
  "summary": "deux phrases maximum decrivant ce qui s'est degrade et de combien par rapport a la baseline attendue",
  "suspected_cause": "cause probable formulee avec prudence -- si imputation_score est eleve (proche de 1.0) tu peux etre plus affirmatif, si bas ou absent reste prudent et dis que la cause n'est pas claire",
  "checks": ["verification concrete 1", "verification concrete 2", "verification concrete 3 optionnelle"]
}}

Regles :
- Ne jamais affirmer la cause avec certitude si imputation_score est null ou faible (< 0.5)
- Mentionner les chiffres cles (valeur observee, seuil SLO, baseline) dans summary
- Les checks doivent etre des actions concretes (ex: "verifier les logs du service X", "comparer avec le dernier deploiement"), pas des generalites
"""


def _validate_explanation(data: dict) -> bool:
    """Valide le contrat JSON minimal attendu."""
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        return False
    if not isinstance(data.get("suspected_cause"), str) or not data["suspected_cause"].strip():
        return False
    checks = data.get("checks")
    if not isinstance(checks, list) or len(checks) == 0:
        return False
    if not all(isinstance(c, str) and c.strip() for c in checks):
        return False
    return True


def _strip_markdown_fences(text: str) -> str:
    """Le modele renvoie parfois le JSON entoure de ```json ... ``` malgre la consigne."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def call_llm(context: dict) -> dict | None:
    """
    Appelle Gemini avec le contexte assemble. Retourne le dict valide ou None en cas d'echec.
    Ne leve jamais d'exception -- toute erreur est loggee et retourne None pour activer le fallback.
    """
    try:
        client = _get_client()
        prompt = PROMPT_TEMPLATE.format(context_json=json.dumps(context, indent=2, ensure_ascii=False))

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )

        raw_text = response.text
        if not raw_text:
            log.error("LLM returned empty response")
            return None

        cleaned = _strip_markdown_fences(raw_text)
        data = json.loads(cleaned)

        if not _validate_explanation(data):
            log.error(f"LLM response failed contract validation: {data}")
            return None

        return {
            "summary":          data["summary"].strip(),
            "suspected_cause":  data["suspected_cause"].strip(),
            "checks":           [c.strip() for c in data["checks"]],
        }

    except json.JSONDecodeError as e:
        log.error(f"LLM response is not valid JSON: {e}")
        return None
    except Exception as e:
        log.error(f"LLM call failed: {e}")
        return None


# ---------------------------------------------------------------------------
# 3. Fallback template
# ---------------------------------------------------------------------------

def _fallback_explanation(context: dict) -> dict:
    """Explication deterministe utilisee quand le LLM est indisponible ou invalide."""
    endpoint_id = context.get("endpoint_id", "unknown")
    signal_type = context.get("signal_type", "unknown")
    observed = context.get("observed_value")
    threshold = context.get("slo_threshold")
    suspected = context.get("correlation", {}).get("suspected_fault")
    imputation = context.get("correlation", {}).get("imputation_score")

    if observed is not None and threshold is not None:
        summary = (
            f"L'endpoint {endpoint_id} depasse le seuil SLO sur {signal_type} "
            f"(observe={observed}, seuil={threshold})."
        )
    else:
        summary = f"L'endpoint {endpoint_id} a declenche une alerte sur {signal_type}."

    if suspected and imputation and imputation >= 0.5:
        cause = f"Cause probable (score {imputation}): {suspected}."
    else:
        cause = "Cause non determinee automatiquement -- aucune correlation forte avec une injection ou un deploiement connu."

    checks = [
        f"Verifier les logs recents du service associe a {endpoint_id}",
        "Comparer avec le dernier evenement de deploiement",
        "Verifier l'etat des dependances downstream",
    ]

    return {
        "summary":          summary,
        "suspected_cause":  cause,
        "checks":           checks,
        "fallback":         True,
    }


# ---------------------------------------------------------------------------
# Orchestration + persistence
# ---------------------------------------------------------------------------

def generate_explanation(cur, endpoint_id: str, signal_type: str, now: datetime) -> dict:
    """
    Point d'entree principal. Assemble le contexte, appelle le LLM, et retombe
    sur un template deterministe en cas d'echec. Ecrit toujours le resultat
    dans alerts.explanation (jsonb).
    """
    # Garde-fou : ne jamais appeler Gemini deux fois pour la meme alerte
    cur.execute("""
        SELECT explanation FROM alerts
        WHERE endpoint_id = %s AND signal_type = %s
    """, (endpoint_id, signal_type))
    row = cur.fetchone()
    if row and row[0] is not None:
        log.info(f"Explanation already exists for {endpoint_id}/{signal_type}, skipping LLM call")
        return row[0]

    context = build_context(cur, endpoint_id, signal_type, now)

    explanation = call_llm(context)
    if explanation is None:
        log.warning(f"Falling back to deterministic template for {endpoint_id}/{signal_type}")
        explanation = _fallback_explanation(context)
    else:
        explanation["fallback"] = False

    write_explanation(cur, endpoint_id, signal_type, explanation)
    return explanation


def write_explanation(cur, endpoint_id: str, signal_type: str, explanation: dict):
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS explanation JSONB")
    cur.execute("""
        UPDATE alerts
        SET explanation = %s
        WHERE endpoint_id = %s AND signal_type = %s
    """, (json.dumps(explanation, ensure_ascii=False), endpoint_id, signal_type))

    source = "fallback" if explanation.get("fallback") else "llm"
    log.info(f"Explanation [{endpoint_id}/{signal_type}] written (source={source})")
