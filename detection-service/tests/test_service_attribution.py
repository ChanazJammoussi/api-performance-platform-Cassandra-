"""
test_service_attribution.py -- tests unitaires pour analyze_service_graph.

Verifie le comportement de l'analyse DAG dans correlator.py, independamment
du detecteur et de la base de donnees.

Format du mock (3 reponses sequentielles fetchall) :
  1. descendant_rows  : resultats de la CTE WITH RECURSIVE
                        (endpoint_id, service, call_type, depth, path, root_service)
  2. parent_rows      : parents 1-hop
                        (parent_endpoint_id, parent_service, child_service, call_type)
  3. anomaly_rows     : scores d'anomalie
                        (endpoint_id, score, direction)

Cas couverts (tests existants, format mis a jour) :
  1.  Aucun enfant degrade      -> root_cause_candidates vide
  2.  Enfant direct degrade     -> figure comme candidat root-cause
  3.  Enfants tous normaux      -> root_cause_candidates vide
  4.  Plusieurs enfants degrades -> trie par confidence_score DESC
  5.  Endpoint enfant           -> parents non vides, pas d'enfants
  6.  Direction "improvement"   -> exclue des candidats (spec §5.4)
  7.  Aretes manquantes         -> descendant non decouvert (comportement attendu)
  8.  Erreur DB                 -> retourne {} sans lever d'exception
  9.  Endpoint isole            -> structure complete avec listes vides
  10. Champ status              -> "degraded" / "improved" / "normal" / "unknown"
  11. Champ reason              -> present dans root_cause_candidates
  12. Champs top-level          -> endpoint_id et service dans le resultat
  13. Partition parent/enfant   -> noeud intermediaire classe correctement
  14. cascade < direct          -> confidence_score (meme anomaly_score, meme depth)
  15. Endpoint independant      -> pas de fausse causalite
  16. confidence_score field    -> valide [0.0, 0.95]
  17. direct high               -> confidence "high"
  18. cascade high score        -> confidence "medium" (jamais "high")

Nouveaux tests P2 :
  A. Multi-hop depth=2          -> GET /orders/{id} decouvert via POST /payments
  B. Protection cycles runtime  -> terminaison garantie meme avec mock potentiellement cyclique
  C. Ponderation profondeur     -> depth=1 > depth=2 par confidence_score
  D. direct depth=1 vs cascade depth=2 -> proximite influence confidence_score
  E. Tri par confidence_score   -> pas par anomaly_score (P2.3)

Tests P3 (durcissement) :
  F. DAG diamant                -> deduplication par endpoint_id, meilleur cs conserve
  G. Semantique call_type       -> call_type = derniere arete parcourue (documente)
"""

import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone

from correlator import analyze_service_graph, _rca_confidence_score

UTC = timezone.utc
NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


def _make_cursor(descendant_rows, parent_rows, anomaly_rows):
    """
    Curseur mock avec 3 reponses sequentielles fetchall() :
      1er  -> descendant_rows  (WITH RECURSIVE : endpoint_id, service, call_type,
                                 depth, path, root_service)
      2eme -> parent_rows      (parents 1-hop : parent_endpoint_id, parent_service,
                                 child_service, call_type)
      3eme -> anomaly_rows     (anomalies : endpoint_id, score, direction)
    """
    cur = MagicMock()
    cur.fetchall.side_effect = [descendant_rows, parent_rows, anomaly_rows]
    return cur


# ---------------------------------------------------------------------------
# Cas 1 : aucun enfant degrade
# ---------------------------------------------------------------------------

