"""
correlator.py -- lie les alertes FIRING aux injections de fautes connues.

Appelé par detector.py à chaque transition OK -> FIRING.

Pipeline :
  1. Scan  : lit tous les fichiers scenario-runner/results/*.json
  2. Parse : extrait injected_at, cleared_at, target_service, target_endpoint
  3. Filter: garde les injections dont la fenetre [injected_at-30min, cleared_at+30min]
             chevauche alert.opened_at
  4. Match : prefere les injections dont le service ou l'endpoint correspond
  5. Score : imputation_score = f(distance temporelle, qualite du match)
  6. Write : met a jour alerts.suspected_fault et alerts.imputation_score
"""

import glob
import json
import logging
import math
import os
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# Fenetre causale : on cherche des injections dans [onset - CAUSAL_WINDOW, onset + CAUSAL_WINDOW]
CAUSAL_WINDOW_MINUTES = 30

# Profondeur maximale de traversee du DAG endpoint_relationships.
# Configurable via MAX_GRAPH_DEPTH pour ajuster le rayon RCA sans modifier le code.
MAX_GRAPH_DEPTH = int(os.environ.get("MAX_GRAPH_DEPTH", "3"))

# Repertoire des ground truth JSON, resolu par rapport a l'emplacement de ce
# fichier (et non au CWD). Surcharges possible via GROUND_TRUTH_DIR.
RESULTS_DIR = os.environ.get(
    "GROUND_TRUTH_DIR",
    os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "scenario-runner", "results"
    ))
)


def _load_all_injections():
    """
    Charge et parse tous les fichiers *.json dans RESULTS_DIR.
    Retourne une liste de dicts normalises.
    """
    injections = []
    pattern = os.path.join(RESULTS_DIR, "**", "*.json")
    for path in glob.glob(pattern, recursive=True):
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, list):
                raw_faults = data
            elif isinstance(data, dict) and "faults" in data:
                raw_faults = data["faults"]
            else:
                continue
            for entry in raw_faults:
                injected_at = entry.get("injected_at")
                cleared_at  = entry.get("cleared_at")
                if not injected_at or not cleared_at:
                    continue
                injections.append({
                    "scenario_id":     entry.get("scenario_id", "unknown"),
                    "fault_type":      entry.get("fault_type", "unknown"),
                    "target_service":  entry.get("target_service", ""),
                    "target_endpoint": entry.get("target_endpoint", ""),
                    "injected_at":     datetime.fromisoformat(injected_at),
                    "cleared_at":      datetime.fromisoformat(cleared_at),
                    "magnitude":       entry.get("magnitude", {}),
                    "source_file":     os.path.basename(path),
                })
        except Exception as e:
            log.warning(f"Could not load ground truth file {path}: {e}")
    return injections


def _normalize_endpoint(endpoint_id: str) -> str:
    """
    Normalise un endpoint_id pour la comparaison :
    'POST /api/payments' -> 'post /api/payments'
    """
    return endpoint_id.strip().lower()


def _service_matches(injection: dict, alert_endpoint_id: str) -> bool:
    """
    Verifie si l'injection concerne le meme service/endpoint que l'alerte.
    Strategies par ordre de precision :
      1. Match exact sur target_endpoint
      2. Match partiel : le route de l'injection est contenu dans l'endpoint_id
      3. Match sur le service name contenu dans l'endpoint_id
    """
    norm_alert    = _normalize_endpoint(alert_endpoint_id)
    norm_target   = _normalize_endpoint(injection["target_endpoint"])
    target_service = injection["target_service"].lower()

    if norm_target == norm_alert:
        return True
    if norm_target and norm_target in norm_alert:
        return True
    if norm_alert and norm_alert in norm_target:
        return True
    if target_service and target_service in norm_alert:
        return True
    return False


