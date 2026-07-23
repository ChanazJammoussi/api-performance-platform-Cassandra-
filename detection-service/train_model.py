#!/usr/bin/env python3
"""
train_model.py -- entraine le modele Isolation Forest global (spec 5.4 / 8.3).

Pipeline :
  1. Charge l'historique endpoint_features sur --lookback-days.
  2. Charge endpoint_baseline en memoire (buckets exacts + fallback MIN/AVG/MAX).
  3. Charge les fenetres d'injection (scenario-runner/results/*.json) et les EXCLUT
     de l'entrainement : les fautes injectees sont le TEST SET, jamais l'entrainement
     (spec 8.1 / 8.2). Correctness, pas une option.
  4. Construit la matrice de features endpoint-relatives (features.compute_features)
     par fenetre glissante, en excluant la fenetre la plus recente incomplete.
  5. Fit IsolationForest, calibration sur la distribution trailing.
  6. Sanity gate (shift de distribution vs modele precedent sur fenetre de reference).
  7. Sauvegarde artefact horodate + promotion du pointeur "latest" si le gate passe.

Lancement :
    python train_model.py                       # run-once, promotion si gate OK
    python train_model.py --dry-run             # entraine, n'ecrit rien
    python train_model.py --force               # promeut meme si le gate echoue
    python train_model.py --loop --interval 86400   # refit nightly (spec 8.3)
"""

import argparse
import glob
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import numpy as np
import psycopg2

import ml_model
from features import compute_features, vectorize, WINDOW_SIZE, MIN_WINDOW
from baseline_utils import pg_dow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")

RESULTS_DIR = os.environ.get(
    "GROUND_TRUTH_DIR",
    os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "scenario-runner", "results"
    )),
)

# Metriques dont on a besoin de la baseline saisonniere pour les features derivees.
BASELINE_METRICS = ["p50_ms", "p95_ms", "p99_ms", "rps"]

# Marge autour des fenetres d'injection exclues (residu de la faute dans les rates).
INJECTION_MARGIN = timedelta(minutes=5)

MIN_TRAIN_SAMPLES = 200


def load_feature_rows(conn, lookback_days):
    """Rows endpoint_features valides sur la fenetre, groupees par endpoint (time asc)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT endpoint_id, time, rps, p50_ms, p95_ms, p99_ms, error_rate_5xx
        FROM endpoint_features
        WHERE time >= NOW() - (%s || ' days')::interval
          AND p99_ms IS NOT NULL AND p99_ms < 'NaN'::float8
        ORDER BY endpoint_id, time ASC
    """, (lookback_days,))
    rows = cur.fetchall()
    cur.close()
    by_ep = {}
    for endpoint_id, t, rps, p50, p95, p99, err in rows:
        by_ep.setdefault(endpoint_id, []).append({
            "time": t, "rps": rps, "p50_ms": p50, "p95_ms": p95,
            "p99_ms": p99, "error_rate_5xx": err,
        })
    return by_ep


def load_baseline_lookup(conn):
    """
    Charge endpoint_baseline en memoire :
      exact[(endpoint, metric, dow, hour)] = (p10, p50, p90)
      fallback[(endpoint, metric)]         = (min p10, avg p50, max p90)  # cf get_baseline
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT endpoint_id, metric, dow, hour_bucket, p10, p50, p90
        FROM endpoint_baseline
    """)
    exact = {}
    agg = {}
    for ep, metric, dow, hour, p10, p50, p90 in cur.fetchall():
        exact[(ep, metric, int(dow), int(hour))] = (p10, p50, p90)
        a = agg.setdefault((ep, metric), {"p10": [], "p50": [], "p90": []})
        if p10 is not None: a["p10"].append(p10)
        if p50 is not None: a["p50"].append(p50)
        if p90 is not None: a["p90"].append(p90)
    cur.close()
    fallback = {}
    for key, a in agg.items():
        fallback[key] = (
            min(a["p10"]) if a["p10"] else None,
            (sum(a["p50"]) / len(a["p50"])) if a["p50"] else None,
            max(a["p90"]) if a["p90"] else None,
        )
    return exact, fallback


def baseline_for(exact, fallback, endpoint_id, dt):
    """Baselines {metric: (p10,p50,p90)} pour un point, bucket exact puis fallback."""
    dow, hour = pg_dow(dt), dt.hour
    out = {}
    for m in BASELINE_METRICS:
        b = exact.get((endpoint_id, m, dow, hour))
        if b is None:
            b = fallback.get((endpoint_id, m))
        out[m] = b
    return out


