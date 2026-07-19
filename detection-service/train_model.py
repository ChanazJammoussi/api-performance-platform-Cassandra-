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

    # Fenetre de reference fixe (creee au 1er run) + sanity gate vs modele precedent.
    reference = ml_model.ensure_reference(X)
    previous = ml_model.load_previous_versioned()
    ok, report = ml_model.sanity_gate(model, previous, reference)
    log.info(f"Sanity gate : {report['reason']}")

    if args.dry_run:
        log.info("--dry-run : aucun artefact ecrit")
        return True

    promote = ok or args.force
    if not ok and args.force:
        log.warning("Sanity gate echoue mais --force : promotion forcee")
    path = ml_model.save_artifact(model, meta, promote=promote)
    if not promote:
        log.warning(f"Artefact conserve mais NON promu (gate echoue) : {path}")
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
