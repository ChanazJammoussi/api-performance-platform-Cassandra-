"""
prompt_builder.py -- contexte synthetique et construction du prompt pour le benchmark Gemini.
"""

import json

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
- Les checks doivent etre des actions concretes, pas des generalites
"""

SCENARIOS = [
    {
        "name": "latency_step_payments",
        "fault_type": "latency_step",
        "endpoint_id": "POST /api/payments",
        "signal_type": "p99_ms",
        "observed_value": 850,
        "slo_threshold": 800,
        "baseline": {"p50": 120, "p90": 180},
        "correlation": {
            "suspected_fault": "deploy v2.3.1",
            "imputation_score": 0.92,
        },
    },
    {
        "name": "error_burst_payments",
        "fault_type": "error_burst",
        "endpoint_id": "POST /payments",
        "signal_type": "error_rate_5xx",
        "observed_value": 0.18,
        "slo_threshold": 0.05,
        "baseline": {"p50": 0.01, "p90": 0.03},
        "correlation": {
            "suspected_fault": "feature_flag FF-42",
            "imputation_score": 0.45,
        },
    },
    {
        "name": "latency_creep_orders",
        "fault_type": "latency_creep",
        "endpoint_id": "GET /orders",
        "signal_type": "p99_ms",
        "observed_value": 420,
        "slo_threshold": 300,
        "baseline": {"p50": 95, "p90": 160},
        "correlation": {
            "suspected_fault": None,
            "imputation_score": None,
        },
    },
    {
        "name": "pool_shrink_payments",
        "fault_type": "pool_shrink",
        "endpoint_id": "POST /api/payments",
        "signal_type": "p99_ms",
        "observed_value": 700,
        "slo_threshold": 800,
        "baseline": {"p50": 120, "p90": 180},
        "correlation": {
            "suspected_fault": "connection_pool_resize",
            "imputation_score": 0.71,
        },
    },
    {
        "name": "bad_deploy_orders",
        "fault_type": "bad_deploy",
        "endpoint_id": "GET /orders/{order_id}",
        "signal_type": "p99_ms",
        "observed_value": 980,
        "slo_threshold": 300,
        "baseline": {"p50": 100, "p90": 200},
        "correlation": {
            "suspected_fault": "deploy v3.0.0",
            "imputation_score": 0.88,
        },
    },
]


def build_prompt(scenario: dict) -> str:
    context_json = json.dumps(scenario, indent=2, ensure_ascii=False)
    return PROMPT_TEMPLATE.format(context_json=context_json)
