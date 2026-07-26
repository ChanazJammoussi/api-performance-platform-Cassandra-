"""
test_service_attribution.py -- tests unitaires pour analyze_service_graph.

Verifie le comportement de l'analyse DAG dans correlator.py, independamment
du detecteur et de la base de donnees.

Cas couverts :
  1. Aucun enfant degrade     -> root_cause_candidates vide
  2. Enfant direct degrade    -> figure comme candidat root-cause
  3. Enfants tous normaux     -> root_cause_candidates vide
  4. Plusieurs enfants degrades -> trie par score descendant (meilleur candidat en tete)
  5. Endpoint enfant          -> parents non vides, pas d'enfants
  6. Direction "improvement"  -> exclue des candidats (spec §5.4 direction gating)
  7. Limite 1-hop             -> comportement volontaire documente
  8. Erreur DB               -> retourne {} sans lever d'exception
  9. Endpoint isole           -> structure complete avec listes vides
 10. Champ status             -> "degraded" / "improved" / "normal" / "unknown"
 11. Champ reason             -> present dans root_cause_candidates
 12. Champs top-level         -> endpoint_id et service dans le resultat
"""

import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone

from correlator import analyze_service_graph

UTC = timezone.utc
NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def _make_cursor(graph_rows, anomaly_rows):
    """
    Curseur mock a deux reponses sequentielles :
      1er fetchall() -> graph_rows  (endpoint_relationships)
      2eme fetchall() -> anomaly_rows (anomalies)
    """
    cur = MagicMock()
    cur.fetchall.side_effect = [graph_rows, anomaly_rows]
    return cur


# ---------------------------------------------------------------------------
# Cas 1 : aucun enfant degrade
# ---------------------------------------------------------------------------

def test_no_degraded_children_returns_empty_candidates():
    graph_rows = [
        ("POST /api/payments", "POST /payments", "gateway", "payments", "direct"),
    ]
    anomaly_rows = []  # POST /payments sans anomalie recente

    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    assert result["root_cause_candidates"] == []
    assert len(result["children"]) == 1
    child = result["children"][0]
    assert child["endpoint_id"] == "POST /payments"
    assert child["service"] == "payments"
    assert child["anomaly_score"] is None
    assert result["parents"] == []


# ---------------------------------------------------------------------------
# Cas 2 : enfant direct degrade -> candidat root-cause identifie
# ---------------------------------------------------------------------------

def test_degraded_direct_child_is_root_cause_candidate():
    graph_rows = [
        ("POST /api/payments", "POST /payments", "gateway", "payments", "direct"),
    ]
    anomaly_rows = [
        ("POST /payments", 0.82, "degradation"),
    ]

    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    assert len(result["root_cause_candidates"]) == 1
    cand = result["root_cause_candidates"][0]
    assert cand["endpoint_id"] == "POST /payments"
    assert cand["service"] == "payments"
    assert cand["anomaly_score"] == pytest.approx(0.82)


# ---------------------------------------------------------------------------
# Cas 3 : enfants presents mais direction normale -> root_cause_candidates vide
# ---------------------------------------------------------------------------

def test_children_normal_direction_excluded_from_candidates():
    graph_rows = [
        ("POST /api/payments", "POST /payments", "gateway", "payments", "direct"),
    ]
    anomaly_rows = [
        ("POST /payments", 0.20, "normal"),  # anomalie faible, direction normale
    ]

    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    assert result["root_cause_candidates"] == []
    # L'enfant est quand meme presente dans children (score visible)
    assert result["children"][0]["direction"] == "normal"


# ---------------------------------------------------------------------------
# Cas 4 : deux enfants degrades -> trie par score descendant
# ---------------------------------------------------------------------------

