"""
test_explainer_fallback.py -- tests unitaires pour _fallback_explanation.

Verifie que le fallback (active quand Gemini est indisponible) :
  - produit toujours un dict valide avec summary / suspected_cause / checks / fallback=True
  - utilise service_attribution.root_cause_candidates en priorite
  - retombe sur la correlation injection/deploiement si pas de candidat service
  - retombe sur le message neutre si aucune information causale

Aucun appel reseau : _fallback_explanation est une fonction pure.
"""

import pytest

from explainer import _fallback_explanation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(**kwargs):
    """Construit un contexte minimal valide, surchargeble par kwargs."""
    base = {
        "endpoint_id":    "POST /api/payments",
        "signal_type":    "p99_ms",
        "observed_value": 920.0,
        "slo_threshold":  800.0,
        "correlation":    {"suspected_fault": None, "imputation_score": None},
        "service_attribution": None,
    }
    base.update(kwargs)
    return base


def _is_valid(result):
    """Verifie le contrat minimal de sortie."""
    assert isinstance(result, dict)
    assert isinstance(result.get("summary"), str) and result["summary"]
    assert isinstance(result.get("suspected_cause"), str) and result["suspected_cause"]
    assert isinstance(result.get("checks"), list) and len(result["checks"]) > 0
    assert result.get("fallback") is True


# ---------------------------------------------------------------------------
# Cas 1 : service_attribution avec un candidat -> cause issue du graphe
# ---------------------------------------------------------------------------

def test_fallback_uses_service_attribution_candidate():
    ctx = _ctx(service_attribution={
        "root_cause_candidates": [
            {"endpoint_id": "GET /orders/{order_id}", "service": "orders", "anomaly_score": 0.91},
        ]
    })
    result = _fallback_explanation(ctx)
    _is_valid(result)

    cause = result["suspected_cause"]
    assert "GET /orders/{order_id}" in cause
    assert "orders" in cause
    assert "0.91" in cause
    assert "propagation" in cause.lower() or "dependant" in cause.lower()


# ---------------------------------------------------------------------------
# Cas 2 : plusieurs candidats -> utilise le premier (score le plus eleve)
# ---------------------------------------------------------------------------

def test_fallback_uses_first_candidate_when_multiple():
    ctx = _ctx(service_attribution={
        "root_cause_candidates": [
            {"endpoint_id": "GET /orders/{order_id}", "service": "orders", "anomaly_score": 0.91},
            {"endpoint_id": "POST /payments",         "service": "payments", "anomaly_score": 0.55},
        ]
    })
    result = _fallback_explanation(ctx)
    _is_valid(result)

    # Le premier candidat (score le plus eleve) doit apparaitre dans la cause
    assert "GET /orders/{order_id}" in result["suspected_cause"]
    # Le second ne doit pas apparaitre (on prend uniquement le meilleur)
    assert "POST /payments" not in result["suspected_cause"]


# ---------------------------------------------------------------------------
# Cas 3 : pas de candidat service mais correlation forte -> comportement existant
# ---------------------------------------------------------------------------

def test_fallback_falls_back_to_correlation_when_no_service_candidate():
    ctx = _ctx(
        service_attribution={"root_cause_candidates": []},
        correlation={"suspected_fault": "bad_deploy:latency_step:orders", "imputation_score": 0.85},
    )
    result = _fallback_explanation(ctx)
    _is_valid(result)

    cause = result["suspected_cause"]
    assert "bad_deploy:latency_step:orders" in cause
    assert "0.85" in cause


# ---------------------------------------------------------------------------
# Cas 4 : pas de candidat service + correlation faible -> message neutre
# ---------------------------------------------------------------------------

def test_fallback_neutral_message_when_no_information():
    ctx = _ctx(
        service_attribution=None,
        correlation={"suspected_fault": None, "imputation_score": None},
    )
    result = _fallback_explanation(ctx)
    _is_valid(result)

    # Le message neutre ne doit pas mentionner de service ou de fault
    assert "non determinee" in result["suspected_cause"].lower() or \
           "aucune" in result["suspected_cause"].lower()


# ---------------------------------------------------------------------------
# Cas 5 : service_attribution absent (None) -> retombe gracieusement
# ---------------------------------------------------------------------------

def test_fallback_handles_missing_service_attribution():
    ctx = _ctx(service_attribution=None)
    result = _fallback_explanation(ctx)
    _is_valid(result)
    # Pas d'exception : le fallback est robuste meme sans service_attribution


# ---------------------------------------------------------------------------
# Cas 6 : candidat sans anomaly_score -> cause sans "(score=...)"
# ---------------------------------------------------------------------------

def test_fallback_candidate_without_score():
    ctx = _ctx(service_attribution={
        "root_cause_candidates": [
            {"endpoint_id": "GET /orders/{order_id}", "service": "orders", "anomaly_score": None},
        ]
    })
    result = _fallback_explanation(ctx)
    _is_valid(result)

    cause = result["suspected_cause"]
    assert "GET /orders/{order_id}" in cause
    # Pas de "(score=...)" si le score est absent
    assert "score=" not in cause


# ---------------------------------------------------------------------------
# Cas 7 : summary contient les valeurs observees et le seuil SLO
# ---------------------------------------------------------------------------