def _compute_imputation_score(injection: dict, onset: datetime) -> float:
    """
    Score dans [0, 1] base sur la distance temporelle entre onset et la fenetre d'injection.

    - Si onset est dans [injected_at, cleared_at]        : score = 1.0
    - Si onset est juste avant injected_at (jusqu'a 30min) : decroissance lineaire vers 0
    - Si onset est juste apres cleared_at (jusqu'a 30min)  : decroissance lineaire vers 0
    - Au-dela de CAUSAL_WINDOW_MINUTES                     : score = 0.0
    """
    injected = injection["injected_at"]
    cleared  = injection["cleared_at"]
    window   = timedelta(minutes=CAUSAL_WINDOW_MINUTES)

    if injected <= onset <= cleared:
        return 1.0

    if onset < injected:
        distance = (injected - onset).total_seconds()
    else:
        distance = (onset - cleared).total_seconds()

    max_distance = window.total_seconds()
    if distance >= max_distance:
        return 0.0

    # Decroissance lineaire
    return 1.0 - (distance / max_distance)


def correlate(alert_endpoint_id: str, onset: datetime) -> dict | None:
    """
    Point d'entree principal. Retourne le meilleur match ou None.

    Retour :
    {
        "scenario_id":      str,
        "fault_type":       str,
        "target_endpoint":  str,
        "magnitude":        dict,
        "imputation_score": float,   # [0, 1]
        "service_match":    bool,
        "source_file":      str,
    }
    """
    if not isinstance(onset, datetime):
        log.error(f"correlate() called with invalid onset type: {type(onset)}")
        return None

    # S'assurer que onset est timezone-aware
    if onset.tzinfo is None:
        onset = onset.replace(tzinfo=timezone.utc)

    injections = _load_all_injections()
    window = timedelta(minutes=CAUSAL_WINDOW_MINUTES)
    candidates = []

    for inj in injections:
        injected = inj["injected_at"]
        cleared  = inj["cleared_at"]

        # S'assurer que les timestamps sont timezone-aware
        if injected.tzinfo is None:
            injected = injected.replace(tzinfo=timezone.utc)
            inj["injected_at"] = injected
        if cleared.tzinfo is None:
            cleared = cleared.replace(tzinfo=timezone.utc)
            inj["cleared_at"] = cleared

        # Filter : onset doit etre dans la fenetre elargie
        if not (injected - window <= onset <= cleared + window):
            continue

        score         = _compute_imputation_score(inj, onset)
        service_match = _service_matches(inj, alert_endpoint_id)

        # Bonus de 20% si le service/endpoint correspond
        adjusted_score = min(1.0, score * 1.2) if service_match else score

        if adjusted_score > 0.0:
            candidates.append({
                "scenario_id":      inj["scenario_id"],
                "fault_type":       inj["fault_type"],
                "target_endpoint":  inj["target_endpoint"],
                "magnitude":        inj["magnitude"],
                "imputation_score": round(adjusted_score, 4),
                "service_match":    service_match,
                "source_file":      inj["source_file"],
            })

    if not candidates:
        return None

    # Meilleur candidat : score le plus eleve, puis service_match en tie-breaker
    best = max(candidates, key=lambda c: (c["imputation_score"], c["service_match"]))
    return best


# ---------------------------------------------------------------------------
# Correlation deploiement (control plane : table deploy_events)
# ---------------------------------------------------------------------------

# Un deploiement CAUSE une regression : il precede l'onset de l'alerte. On
# cherche donc les deploys dans [onset - DEPLOY_CAUSAL_WINDOW, onset].
DEPLOY_CAUSAL_WINDOW_MINUTES = 30


def _get_service_for_endpoint(cur, endpoint_id: str) -> str | None:
    """Deduit le service d'un endpoint_id via la derniere ligne endpoint_features."""
    cur.execute("""
        SELECT service FROM endpoint_features
        WHERE endpoint_id = %s
        ORDER BY time DESC
        LIMIT 1
    """, (endpoint_id,))
    row = cur.fetchone()
    return row[0] if row else None