def test_multiple_degraded_children_ranked_by_score():
    graph_rows = [
        ("POST /api/payments", "POST /payments",         "gateway", "payments", "direct"),
        ("POST /api/payments", "GET /orders/{order_id}", "gateway", "orders",   "direct"),
    ]
    anomaly_rows = [
        ("POST /payments",         0.45, "degradation"),
        ("GET /orders/{order_id}", 0.87, "degradation"),
    ]

    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    candidates = result["root_cause_candidates"]
    assert len(candidates) == 2
    # Meilleur candidat en premier
    assert candidates[0]["endpoint_id"] == "GET /orders/{order_id}"
    assert candidates[0]["anomaly_score"] == pytest.approx(0.87)
    assert candidates[1]["endpoint_id"] == "POST /payments"
    assert candidates[1]["anomaly_score"] == pytest.approx(0.45)
    # Ordre descendant verifie
    assert candidates[0]["anomaly_score"] > candidates[1]["anomaly_score"]


# ---------------------------------------------------------------------------
# Cas bonus : endpoint enfant (parents non vides, pas d'enfants)
# ---------------------------------------------------------------------------

def test_child_endpoint_has_parents_no_children():
    """GET /orders/{order_id} est appele par deux parents ; lui-meme n'appelle rien."""
    graph_rows = [
        ("GET /api/orders/{order_id}", "GET /orders/{order_id}", "gateway",  "orders", "direct"),
        ("POST /payments",             "GET /orders/{order_id}", "payments", "orders", "cascade"),
    ]
    anomaly_rows = []  # les parents n'ont pas d'anomalie recente

    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "GET /orders/{order_id}", NOW)

    assert result["children"] == []
    assert result["root_cause_candidates"] == []
    parent_ids = {p["endpoint_id"] for p in result["parents"]}
    assert "GET /api/orders/{order_id}" in parent_ids
    assert "POST /payments" in parent_ids


# ---------------------------------------------------------------------------
# Test A : direction "improvement" exclue des candidats (spec §5.4)
# ---------------------------------------------------------------------------

def test_improvement_direction_excluded_from_candidates():
    """
    Une anomalie avec direction="improvement" (perf anormalement bonne) ne doit
    jamais figurer dans root_cause_candidates.

    Regle spec §5.4 : la couche ML ne contribue qu'en cas de degradation.
    Le meme principe s'applique a l'attribution de cause racine : on ne signale
    pas comme cause un service qui va "trop bien".
    """
    graph_rows = [
        ("POST /payments", "GET /orders/{order_id}", "payments", "orders", "cascade"),
    ]
    anomaly_rows = [
        ("GET /orders/{order_id}", 0.90, "improvement"),
    ]

    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /payments", NOW)

    # "improvement" ne doit jamais etre un candidat root-cause
    assert result["root_cause_candidates"] == []
    # L'enfant est quand meme visible dans children (score et direction affichables)
    assert len(result["children"]) == 1
    assert result["children"][0]["direction"] == "improvement"
    assert result["children"][0]["anomaly_score"] == pytest.approx(0.90)


# ---------------------------------------------------------------------------
# Test B : limite 1-hop volontaire documentee
# ---------------------------------------------------------------------------