def test_fallback_summary_contains_observed_and_threshold():
    ctx = _ctx(observed_value=920.0, slo_threshold=800.0)
    result = _fallback_explanation(ctx)
    _is_valid(result)

    assert "920.0" in result["summary"] or "920" in result["summary"]
    assert "800.0" in result["summary"] or "800" in result["summary"]


# ---------------------------------------------------------------------------
# Cas 8 : summary enrichi avec score et severite quand disponibles
# ---------------------------------------------------------------------------

def test_fallback_cause_uses_call_type_and_uncertainty_language():
    """
    Test C — LLM indisponible : le fallback exploite le champ call_type du candidat
    et utilise toujours un vocabulaire d'incertitude ("suspecte", "pourrait"...).
    """
    ctx = _ctx(service_attribution={
        "root_cause_candidates": [
            {
                "endpoint_id": "POST /payments",
                "service":     "payments",
                "anomaly_score": 0.82,
                "call_type":   "direct",
                "reason":      "degradation de dependance direct",
            }
        ]
    })
    result = _fallback_explanation(ctx)
    _is_valid(result)

    cause = result["suspected_cause"]
    assert "POST /payments" in cause
    assert "payments" in cause
    assert "0.82" in cause
    # Incertitude obligatoire
    cause_lower = cause.lower()
    assert any(w in cause_lower for w in ["probable", "suspectee", "suspecte", "pourrait", "correl"])


def test_fallback_neutral_when_children_present_but_healthy():
    """
    Test D — Donnees incompletes : service_attribution present mais root_cause_candidates vide.
    Les dependances directes sont saines -> degradation locale -> message neutre.
    Aucune propagation ne doit etre affirmee.
    """
    ctx = _ctx(
        service_attribution={
            "root_cause_candidates": [],
            "children": [
                {"endpoint_id": "GET /orders", "service": "orders",
                 "call_type": "direct", "status": "normal",
                 "anomaly_score": 0.12, "direction": "normal"}
            ],
            "parents": [],
        }
    )
    result = _fallback_explanation(ctx)
    _is_valid(result)

    cause_lower = result["suspected_cause"].lower()
    # Pas de candidat -> message neutre (priorite 3)
    assert "non determinee" in cause_lower or "aucune" in cause_lower
    # Pas de propagation affirmee quand les dependances sont saines
    assert "propagation" not in cause_lower


def test_fallback_cause_high_confidence_uses_vraisemblablement():
    """
    confidence="high" -> "vraisemblablement causee par" dans la cause.
    Niveau le plus affirmatif acceptable (suspectee, pas prouvee).
    """
    ctx = _ctx(service_attribution={
        "root_cause_candidates": [
            {"endpoint_id": "POST /payments", "service": "payments",
             "anomaly_score": 0.82, "confidence_score": 0.74,
             "confidence": "high", "call_type": "direct",
             "reason": "co-degradation correlee sur dependance direct -- causalite suspectee, non demontree"}
        ]
    })
    result = _fallback_explanation(ctx)
    _is_valid(result)
    cause_lower = result["suspected_cause"].lower()
    assert "vraisemblablement" in cause_lower or "causee par" in cause_lower
    # Ne jamais presenter comme certitude absolue
    assert "certainement" not in cause_lower and "prouvee" not in cause_lower


def test_fallback_cause_medium_confidence_uses_semble():
    """
    confidence="medium" (cas cascade score eleve) -> "semble liee a" dans la cause.
    Reproduit l'exemple : anomaly_score=0.95, confidence_score=0.62, confidence="medium".
    """
    ctx = _ctx(service_attribution={
        "root_cause_candidates": [
            {"endpoint_id": "GET /orders/{order_id}", "service": "orders",
             "anomaly_score": 0.95, "confidence_score": 0.62,
             "confidence": "medium", "call_type": "cascade",
             "reason": "co-degradation observee sur dependance cascade -- causalite non demontree"}
        ]
    })
    result = _fallback_explanation(ctx)
    _is_valid(result)
    cause_lower = result["suspected_cause"].lower()
    assert "semble" in cause_lower or "liee" in cause_lower
    # Incertitude maintenue meme avec anomaly_score=0.95
    assert "certainement" not in cause_lower and "prouvee" not in cause_lower


def test_fallback_cause_low_confidence_uses_correlation_possible():
    """
    confidence="low" -> vocabulaire "correlation possible", invitation a investiguer.
    Signal faible : ne pas conclure.
    """
    ctx = _ctx(service_attribution={
        "root_cause_candidates": [
            {"endpoint_id": "GET /orders", "service": "orders",
             "anomaly_score": 0.40, "confidence_score": 0.26,
             "confidence": "low", "call_type": "cascade",
             "reason": "signal de degradation faible sur dependance cascade -- correlation possible, non demontree"}
        ]
    })
    result = _fallback_explanation(ctx)
    _is_valid(result)
    cause_lower = result["suspected_cause"].lower()
    assert "possible" in cause_lower or "faible" in cause_lower or "investigation" in cause_lower


def test_fallback_summary_enriched_with_score_and_severity():
    """
    Quand le contexte contient score et severity, le summary doit les inclure
    pour que le fallback soit aussi informatif que possible sans LLM.
    """
    ctx = _ctx(
        observed_value=920.0,
        slo_threshold=800.0,
        score=0.78,
        severity="high",
    )
    result = _fallback_explanation(ctx)
    _is_valid(result)

    summary = result["summary"]
    assert "0.78" in summary or "score" in summary.lower()
    assert "high" in summary or "severite" in summary.lower()