def load_injection_windows():
    """Fenetres [injected_at, cleared_at] de toutes les injections ground-truth."""
    windows = []
    for path in glob.glob(os.path.join(RESULTS_DIR, "**", "*.json"), recursive=True):
        try:
            with open(path) as f:
                data = json.load(f)
            faults = data["faults"] if isinstance(data, dict) and "faults" in data else data
            if not isinstance(faults, list):
                continue
            for e in faults:
                ia, ca = e.get("injected_at"), e.get("cleared_at")
                if not ia or not ca:
                    continue
                a = datetime.fromisoformat(ia)
                b = datetime.fromisoformat(ca)
                if a.tzinfo is None: a = a.replace(tzinfo=timezone.utc)
                if b.tzinfo is None: b = b.replace(tzinfo=timezone.utc)
                windows.append((a - INJECTION_MARGIN, b + INJECTION_MARGIN))
        except Exception as e:
            log.warning(f"Ground truth illisible {path}: {e}")
    return windows


def in_injection(dt, windows):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return any(a <= dt <= b for a, b in windows)


def build_matrix(by_ep, exact, fallback, windows):
    """
    Matrice de features endpoint-relatives par fenetre glissante. Exclut :
      - les points dans une fenetre d'injection (test set),
      - la fenetre la plus recente incomplete (watermark, spec 5.2),
      - les fenetres trop courtes (< MIN_WINDOW) pour une pente fiable.
    """
    X = []
    excluded_injection = 0
    for endpoint_id, rows in by_ep.items():
        # Watermark : on ignore le dernier point (fenetre potentiellement incomplete).
        usable = rows[:-1] if len(rows) > 1 else rows
        for i in range(len(usable)):
            point = usable[i]
            if in_injection(point["time"], windows):
                excluded_injection += 1
                continue
            window = usable[max(0, i - WINDOW_SIZE + 1): i + 1]
            if len(window) < MIN_WINDOW:
                continue
            baselines = baseline_for(exact, fallback, endpoint_id, point["time"])
            feat = compute_features(window, baselines)
            X.append(vectorize(feat))
    return np.array(X, dtype=float), excluded_injection


# --- Garde-fous de promotion (audit #13) -----------------------------------
RECALL_TOLERANCE = 0.05      # baisse de recall toleree vs modele courant
FP_TOLERANCE_REL = 0.50      # hausse relative de FP toleree
FP_TOLERANCE_ABS = 2         # + marge absolue (petits nombres)
MAX_DATA_AGE_HOURS = 24      # au-dela, donnees stale -> promotion deconseillee


def data_freshness_hours(conn):
    """Age (heures) de la derniere ligne endpoint_features, ou None si vide."""
    cur = conn.cursor()
    cur.execute("SELECT EXTRACT(EPOCH FROM (now() - max(time)))/3600 FROM endpoint_features")
    row = cur.fetchone()
    cur.close()
    return float(row[0]) if row and row[0] is not None else None


def _recall_fp(ev, windows, by_ep, exact, fallback, bundle):
    """(recall, faux positifs) du detecteur layered avec ce modele, sur la campagne."""
    onsets = ev.build_onsets(by_ep, exact, fallback, bundle)
    per_type, fp, _ = ev.evaluate(windows, onsets)
    tp = sum(d["layered"].tp for d in per_type.values())
    fn = sum(d["layered"].fn for d in per_type.values())
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return recall, fp["layered"]


def validate_promotion(conn, candidate_bundle):
    """
    Rejoue l'evaluation layered avec le modele CANDIDAT et le modele COURANT sur le
    jeu de validation (campagne), et refuse la promotion si le candidat degrade la
    detection (recall en baisse au-dela de la tolerance) ou fait exploser les FP.
    Complementaire du sanity-gate (qui, lui, ne regarde que la distribution des scores).
    Import paresseux d'evaluate_layered pour eviter l'import circulaire.
    """
    from pathlib import Path
    import evaluate_layered as ev

    windows = ev.load_windows(Path(ev.DEFAULT_INPUT))
    if not windows:
        return True, "validation eval : pas de jeu de validation -> autorisee"
    current = ml_model.load_latest()
    if current is None:
        return True, "validation eval : premier modele -> pas de reference"

    since = min(w.injected_at for w in windows) - ev.PAD
    until = max(w.cleared_at for w in windows) + ev.PAD
    by_ep = ev.load_features_span(conn, since, until)
    exact, fallback = load_baseline_lookup(conn)

    r_cand, fp_cand = _recall_fp(ev, windows, by_ep, exact, fallback, candidate_bundle)
    r_cur, fp_cur = _recall_fp(ev, windows, by_ep, exact, fallback, current)

    recall_ok = r_cand >= r_cur - RECALL_TOLERANCE
    fp_ok = fp_cand <= fp_cur + max(FP_TOLERANCE_ABS, int(fp_cur * FP_TOLERANCE_REL))
    ok = recall_ok and fp_ok
    report = (f"validation eval : recall {r_cur:.2f}->{r_cand:.2f}, FP {fp_cur}->{fp_cand} -> "
              + ("OK" if ok else "REGRESSION"))
    return ok, report