def correlate_deploy(cur, endpoint_id: str, onset: datetime) -> dict | None:
    """
    Cherche un deploiement recent susceptible d'avoir cause la regression.

    Retour (ou None) :
    {
        "deploy_id":        str,
        "service":          str,
        "version":          str,
        "deployed_at":      datetime,
        "imputation_score": float,   # [0, 1], decroissance avec la distance temporelle
        "service_match":    bool,
    }
    """
    if onset.tzinfo is None:
        onset = onset.replace(tzinfo=timezone.utc)

    window = timedelta(minutes=DEPLOY_CAUSAL_WINDOW_MINUTES)
    since = onset - window
    alert_service = _get_service_for_endpoint(cur, endpoint_id)

    # Deploys dans la fenetre causale (deployes AVANT l'onset).
    cur.execute("""
        SELECT deploy_id, service, version, deployed_at
        FROM deploy_events
        WHERE deployed_at BETWEEN %s AND %s
        ORDER BY deployed_at DESC
    """, (since, onset))
    rows = cur.fetchall()
    if not rows:
        return None

    window_seconds = window.total_seconds()
    candidates = []
    for deploy_id, service, version, deployed_at in rows:
        if deployed_at.tzinfo is None:
            deployed_at = deployed_at.replace(tzinfo=timezone.utc)
        distance = (onset - deployed_at).total_seconds()
        if distance < 0:
            continue  # deploy posterieur a l'onset : pas causal
        score = max(0.0, 1.0 - distance / window_seconds)
        service_match = bool(alert_service) and service == alert_service
        adjusted = min(1.0, score * 1.2) if service_match else score
        if adjusted > 0.0:
            candidates.append({
                "deploy_id":        str(deploy_id),
                "service":          service,
                "version":          version,
                "deployed_at":      deployed_at,
                "imputation_score": round(adjusted, 4),
                "service_match":    service_match,
            })

    if not candidates:
        return None

    return max(candidates, key=lambda c: (c["imputation_score"], c["service_match"]))


def write_deploy_correlation(cur, endpoint_id: str, signal_type: str, result: dict | None):
    """Ecrit le deploiement suspecte dans alerts.suspected_deploy_id."""
    if result is None:
        cur.execute("""
            UPDATE alerts SET suspected_deploy_id = NULL
            WHERE endpoint_id = %s AND signal_type = %s
        """, (endpoint_id, signal_type))
        log.info(f"Deploy correlation [{endpoint_id}/{signal_type}]: no deploy in causal window")
    else:
        cur.execute("""
            UPDATE alerts SET suspected_deploy_id = %s
            WHERE endpoint_id = %s AND signal_type = %s
        """, (result["deploy_id"], endpoint_id, signal_type))
        log.info(
            f"Deploy correlation [{endpoint_id}/{signal_type}]: "
            f"matched {result['service']} {result['version']} "
            f"score={result['imputation_score']:.3f} service_match={result['service_match']}"
        )


def write_correlation(cur, endpoint_id: str, signal_type: str, result: dict | None):
    """
    Ecrit le resultat de correlation dans la table alerts.
    Ajoute les colonnes si elles n'existent pas encore.
    """
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS suspected_fault TEXT")
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS imputation_score DOUBLE PRECISION")

    if result is None:
        cur.execute("""
            UPDATE alerts
            SET suspected_fault   = NULL,
                imputation_score  = 0.0
            WHERE endpoint_id = %s AND signal_type = %s
        """, (endpoint_id, signal_type))
        log.info(f"Correlation [{endpoint_id}/{signal_type}]: no match found")
    else:
        suspected = f"{result['scenario_id']}:{result['fault_type']}:{result['target_endpoint']}"
        cur.execute("""
            UPDATE alerts
            SET suspected_fault   = %s,
                imputation_score  = %s
            WHERE endpoint_id = %s AND signal_type = %s
        """, (suspected, result["imputation_score"], endpoint_id, signal_type))
        log.info(
            f"Correlation [{endpoint_id}/{signal_type}]: "
            f"matched '{suspected}' "
            f"score={result['imputation_score']:.3f} "
            f"service_match={result['service_match']}"
        )


# ---------------------------------------------------------------------------
# Analyse du graphe de dependances (DAG endpoint_relationships)
# ---------------------------------------------------------------------------

def _status_from_direction(direction: str | None) -> str:
    """Convertit une direction d'anomalie en statut lisible (JSON + LLM)."""
    if direction == "degradation":  return "degraded"
    if direction == "improvement":  return "improved"
    if direction is not None:       return "normal"
    return "unknown"