def test_one_hop_limit_deep_cause_not_visible():
    """
    Comportement VOLONTAIRE : analyze_service_graph ne traverse qu'un seul saut.

    Graphe de dependance :
        POST /api/payments  ──direct──►  POST /payments  ──cascade──►  GET /orders/{order_id}

    Si GET /orders/{order_id} est en degradation mais POST /payments ne l'est pas,
    POST /api/payments ne peut pas voir GET /orders/{order_id} comme candidat root-cause.
    C'est correct : la cause profonde est visible dans l'alerte de POST /payments.

    La requete SQL ne charge que les aretes touchant directement l'endpoint interroge :
        WHERE parent_endpoint_id = %s OR child_endpoint_id = %s
    L'arete (POST /payments -> GET /orders/{order_id}) n'est donc pas retournee
    pour la requete sur POST /api/payments.

    Ce test documente cette limite comme comportement attendu, pas comme un bug.
    Pour obtenir la chaine complete, consulter les alertes de POST /payments.
    """
    # Seule l'arete touchant directement POST /api/payments est retournee par le mock.
    # L'arete (POST /payments -> GET /orders/{order_id}) est absente : 2 sauts.
    graph_rows = [
        ("POST /api/payments", "POST /payments", "gateway", "payments", "direct"),
    ]
    # GET /orders/{order_id} est degrade (2 sauts) mais POST /payments est sain.
    # Le mock ne retourne donc aucune anomalie pour POST /payments (l'unique voisin direct).
    anomaly_rows = []

    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    # POST /api/payments ne voit PAS GET /orders/{order_id} (2 sauts)
    all_candidate_ids = [c["endpoint_id"] for c in result["root_cause_candidates"]]
    assert "GET /orders/{order_id}" not in all_candidate_ids

    # Son seul enfant visible est POST /payments, sans score d'anomalie ce cycle
    assert len(result["children"]) == 1
    assert result["children"][0]["endpoint_id"] == "POST /payments"
    assert result["children"][0]["anomaly_score"] is None

    # root_cause_candidates vide : POST /payments n'est pas degrade
    assert result["root_cause_candidates"] == []


# ---------------------------------------------------------------------------
# Test C : erreur DB lors de la lecture du graphe -> retourne {} sans exception
# ---------------------------------------------------------------------------

def test_db_error_returns_empty_dict():
    """
    Si endpoint_relationships est inaccessible (table absente, connexion perdue...),
    analyze_service_graph doit retourner {} sans lever d'exception.
    Best-effort : l'alerte ne doit jamais etre bloquee par une erreur DB ici.
    """
    cur = MagicMock()
    cur.execute.side_effect = Exception("relation endpoint_relationships does not exist")

    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    assert result == {}


# ---------------------------------------------------------------------------
# Test D : endpoint isole (aucune arete) -> structure complete avec listes vides
# ---------------------------------------------------------------------------

def test_isolated_endpoint_returns_complete_empty_structure():
    """
    Un endpoint sans aucune relation dans endpoint_relationships (ex: /health)
    doit retourner une structure valide avec des listes vides, pas {}.
    """
    cur = _make_cursor([], [])  # aucune arete, aucune anomalie voisine

    result = analyze_service_graph(cur, "GET /health", NOW)

    assert result.get("endpoint_id") == "GET /health"
    assert result.get("service") is None   # aucune arete => service inconnu
    assert result.get("children") == []
    assert result.get("parents") == []
    assert result.get("root_cause_candidates") == []


# ---------------------------------------------------------------------------
# Test E : champ "status" dans children + champ "reason" dans candidates
# ---------------------------------------------------------------------------

def test_enriched_fields_status_and_reason():
    """
    Chaque entree de children doit avoir un champ "status" derive de la direction.
    Chaque entree de root_cause_candidates doit avoir un champ "reason".
    """
    graph_rows = [
        ("POST /api/payments", "POST /payments", "gateway", "payments", "direct"),
    ]
    anomaly_rows = [
        ("POST /payments", 0.75, "degradation"),
    ]
    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    child = result["children"][0]
    assert child.get("status") == "degraded"

    cand = result["root_cause_candidates"][0]
    assert "reason" in cand
    # La raison doit mentionner le call_type pour etre lisible
    assert "direct" in cand["reason"]


# ---------------------------------------------------------------------------
# Test F : champs top-level endpoint_id et service dans le resultat
# ---------------------------------------------------------------------------