def test_no_degraded_children_returns_empty_candidates():
    descendant_rows = [
        ("POST /payments", "payments", "direct", 1,
         ["POST /api/payments", "POST /payments"], "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = []

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
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
    descendant_rows = [
        ("POST /payments", "payments", "direct", 1,
         ["POST /api/payments", "POST /payments"], "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = [("POST /payments", 0.82, "degradation")]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
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
    descendant_rows = [
        ("POST /payments", "payments", "direct", 1,
         ["POST /api/payments", "POST /payments"], "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = [("POST /payments", 0.20, "normal")]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    assert result["root_cause_candidates"] == []
    assert result["children"][0]["direction"] == "normal"


# ---------------------------------------------------------------------------
# Cas 4 : deux enfants degrades -> trie par confidence_score DESC
# Pour depth=1 direct : confidence_score = anomaly_score * 1.0 * 1.0 = anomaly_score
# Donc le tri par confidence_score est identique au tri par anomaly_score ici.
# ---------------------------------------------------------------------------

def test_multiple_degraded_children_ranked_by_confidence_score():
    descendant_rows = [
        ("GET /orders/{order_id}", "orders",   "direct", 1,
         ["POST /api/payments", "GET /orders/{order_id}"], "gateway"),
        ("POST /payments",         "payments", "direct", 1,
         ["POST /api/payments", "POST /payments"],         "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = [
        ("POST /payments",         0.45, "degradation"),
        ("GET /orders/{order_id}", 0.87, "degradation"),
    ]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    candidates = result["root_cause_candidates"]
    assert len(candidates) == 2
    # Trie par confidence_score DESC (= anomaly_score pour depth=1 direct)
    assert candidates[0]["endpoint_id"] == "GET /orders/{order_id}"
    assert candidates[0]["anomaly_score"] == pytest.approx(0.87)
    assert candidates[1]["endpoint_id"] == "POST /payments"
    assert candidates[1]["anomaly_score"] == pytest.approx(0.45)
    assert candidates[0]["confidence_score"] > candidates[1]["confidence_score"]


# ---------------------------------------------------------------------------
# Cas 5 : endpoint enfant (parents non vides, pas d'enfants)
# ---------------------------------------------------------------------------

def test_child_endpoint_has_parents_no_children():
    """GET /orders/{order_id} est appele par deux parents ; lui-meme n'appelle rien."""
    descendant_rows = []  # GET /orders/{order_id} n'a pas d'enfants
    parent_rows = [
        ("GET /api/orders/{order_id}", "gateway",  "orders", "direct"),
        ("POST /payments",             "payments", "orders", "cascade"),
    ]
    anomaly_rows = []

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "GET /orders/{order_id}", NOW)

    assert result["children"] == []
    assert result["root_cause_candidates"] == []
    parent_ids = {p["endpoint_id"] for p in result["parents"]}
    assert "GET /api/orders/{order_id}" in parent_ids
    assert "POST /payments" in parent_ids


# ---------------------------------------------------------------------------
# Cas 6 : direction "improvement" exclue des candidats (spec §5.4)
# ---------------------------------------------------------------------------

def test_improvement_direction_excluded_from_candidates():
    """
    Une anomalie avec direction="improvement" (perf anormalement bonne) ne doit
    jamais figurer dans root_cause_candidates.
    """
    descendant_rows = [
        ("GET /orders/{order_id}", "orders", "cascade", 1,
         ["POST /payments", "GET /orders/{order_id}"], "payments"),
    ]
    parent_rows  = []
    anomaly_rows = [("GET /orders/{order_id}", 0.90, "improvement")]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /payments", NOW)

    assert result["root_cause_candidates"] == []
    assert len(result["children"]) == 1
    assert result["children"][0]["direction"] == "improvement"
    assert result["children"][0]["anomaly_score"] == pytest.approx(0.90)


# ---------------------------------------------------------------------------
# Cas 7 : aretes manquantes -> descendant non decouvert (comportement attendu)
# ---------------------------------------------------------------------------

def test_deep_cause_only_visible_via_graph_edges():
    """
    Un descendant n'est visible que si ses aretes figurent dans le resultat
    de la requete WITH RECURSIVE (controlees ici par le mock).

    La traversee multi-hop ne cree pas d'aretes manquantes -- elle traverse
    uniquement les aretes qui existent dans endpoint_relationships.

    Si l'arete POST /payments -> GET /orders/{id} est absente du mock
    (comme si elle n'existait pas en DB), GET /orders/{id} n'est pas decouvert.
    """
    descendant_rows = [
        ("POST /payments", "payments", "direct", 1,
         ["POST /api/payments", "POST /payments"], "gateway"),
        # L'arete POST /payments -> GET /orders/{id} est absente du mock
    ]
    parent_rows  = []
    anomaly_rows = []

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    all_candidate_ids = [c["endpoint_id"] for c in result["root_cause_candidates"]]
    assert "GET /orders/{order_id}" not in all_candidate_ids
    assert len(result["children"]) == 1
    assert result["children"][0]["endpoint_id"] == "POST /payments"
    assert result["root_cause_candidates"] == []


# ---------------------------------------------------------------------------
# Cas 8 : erreur DB -> retourne {} sans lever d'exception
# ---------------------------------------------------------------------------

def test_db_error_returns_empty_dict():
    """
    Si endpoint_relationships est inaccessible, analyze_service_graph doit retourner {}
    sans lever d'exception. Best-effort : l'alerte ne doit jamais etre bloquee ici.
    """
    cur = MagicMock()
    cur.execute.side_effect = Exception("relation endpoint_relationships does not exist")

    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    assert result == {}


# ---------------------------------------------------------------------------
# Cas 9 : endpoint isole (aucune arete) -> structure complete avec listes vides
# ---------------------------------------------------------------------------

def test_isolated_endpoint_returns_complete_empty_structure():
    """
    Un endpoint sans aucune relation dans endpoint_relationships (ex: /health)
    doit retourner une structure valide avec des listes vides, pas {}.
    """
    cur = _make_cursor([], [], [])

    result = analyze_service_graph(cur, "GET /health", NOW)

    assert result.get("endpoint_id") == "GET /health"
    assert result.get("service") is None
    assert result.get("children") == []
    assert result.get("parents") == []
    assert result.get("root_cause_candidates") == []


# ---------------------------------------------------------------------------
# Cas 10+11 : champs "status" + "reason" enrichis
# ---------------------------------------------------------------------------

def test_enriched_fields_status_and_reason():
    """
    Chaque entree de children doit avoir un champ "status" derive de la direction.
    Chaque entree de root_cause_candidates doit avoir un champ "reason".
    """
    descendant_rows = [
        ("POST /payments", "payments", "direct", 1,
         ["POST /api/payments", "POST /payments"], "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = [("POST /payments", 0.75, "degradation")]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    child = result["children"][0]
    assert child.get("status") == "degraded"

    cand = result["root_cause_candidates"][0]
    assert "reason" in cand
    assert "direct" in cand["reason"]


# ---------------------------------------------------------------------------
# Cas 12 : champs top-level endpoint_id et service dans le resultat
# ---------------------------------------------------------------------------

def test_result_contains_top_level_endpoint_and_service():
    """
    Le resultat doit exposer endpoint_id et service au niveau racine du dict.
    """
    descendant_rows = [
        ("GET /orders", "orders", "direct", 1,
         ["GET /api/orders", "GET /orders"], "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = []

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "GET /api/orders", NOW)

    assert result.get("endpoint_id") == "GET /api/orders"
    assert result.get("service") == "gateway"  # root_service derive du 1er descendant
    assert isinstance(result.get("children"), list)
    assert isinstance(result.get("parents"), list)
    assert isinstance(result.get("root_cause_candidates"), list)


# ---------------------------------------------------------------------------
# Cas 13 : noeud intermediaire -- partition parent/enfant correcte
# ---------------------------------------------------------------------------

def test_middle_node_correct_parent_child_partition():
    """
    POST /payments est un noeud intermediaire :
        POST /api/payments --direct--> POST /payments --cascade--> GET /orders/{order_id}

    Pour le noeud du milieu, la partition parent/enfant doit etre correcte :
    POST /api/payments est parent, GET /orders/{order_id} est enfant.
    Seul l'enfant degrade figure dans root_cause_candidates.
    """
    descendant_rows = [
        ("GET /orders/{order_id}", "orders", "cascade", 1,
         ["POST /payments", "GET /orders/{order_id}"], "payments"),
    ]
    parent_rows = [
        ("POST /api/payments", "gateway", "payments", "direct"),
    ]
    anomaly_rows = [
        ("POST /api/payments",     0.78, "degradation"),
        ("GET /orders/{order_id}", 0.91, "degradation"),
    ]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /payments", NOW)

    assert len(result["children"]) == 1
    assert result["children"][0]["endpoint_id"] == "GET /orders/{order_id}"
    assert len(result["parents"]) == 1
    assert result["parents"][0]["endpoint_id"] == "POST /api/payments"

    assert len(result["root_cause_candidates"]) == 1
    cand = result["root_cause_candidates"][0]
    assert cand["endpoint_id"] == "GET /orders/{order_id}"
    assert cand["call_type"] == "cascade"
    assert "cascade" in cand["reason"]


# ---------------------------------------------------------------------------
# Cas 14 : cascade < direct (meme anomaly_score, meme depth)
# ---------------------------------------------------------------------------

def test_confidence_score_cascade_lower_than_direct_same_anomaly():
    """
    A anomaly_score identique et depth=1, cascade a un confidence_score inferieur au direct.
    Garantit que le call_type influence la certitude causale.

    direct  : 0.90 * 1.0 * 1.0 = 0.90 -> high
    cascade : 0.90 * 0.7 * 1.0 = 0.63 -> medium (< seuil high 0.70)
    """
    descendant_rows = [
        ("GET /orders/{order_id}", "orders",   "cascade", 1,
         ["POST /api/payments", "GET /orders/{order_id}"], "gateway"),
        ("POST /payments",         "payments", "direct",  1,
         ["POST /api/payments", "POST /payments"],         "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = [
        ("POST /payments",         0.90, "degradation"),
        ("GET /orders/{order_id}", 0.90, "degradation"),
    ]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    cands = {c["endpoint_id"]: c for c in result["root_cause_candidates"]}
    direct_cs  = cands["POST /payments"]["confidence_score"]           # 0.90
    cascade_cs = cands["GET /orders/{order_id}"]["confidence_score"]   # 0.63

    assert direct_cs > cascade_cs
    assert cands["GET /orders/{order_id}"]["confidence"] != "high"  # 0.63 < 0.70
    assert cands["POST /payments"]["confidence"] == "high"           # 0.90 >= 0.70


# ---------------------------------------------------------------------------
# Cas 15 : endpoint independant -> pas de fausse causalite
# ---------------------------------------------------------------------------

def test_independent_degraded_endpoint_not_root_cause():
    """
    Deux endpoints degrades sans relation entre eux dans endpoint_relationships.
    analyze_service_graph ne doit pas creer de lien entre eux.
    """
    descendant_rows = [
        ("GET /orders", "orders", "direct", 1,
         ["GET /api/orders", "GET /orders"], "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = [("GET /orders", 0.12, "normal")]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "GET /api/orders", NOW)

    all_neighbor_ids = (
        [c["endpoint_id"] for c in result["children"]] +
        [p["endpoint_id"] for p in result["parents"]]
    )
    assert "POST /payments" not in all_neighbor_ids
    assert result["root_cause_candidates"] == []


# ---------------------------------------------------------------------------
# Cas 16 : champ confidence_score valide [0.0, 0.95]
# ---------------------------------------------------------------------------

def test_candidate_has_confidence_score_field():
    """
    confidence_score present et valide ([0.0, 0.95]).

    direct depth=1 + score=0.82 : cs = min(0.95, 0.82 * 1.0 * 1.0) = 0.82
    Le poids call_type direct=1.0 et depth_weight=1.0 ne reduisent pas le score ;
    le plafond 0.95 s'applique uniquement si anomaly_score > 0.95.
    """
    descendant_rows = [
        ("POST /payments", "payments", "direct", 1,
         ["POST /api/payments", "POST /payments"], "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = [("POST /payments", 0.82, "degradation")]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    cand = result["root_cause_candidates"][0]
    assert "confidence_score" in cand
    cs = cand["confidence_score"]
    assert isinstance(cs, float)
    assert 0.0 <= cs <= 0.95
    # direct + depth=1 : cs = anomaly_score * 1.0 * 1.0 = anomaly_score (pas de reduction)
    assert cs == pytest.approx(cand["anomaly_score"])


# ---------------------------------------------------------------------------
# Cas 17 : direct + score eleve -> confidence "high"
# ---------------------------------------------------------------------------

def test_direct_high_score_candidate_has_confidence_high():
    """direct + depth=1 + 0.91 -> cs = 0.91 >= 0.70 -> high"""
    descendant_rows = [
        ("POST /payments", "payments", "direct", 1,
         ["POST /api/payments", "POST /payments"], "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = [("POST /payments", 0.91, "degradation")]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    cand = result["root_cause_candidates"][0]
    assert "confidence" in cand
    assert cand["confidence"] in {"high", "medium", "low"}
    assert cand["confidence"] == "high"


# ---------------------------------------------------------------------------
# Cas 18 : cascade + score eleve -> confidence "medium" (jamais "high")
# ---------------------------------------------------------------------------

def test_cascade_high_score_candidate_has_confidence_medium():
    """
    cascade + score eleve -> confidence "medium" (jamais "high").

    Avec cascade=0.70 et depth=1 : cs = min(0.95, 0.95 * 0.70 * 1.0) = 0.665
    Seuil high = 0.70 : 0.665 < 0.70 -> medium.

    Invariant : cascade ne peut jamais atteindre "high"
    (max cascade = 0.95 * 0.70 = 0.665 < 0.70).
    """
    descendant_rows = [
        ("GET /orders/{order_id}", "orders", "cascade", 1,
         ["POST /payments", "GET /orders/{order_id}"], "payments"),
    ]
    parent_rows  = []
    anomaly_rows = [("GET /orders/{order_id}", 0.95, "degradation")]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /payments", NOW)

    cand = result["root_cause_candidates"][0]
    assert cand["confidence"] == "medium"
    assert cand["anomaly_score"] == pytest.approx(0.95)
    assert "cascade" in cand["reason"]
    assert any(w in cand["reason"] for w in ["non demontree", "non confirmee", "possible", "suspecte"])


# ===========================================================================
# Nouveaux tests P2
# ===========================================================================

# ---------------------------------------------------------------------------
# Test A : traversee multi-hop -- GET /orders/{id} decouvert a depth=2
# ---------------------------------------------------------------------------

def test_multihop_depth2_candidate_discovered():
    """
    Traversee multi-hop : GET /orders/{id} decouvert a depth=2.

    Graphe :
        POST /api/payments --direct--> POST /payments --cascade--> GET /orders/{id}

    GET /orders/{id} est en degradation. Avec la traversee multi-hop,
    il doit apparaitre dans root_cause_candidates avec depth=2.

    cascade depth=2 + score=0.91 : cs = 0.91 * 0.7 * 0.5 = 0.3185 -> low
    """
    descendant_rows = [
        ("POST /payments",         "payments", "direct",  1,
         ["POST /api/payments", "POST /payments"], "gateway"),
        ("GET /orders/{order_id}", "orders",   "cascade", 2,
         ["POST /api/payments", "POST /payments", "GET /orders/{order_id}"], "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = [
        ("GET /orders/{order_id}", 0.91, "degradation"),
    ]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    candidate_ids = [c["endpoint_id"] for c in result["root_cause_candidates"]]
    assert "GET /orders/{order_id}" in candidate_ids

    cand = next(c for c in result["root_cause_candidates"]
                if c["endpoint_id"] == "GET /orders/{order_id}")
    assert cand["depth"] == 2
    assert cand["path"] == ["POST /api/payments", "POST /payments", "GET /orders/{order_id}"]
    assert cand["confidence_score"] == pytest.approx(0.32, abs=0.01)
    assert cand["confidence"] == "low"   # cascade + depth=2 -> toujours low

    # children ne contient que les descendants directs (depth=1)
    child_ids = [c["endpoint_id"] for c in result["children"]]
    assert "POST /payments" in child_ids
    assert "GET /orders/{order_id}" not in child_ids


# ---------------------------------------------------------------------------
# Test B : protection cycles runtime -- terminaison garantie
# ---------------------------------------------------------------------------

def test_cycle_protection_no_infinite_loop():
    """
    Protection anti-cycles runtime.

    La requete WITH RECURSIVE est bornee par :
      1. NOT (child_endpoint_id = ANY(path)) dans le CTE
      2. depth < MAX_GRAPH_DEPTH

    Ce test verifie qu'analyze_service_graph termine et ne plante pas
    meme si le mock retourne des donnees denses representant plusieurs niveaux.
    La double protection (SQL + borne) garantit la terminaison.
    """
    # Simuler ce que retournerait la CTE apres avoir decouvert deux niveaux.
    # En production, le trigger trg_no_cycle interdit les cycles a la DB.
    descendant_rows = [
        ("POST /payments",         "payments", "direct",  1,
         ["POST /api/payments", "POST /payments"],                           "gateway"),
        ("GET /orders/{order_id}", "orders",   "cascade", 2,
         ["POST /api/payments", "POST /payments", "GET /orders/{order_id}"], "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = []

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    assert isinstance(result, dict)
    assert "root_cause_candidates" in result
    # 2 descendants decouverts, aucun degrade -> candidates vide
    assert len(result["children"]) == 1  # depth=1 uniquement dans children
    assert result["root_cause_candidates"] == []


# ---------------------------------------------------------------------------
# Test C : ponderation profondeur (depth_weight = 1/depth)
# ---------------------------------------------------------------------------

def test_depth_weighting_reduces_confidence_score():
    """
    Meme anomaly_score, meme call_type : depth=1 > depth=2 par confidence_score.

    depth_weight = 1/depth penalise les causes distantes.
    direct depth=1 score=0.90 : 0.90 * 1.0 * 1.0 = 0.90
    direct depth=2 score=0.90 : 0.90 * 1.0 * 0.5 = 0.45
    """
    cs_d1 = _rca_confidence_score(0.90, "direct", depth=1)
    cs_d2 = _rca_confidence_score(0.90, "direct", depth=2)

    assert cs_d1 > cs_d2
    assert cs_d1 == pytest.approx(0.90)
    assert cs_d2 == pytest.approx(0.45)


# ---------------------------------------------------------------------------
# Test D : direct depth=1 vs cascade depth=2
# ---------------------------------------------------------------------------

def test_direct_depth1_greater_than_cascade_depth2():
    """
    La proximite (direct depth=1) l'emporte sur la cascade distante (cascade depth=2),
    meme si l'anomaly_score est identique.

    direct  depth=1 score=0.90 : 0.90 * 1.0 * 1.0 = 0.90
    cascade depth=2 score=0.90 : 0.90 * 0.7 * 0.5 = 0.315
    """
    cs_direct_d1  = _rca_confidence_score(0.90, "direct",  depth=1)
    cs_cascade_d2 = _rca_confidence_score(0.90, "cascade", depth=2)

    assert cs_direct_d1 > cs_cascade_d2
    assert cs_direct_d1  == pytest.approx(0.90)
    assert cs_cascade_d2 == pytest.approx(0.32)   # round(0.315, 2) -> 0.32


# ---------------------------------------------------------------------------
# Test E : tri par confidence_score DESC (pas anomaly_score)
# ---------------------------------------------------------------------------

def test_candidates_sorted_by_confidence_score_not_anomaly_score():
    """
    Les root_cause_candidates sont tries par confidence_score DESC, pas par anomaly_score.

    Candidat A : direct depth=1  anomaly_score=0.50 -> cs = 0.50
    Candidat B : cascade depth=2 anomaly_score=0.90 -> cs = 0.90 * 0.7 * 0.5 = 0.32

    Tri par confidence_score : A (0.50) avant B (0.32),
    meme si B a un anomaly_score plus eleve.
    Ce tri favorise les causes proches et directes.
    """
    descendant_rows = [
        ("endpoint_A", "svc_a", "direct",  1,
         ["POST /api/payments", "endpoint_A"],              "gateway"),
        ("endpoint_B", "svc_b", "cascade", 2,
         ["POST /api/payments", "endpoint_A", "endpoint_B"], "gateway"),
    ]
    parent_rows  = []
    anomaly_rows = [
        ("endpoint_A", 0.50, "degradation"),
        ("endpoint_B", 0.90, "degradation"),
    ]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "POST /api/payments", NOW)

    candidates = result["root_cause_candidates"]
    assert len(candidates) == 2

    # endpoint_A (cs=0.50) avant endpoint_B (cs=0.32)
    assert candidates[0]["endpoint_id"] == "endpoint_A"
    assert candidates[1]["endpoint_id"] == "endpoint_B"

    # Verifie l'inversion : tri confidence_score != tri anomaly_score
    assert candidates[0]["confidence_score"] > candidates[1]["confidence_score"]
    assert candidates[0]["anomaly_score"] < candidates[1]["anomaly_score"]


# ===========================================================================
# Tests P3 : durcissement DAG diamant + semantique call_type
# ===========================================================================

# ---------------------------------------------------------------------------
# Test F : DAG diamant -- deduplication par endpoint_id
# ---------------------------------------------------------------------------

def test_diamond_dag_no_duplicate_candidates():
    """
    DAG en diamant : leaf est atteignable depuis endpoint_root via deux chemins.

    Graphe :
        endpoint_root --direct-->  mid_A --direct-->  leaf
        endpoint_root --cascade--> mid_B --cascade--> leaf

    La CTE WITH RECURSIVE retourne leaf deux fois (chemins distincts = tuples distincts).
    analyze_service_graph doit deduplication par endpoint_id et conserver
    la meilleure entree (confidence_score le plus eleve) :
      direct  depth=2 : cs = 0.90 * 1.0 * 0.5 = 0.45
      cascade depth=2 : cs = 0.90 * 0.7 * 0.5 = 0.32
    -> leaf conserve avec call_type="direct", cs=0.45.
    """
    descendant_rows = [
        ("mid_A", "svc_a", "direct",  1, ["endpoint_root", "mid_A"],        "root_svc"),
        ("mid_B", "svc_b", "cascade", 1, ["endpoint_root", "mid_B"],        "root_svc"),
        ("leaf",  "svc_c", "direct",  2, ["endpoint_root", "mid_A", "leaf"], "root_svc"),
        ("leaf",  "svc_c", "cascade", 2, ["endpoint_root", "mid_B", "leaf"], "root_svc"),
    ]
    parent_rows  = []
    anomaly_rows = [
        ("mid_A", 0.80, "degradation"),
        ("mid_B", 0.80, "degradation"),
        ("leaf",  0.90, "degradation"),
    ]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "endpoint_root", NOW)

    candidates = result["root_cause_candidates"]

    # leaf doit apparaitre UNE SEULE FOIS (deduplication DAG diamant)
    leaf_candidates = [c for c in candidates if c["endpoint_id"] == "leaf"]
    assert len(leaf_candidates) == 1

    # Meilleure entree conservee : direct depth=2 (cs=0.45 > cascade depth=2 cs=0.32)
    leaf = leaf_candidates[0]
    assert leaf["call_type"] == "direct"
    assert leaf["confidence_score"] == pytest.approx(0.45)

    # Les deux mid_* sont aussi des candidats (chacun unique)
    candidate_ids = [c["endpoint_id"] for c in candidates]
    assert "mid_A" in candidate_ids
    assert "mid_B" in candidate_ids
    assert len(candidates) == 3  # mid_A, mid_B, leaf (pas de doublon)


# ---------------------------------------------------------------------------
# Test G : semantique call_type = derniere arete du chemin parcouru
# ---------------------------------------------------------------------------

def test_call_type_is_last_hop_edge():
    """
    call_type represente le type de la DERNIERE arete parcourue vers ce noeud,
    pas un resume du chemin complet.

    Chemin : root --direct--> mid --cascade--> leaf
      mid  : call_type = "direct"  (arete root -> mid)
      leaf : call_type = "cascade" (arete mid -> leaf, la derniere)

    Ce comportement est inherent a la CTE WITH RECURSIVE :
    er.call_type provient de l'arete courante, pas de la chaine.
    Il est documente et voulu : la formule confidence_score penalise
    la derniere arete pour mesurer le type de lien immediatement avant le candidat.
    """
    descendant_rows = [
        ("mid",  "svc_b", "direct",  1, ["root", "mid"],        "svc_a"),
        ("leaf", "svc_c", "cascade", 2, ["root", "mid", "leaf"], "svc_a"),
    ]
    parent_rows  = []
    anomaly_rows = [
        ("mid",  0.85, "degradation"),
        ("leaf", 0.85, "degradation"),
    ]

    cur = _make_cursor(descendant_rows, parent_rows, anomaly_rows)
    result = analyze_service_graph(cur, "root", NOW)

    cands = {c["endpoint_id"]: c for c in result["root_cause_candidates"]}

    # mid : arete directe root->mid
    assert cands["mid"]["call_type"] == "direct"
    assert cands["mid"]["depth"] == 1

    # leaf : derniere arete = cascade (mid->leaf)
    assert cands["leaf"]["call_type"] == "cascade"
    assert cands["leaf"]["depth"] == 2

    # Scoring coheVrent avec call_type de la derniere arete
    assert cands["leaf"]["confidence_score"] == pytest.approx(
        _rca_confidence_score(0.85, "cascade", depth=2)
    )