# Poids de causalite par type de relation.
# direct  = 1.00 : appel synchrone premier niveau, lien causal plausible.
# cascade = 0.70 : appel indirect en cascade, plusieurs maillons possibles.
# Design : avec seuil high >= 0.70, cascade ne peut JAMAIS atteindre "high"
# (max cascade = round(0.95 * 0.70, 2) = 0.66 < 0.70). Invariant garanti.
_CALL_TYPE_WEIGHT: dict[str, float] = {"direct": 1.0, "cascade": 0.7}


def _rca_confidence_score(anomaly_score: float, call_type: str, depth: int = 1) -> float:
    """
    Score numerique de confiance RCA dans [0.0, 0.95].

    DISTINCTION FONDAMENTALE :
      anomaly_score    = intensite de l'anomalie du voisin (est-il anomal ?)
      confidence_score = plausibilite du LIEN CAUSAL     (est-il LA cause ?)

    Plafond 0.95 : une co-degradation observee dans une fenetre temporelle
    n'etablit jamais une causalite certaine. Correlation != causalite.

    Formule : confidence_score = min(0.95, anomaly_score * call_type_weight * depth_weight)
      call_type_weight : direct=1.0, cascade=0.70
      depth_weight     : 1 / depth  (penalise les causes distantes)

    Exemples :
      direct  depth=1 score=0.90 -> 0.90 * 1.0 * 1.0 = 0.90 -> high
      cascade depth=1 score=0.90 -> 0.90 * 0.7 * 1.0 = 0.63 -> medium
      direct  depth=2 score=0.90 -> 0.90 * 1.0 * 0.5 = 0.45 -> medium
      cascade depth=2 score=0.90 -> 0.90 * 0.7 * 0.5 = 0.32 -> low
    """
    weight       = _CALL_TYPE_WEIGHT.get(call_type, 0.75)
    depth_weight = 1.0 / max(1, depth)
    return round(min(0.95, anomaly_score * weight * depth_weight), 2)


def _rca_confidence(confidence_score: float) -> str:
    """
    Niveau qualitatif derive du confidence_score.

    Seuils :
      high   >= 0.70  (direct depth=1 + signal fort)
      medium >= 0.35  (evidence moderee : cascade ou profondeur > 1)
      low    <  0.35  (signal faible ou cause tres distante)

    Design : cascade (max 0.95*0.70=0.665) reste toujours en dessous du seuil high (0.70).
    Un appel indirect ne peut pas avoir la meme certitude causale qu'un appel direct.
    """
    if confidence_score >= 0.70: return "high"
    if confidence_score >= 0.35: return "medium"
    return "low"


def _rca_reason(confidence: str, call_type: str, depth: int = 1) -> str:
    """
    Raison avec vocabulaire SRE : 'suspectee', 'correlee', 'non demontree'.
    Mentionne la profondeur si > 1 pour contextualiser la distance causale.
    N'affirme jamais une certitude causale.
    """
    hop_info = f", profondeur {depth}" if depth > 1 else ""
    if confidence == "high":
        return (
            f"co-degradation correlee sur dependance {call_type}{hop_info} -- "
            f"causalite suspectee, non demontree"
        )
    if confidence == "medium":
        return (
            f"co-degradation observee sur dependance {call_type}{hop_info} -- "
            f"causalite non demontree"
        )
    return (
        f"signal de degradation faible sur dependance {call_type}{hop_info} -- "
        f"correlation possible, non demontree"
    )


def _get_anomaly_scores(cur, endpoint_ids: list, detected_at) -> dict:
    """
    Score d'anomalie le plus recent pour chaque endpoint dans la fenetre
    [detected_at - 10min, detected_at]. Lit les anomalies du cycle precedent
    (la transaction courante n'est pas encore committee).

    Retourne {endpoint_id: {"score": float|None, "direction": str|None}}.
    """
    if not endpoint_ids:
        return {}
    cur.execute("""
        SELECT DISTINCT ON (endpoint_id)
            endpoint_id, score, direction
        FROM anomalies
        WHERE endpoint_id = ANY(%s)
          AND detected_at BETWEEN %s - INTERVAL '10 minutes' AND %s
        ORDER BY endpoint_id, detected_at DESC
    """, (endpoint_ids, detected_at, detected_at))
    return {
        row[0]: {"score": row[1], "direction": row[2]}
        for row in cur.fetchall()
    }