def test_result_contains_top_level_endpoint_and_service():
    """
    Le resultat doit exposer endpoint_id et service au niveau racine du dict,
    pour que l'explainer puisse identifier le service de l'endpoint en alerte
    sans devoir parcourir les aretes.
    """
    graph_rows = [
        ("GET /api/orders", "GET /orders", "gateway", "orders", "direct"),
    ]
    anomaly_rows = []
    cur = _make_cursor(graph_rows, anomaly_rows)

    result = analyze_service_graph(cur, "GET /api/orders", NOW)

    assert result.get("endpoint_id") == "GET /api/orders"
    assert result.get("service") == "gateway"   # derive de parent_svc
    assert isinstance(result.get("children"), list)
    assert isinstance(result.get("parents"), list)
    assert isinstance(result.get("root_cause_candidates"), list)


# ---------------------------------------------------------------------------
# Test G : noeud intermediaire — partition parent/enfant correcte
# ---------------------------------------------------------------------------

def test_middle_node_correct_parent_child_partition():
    """
    POST /payments est un noeud intermediaire dans la chaine :
        POST /api/payments ──direct──► POST /payments ──cascade──► GET /orders/{order_id}

    Verifie que pour le noeud du milieu, la partition parent/enfant est correcte :
    POST /api/payments est parent, GET /orders/{order_id} est enfant.
    Seul l'enfant degrade figure dans root_cause_candidates, pas le parent.
    """
    graph_rows = [
        ("POST /api/payments", "POST /payments",         "gateway",  "payments", "direct"),
        ("POST /payments",     "GET /orders/{order_id}", "payments", "orders",   "cascade"),
    ]
    anomaly_rows = [
        ("POST /api/payments",     0.78, "degradation"),  # parent degrade aussi
        ("GET /orders/{order_id}", 0.91, "degradation"),  # enfant degrade
    ]
    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /payments", NOW)

    # Partition correcte : 1 enfant, 1 parent
    assert len(result["children"]) == 1
    assert result["children"][0]["endpoint_id"] == "GET /orders/{order_id}"
    assert len(result["parents"]) == 1
    assert result["parents"][0]["endpoint_id"] == "POST /api/payments"

    # Un seul candidat root-cause : l'enfant degrade (pas le parent, meme degrade)
    assert len(result["root_cause_candidates"]) == 1
    cand = result["root_cause_candidates"][0]
    assert cand["endpoint_id"] == "GET /orders/{order_id}"
    assert cand["call_type"] == "cascade"
    assert "cascade" in cand["reason"]


# ---------------------------------------------------------------------------
# Test H : degradations independantes — pas de fausse causalite
# ---------------------------------------------------------------------------

def test_confidence_score_cascade_lower_than_direct_same_anomaly():
    """
    Test K — a anomaly_score identique, la cascade a un confidence_score inferieur au direct.
    Garantit que le call_type influence la certitude causale, pas seulement le score d'anomalie.

    Cas de l'exemple utilisateur : anomaly_score=0.95 cascade -> confidence_score ~0.62 (medium).
    """
    graph_rows = [
        ("POST /api/payments", "POST /payments",         "gateway", "payments", "direct"),
        ("POST /api/payments", "GET /orders/{order_id}", "gateway", "orders",   "cascade"),
    ]
    anomaly_rows = [
        ("POST /payments",         0.90, "degradation"),
        ("GET /orders/{order_id}", 0.90, "degradation"),
    ]
    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    cands = {c["endpoint_id"]: c for c in result["root_cause_candidates"]}
    direct_cs  = cands["POST /payments"]["confidence_score"]
    cascade_cs = cands["GET /orders/{order_id}"]["confidence_score"]

    # Meme anomaly_score, mais direct doit avoir un confidence_score superieur
    assert direct_cs > cascade_cs

    # La cascade ne peut pas etre "high" meme avec un score d'anomalie eleve
    assert cands["GET /orders/{order_id}"]["confidence"] != "high"
    # Le direct avec score fort est "high"
    assert cands["POST /payments"]["confidence"] == "high"


