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

# Repertoire des ground truth JSON (relatif au CWD ou absolu)
RESULTS_DIR = os.environ.get(
    "GROUND_TRUTH_DIR",
    "/mnt/c/Users/chana/cassandra/scenario-runner/results"
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