def analyze_service_graph(cur, endpoint_id: str, detected_at) -> dict:
    """
    Analyse le graphe de dependances autour d'un endpoint (traversee multi-hop bornee).

    SEPARATION DES RESPONSABILITES :
      - Cette fonction fait de l'ATTRIBUTION causale (post-detection, best-effort).
      - Elle n'influence PAS la decision de detection (Layer 0/1/2).
      - Toute exception est catchee dans l'appelant (detector.py) : une erreur
        ici ne bloque jamais une alerte.

    Traversee bornee par MAX_GRAPH_DEPTH (defaut 3) avec double protection anti-cycles :
      1. UNION (pas UNION ALL) : deduplication native dans la CTE
      2. NOT (child_endpoint_id = ANY(path)) : protection runtime si un cycle invalide
         echapperait au trigger trg_no_cycle

    Lit les anomalies du cycle precedent (transaction courante non committee).
    Lag inherent ~60 s : acceptable car PENDING -> FIRING necessite 2 cycles.

    Retour :
    {
        "endpoint_id":           str,
        "service":               str | None,
        "children":              [{endpoint_id, service, call_type, depth=1, path,
                                   status, anomaly_score, direction}],
        "parents":               [{endpoint_id, service, call_type,
                                   status, anomaly_score, direction}],
        "root_cause_candidates": [{...descendants fields...,
                                   confidence_score float[0, 0.95],
                                   confidence str (high|medium|low),
                                   reason str}],
    }

    confidence_score in [0.0, 0.95] : plausibilite du lien causal, penalisee par la profondeur.
    depth : nombre de sauts depuis l'endpoint analyse (1 = enfant direct).
    path  : liste d'endpoints de l'endpoint analyse jusqu'au candidat (inclus).
    call_type : type de la DERNIERE arete parcourue vers ce noeud (pas du chemin complet).
      Exemple : A --direct--> B --cascade--> C  =>  C.call_type = "cascade".
      Pour depth=1, call_type est l'arete directe (comportement intuitif).
    Deduplication : un meme endpoint peut etre atteignable via plusieurs chemins dans un
      DAG en diamant. On conserve l'entree avec le meilleur confidence_score.
    Trie par confidence_score DESC : favorise les causes proches et directes.
    Retourne {} si la table endpoint_relationships est inaccessible.
    """
    try:
        # Query 1 : descendants multi-hop via CTE recursive bornee par MAX_GRAPH_DEPTH.
        # UNION (deduplication) + NOT ANY(path) : double protection anti-cycles.
        # root_service : service de l'endpoint courant, propage depuis le niveau 1.
        cur.execute("""
            WITH RECURSIVE descendants(endpoint_id, service, call_type, depth, path, root_service) AS (
                SELECT
                    er.child_endpoint_id,
                    er.child_service,
                    er.call_type,
                    1,
                    ARRAY[%s::text, er.child_endpoint_id],
                    er.parent_service
                FROM endpoint_relationships er
                WHERE er.parent_endpoint_id = %s

                UNION

                SELECT
                    er.child_endpoint_id,
                    er.child_service,
                    er.call_type,
                    d.depth + 1,
                    d.path || er.child_endpoint_id,
                    d.root_service
                FROM endpoint_relationships er
                JOIN descendants d ON er.parent_endpoint_id = d.endpoint_id
                WHERE d.depth < %s
                  AND NOT (er.child_endpoint_id = ANY(d.path))
            )
            SELECT endpoint_id, service, call_type, depth, path, root_service
            FROM descendants
            ORDER BY depth, endpoint_id
        """, (endpoint_id, endpoint_id, MAX_GRAPH_DEPTH))
        desc_rows = cur.fetchall()
    except Exception as e:
        log.warning(f"analyze_service_graph: lecture descendants echouee : {e}")
        return {}

    try:
        # Query 2 : parents 1-hop avec child_service pour deriver current_service.
        cur.execute("""
            SELECT parent_endpoint_id, parent_service, child_service, call_type
            FROM endpoint_relationships
            WHERE child_endpoint_id = %s
        """, (endpoint_id,))
        parent_rows = cur.fetchall()
    except Exception as e:
        log.warning(f"analyze_service_graph: lecture parents echouee : {e}")
        parent_rows = []

    # Service de l'endpoint courant (deux sources complementaires) :
    #   1. child_service d'une arete entrante (si l'endpoint a des parents)
    #   2. root_service propage par la CTE (si l'endpoint est une racine avec enfants)
    current_service = None
    for _, _, child_svc, _ in parent_rows:
        if child_svc:
            current_service = child_svc
            break
    if current_service is None and desc_rows:
        current_service = desc_rows[0][5]  # root_service du premier descendant

    # IDs de tous les voisins pour le lookup d'anomalies (cycle precedent).
    all_neighbor_ids = list(
        {row[0] for row in desc_rows} | {row[0] for row in parent_rows}
    )

    try:
        scores = _get_anomaly_scores(cur, all_neighbor_ids, detected_at)
    except Exception as e:
        log.warning(f"analyze_service_graph: lecture anomalies echouee : {e}")
        scores = {}

    def _enrich_desc(row):
        ep_id, svc, call_type, depth, path, _ = row
        anm       = scores.get(ep_id, {})
        direction = anm.get("direction")
        return {
            "endpoint_id":   ep_id,
            "service":       svc,
            "call_type":     call_type,
            "depth":         depth,
            "path":          list(path) if path else [],
            "status":        _status_from_direction(direction),
            "anomaly_score": anm.get("score"),
            "direction":     direction,
        }

    def _enrich_parent(row):
        p_id, p_svc, _, call_type = row
        anm       = scores.get(p_id, {})
        direction = anm.get("direction")
        return {
            "endpoint_id":   p_id,
            "service":       p_svc,
            "call_type":     call_type,
            "status":        _status_from_direction(direction),
            "anomaly_score": anm.get("score"),
            "direction":     direction,
        }

    all_descendants = [_enrich_desc(row) for row in desc_rows]
    children = [d for d in all_descendants if d["depth"] == 1]
    parents  = [_enrich_parent(row) for row in parent_rows]

    # Candidats root-cause : tous descendants degrades (depth 1..MAX_GRAPH_DEPTH).
    # depth_weight = 1/depth penalise mecaniquement les causes distantes.
    def _build_candidate(d: dict) -> dict:
        cs     = _rca_confidence_score(d["anomaly_score"], d["call_type"], d["depth"])
        conf   = _rca_confidence(cs)
        reason = _rca_reason(conf, d["call_type"], d["depth"])
        return {**d, "confidence_score": cs, "confidence": conf, "reason": reason}

    # Deduplication DAG diamant : un meme endpoint peut etre atteignable via plusieurs
    # chemins (ex: A->B->D et A->C->D). On garde l'entree avec le meilleur
    # confidence_score (chemin direct/peu profond prefere mecaniquement).
    raw_candidates = [
        _build_candidate(d)
        for d in all_descendants
        if d["direction"] == "degradation" and d["anomaly_score"] is not None
    ]
    best_by_ep: dict[str, dict] = {}
    for cand in raw_candidates:
        ep = cand["endpoint_id"]
        if ep not in best_by_ep or cand["confidence_score"] > best_by_ep[ep]["confidence_score"]:
            best_by_ep[ep] = cand

    # Tri par confidence_score DESC : favorise les causes proches et directes.
    root_cause_candidates = sorted(
        best_by_ep.values(),
        key=lambda c: c["confidence_score"],
        reverse=True,
    )

    result = {
        "endpoint_id":           endpoint_id,
        "service":               current_service,
        "children":              children,
        "parents":               parents,
        "root_cause_candidates": root_cause_candidates,
    }

    if root_cause_candidates:
        log.info(
            f"Service graph [{endpoint_id}]: "
            f"{len(root_cause_candidates)} candidat(s) root-cause -- "
            + ", ".join(
                f"{c['endpoint_id']}(score={c['anomaly_score']:.2f},depth={c['depth']})"
                for c in root_cause_candidates
            )
        )
    else:
        log.debug(
            f"Service graph [{endpoint_id}]: "
            f"{len(children)} enfant(s), {len(parents)} parent(s), aucun candidat"
        )
    return result