def test_independent_degraded_endpoint_not_root_cause():
    """
    Deux endpoints degrades sans relation entre eux dans endpoint_relationships.
    analyze_service_graph ne doit pas creer de lien entre eux.

    Scenario : GET /api/orders et POST /payments sont degrades independamment.
    GET /api/orders n'a qu'une arete vers GET /orders (sain).
    POST /payments n'est pas un voisin de GET /api/orders -> ne doit pas apparaitre.
    """
    graph_rows = [
        ("GET /api/orders", "GET /orders", "gateway", "orders", "direct"),
    ]
    # GET /orders est sain ; POST /payments degrade mais aucune arete
    anomaly_rows = [
        ("GET /orders", 0.12, "normal"),
    ]
    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "GET /api/orders", NOW)

    all_neighbor_ids = (
        [c["endpoint_id"] for c in result["children"]] +
        [p["endpoint_id"] for p in result["parents"]]
    )
    # POST /payments ne doit pas apparaitre (aucune relation)
    assert "POST /payments" not in all_neighbor_ids
    # Aucun candidat : GET /orders est sain
    assert result["root_cause_candidates"] == []


# ---------------------------------------------------------------------------
# Test I : champ confidence dans root_cause_candidates
# ---------------------------------------------------------------------------

def test_candidate_has_confidence_score_field():
    """
    Test J — confidence_score present, valide (float dans [0.0, 0.95]).

    confidence_score != anomaly_score : le poids call_type est applique.
    direct + 0.82 -> confidence_score = 0.82 * 0.90 = 0.738 -> 0.74 (< 0.82).
    """
    graph_rows = [
        ("POST /api/payments", "POST /payments", "gateway", "payments", "direct"),
    ]
    anomaly_rows = [("POST /payments", 0.82, "degradation")]
    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    cand = result["root_cause_candidates"][0]
    assert "confidence_score" in cand
    cs = cand["confidence_score"]
    assert isinstance(cs, float)
    assert 0.0 <= cs <= 0.95
    # Le poids est applique : confidence_score < anomaly_score
    assert cs < cand["anomaly_score"]


def test_direct_high_score_candidate_has_confidence_high():
    """
    Dependance directe + score eleve -> confidence "high".
    Distinction fondamentale : anomaly_score mesure l'intensite de l'anomalie
    du voisin ; confidence mesure la certitude du lien causal.
    """
    graph_rows = [
        ("POST /api/payments", "POST /payments", "gateway", "payments", "direct"),
    ]
    anomaly_rows = [("POST /payments", 0.91, "degradation")]
    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    cand = result["root_cause_candidates"][0]
    assert "confidence" in cand
    assert cand["confidence"] in {"high", "medium", "low"}
    assert cand["confidence"] == "high"   # direct + 0.91 >= 0.75


def test_cascade_high_score_candidate_has_confidence_medium():
    """
    Dependance cascade + score eleve -> confidence "medium" (jamais "high").

    Meme avec anomaly_score = 0.95, une relation cascade reste une relation
    indirecte (appel interne en cascade). La certitude causale est moderee
    car une co-degradation n'implique pas une causalite directe.

    C'est l'exemple exact de la demande :
    anomaly_score = 0.95, confidence = "medium" (correct scientifiquement).
    """
    graph_rows = [
        ("POST /payments", "GET /orders/{order_id}", "payments", "orders", "cascade"),
    ]
    anomaly_rows = [("GET /orders/{order_id}", 0.95, "degradation")]
    cur = _make_cursor(graph_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /payments", NOW)

    cand = result["root_cause_candidates"][0]
    assert cand["confidence"] == "medium"   # cascade => jamais high, meme score eleve
    assert cand["anomaly_score"] == pytest.approx(0.95)
    # La raison mentionne le call_type et un vocabulaire d'incertitude SRE
    assert "cascade" in cand["reason"]
    assert any(w in cand["reason"] for w in ["non demontree", "non confirmee", "possible", "suspecte"])