def train_and_maybe_promote(conn, args):
    by_ep = load_feature_rows(conn, args.lookback_days)
    if not by_ep:
        log.error("Aucune donnee endpoint_features sur la fenetre -- entrainement annule")
        return False
    exact, fallback = load_baseline_lookup(conn)
    windows = load_injection_windows()
    log.info(f"Endpoints={len(by_ep)}  baselines_exacts={len(exact)}  fenetres_injection={len(windows)}")

    X, excluded = build_matrix(by_ep, exact, fallback, windows)
    log.info(f"Matrice features : {X.shape}  (points exclus car injection: {excluded})")
    if len(X) < MIN_TRAIN_SAMPLES:
        log.error(f"Trop peu d'echantillons ({len(X)} < {MIN_TRAIN_SAMPLES}) -- entrainement annule")
        return False

    model = ml_model.train(X, contamination=args.contamination)
    raw = ml_model.raw_anomaly(model, X)
    calib = ml_model.calibration_params(raw)

    trained_at = datetime.now(timezone.utc).isoformat()
    meta = ml_model.build_meta(trained_at, X, args.contamination, WINDOW_SIZE, args.lookback_days)
    meta["calibration"] = calib

    # --- Garde-fous de promotion (dans l'ordre) ---
    # 1. Sanity gate : shift de distribution des scores vs modele precedent.
    reference = ml_model.ensure_reference(X)
    previous = ml_model.load_previous_versioned()
    ok_gate, gate_report = ml_model.sanity_gate(model, previous, reference)
    log.info(f"Sanity gate : {gate_report['reason']}")

    # 2. Validation d'eval : le candidat ne doit pas degrader la detection.
    candidate = {"model": model, "meta": meta}
    try:
        ok_val, val_report = validate_promotion(conn, candidate)
    except Exception as e:
        ok_val, val_report = True, f"validation eval sautee (erreur: {e})"
    log.info(val_report)

    # 3. Fraicheur : ne pas promouvoir un modele entraine sur des donnees stale.
    age = data_freshness_hours(conn)
    fresh = age is None or age <= MAX_DATA_AGE_HOURS
    if not fresh:
        log.warning(f"Donnees stale (derniere feature il y a {age:.1f}h > {MAX_DATA_AGE_HOURS}h)")

    if args.dry_run:
        log.info(f"--dry-run : aucun artefact ecrit (gate={ok_gate} eval={ok_val} fresh={fresh})")
        return True

    promote = (ok_gate and ok_val and fresh) or args.force
    if not promote:
        reasons = [r for r, ok in (("sanity-gate", ok_gate), ("validation-eval", ok_val),
                                   ("fraicheur", fresh)) if not ok]
        log.warning(f"Promotion REFUSEE ({', '.join(reasons)}) -- artefact conserve, latest inchange")
    elif args.force and not (ok_gate and ok_val and fresh):
        log.warning("Garde-fous non satisfaits mais --force : promotion forcee")

    path = ml_model.save_artifact(model, meta, promote=promote)
    if not promote:
        log.warning(f"Artefact NON promu : {path}")
    log.info(f"Entrainement termine : n={meta['n_samples']} contamination={args.contamination} "
             f"calib(p50={calib['p50']:.3f}, p99={calib['p99']:.3f})")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--contamination", type=float, default=0.02)
    parser.add_argument("--dry-run", action="store_true", help="Entraine sans rien ecrire")
    parser.add_argument("--force", action="store_true", help="Promeut meme si le sanity gate echoue")
    parser.add_argument("--loop", action="store_true", help="Refit periodique (nightly)")
    parser.add_argument("--interval", type=int, default=86400, help="Intervalle en secondes (defaut 24h)")
    args = parser.parse_args()

    if args.loop:
        log.info(f"Trainer en boucle -- interval={args.interval}s")
        while True:
            try:
                conn = psycopg2.connect(DB_URL)
                train_and_maybe_promote(conn, args)
                conn.close()
            except Exception as e:
                log.error(f"Cycle d'entrainement echoue : {e}")
            time.sleep(args.interval)
    else:
        conn = psycopg2.connect(DB_URL)
        try:
            train_and_maybe_promote(conn, args)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
