#!/usr/bin/env python3
"""
tune_contamination.py -- tuning de sensibilite de la couche ML (spec 9.2).

Deux balayages, sur la campagne d'evaluation, en experience controlee (matrice
d'entrainement et jeu d'eval charges UNE fois ; modeles entraines EN MEMOIRE, sans
promouvoir d'artefact, donc le detecteur live n'est pas perturbe) :

  A. contamination de l'Isolation Forest {0.01..0.10}
     NOTE : dans scikit-learn, `contamination` ne modifie QUE le seuil interne de
     .predict()/.decision_function() ; il ne change PAS score_samples(). Or notre
     scoring passe par score_samples -> calibration par percentiles -> seuil. Le
     parametre est donc quasi inerte ici ; on le montre explicitement.

  B. seuil de declenchement calibre (FIRE_THRESHOLD) {0.3..0.7}
     C'est le VRAI levier du compromis Recall / Precision / FP dans ce design.

Metriques (detecteur LAYERED) :
  Recall = TP/(TP+FN)   Precision = TP/(TP+FP)   F1 = 2PR/(P+R)   FP/heure

Usage :
    python tune_contamination.py
    python tune_contamination.py --contams 0.01 0.03 0.05 0.08 0.10
    python tune_contamination.py --thresholds 0.3 0.4 0.5 0.6 0.7
"""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

import ml_model
from features import WINDOW_SIZE
import train_model as tm
import evaluate_layered as ev

DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")
DEFAULT_CONTAMS = [0.01, 0.03, 0.05, 0.08, 0.10]
DEFAULT_THRESHOLDS = [0.3, 0.4, 0.5, 0.6, 0.7]
DEFAULT_CONTAM = 0.02  # contamination fixe pour le balayage de seuil
LOOKBACK_DAYS = 14


def make_bundle(X, contamination):
    model = ml_model.train(X, contamination=contamination)
    raw = ml_model.raw_anomaly(model, X)
    meta = ml_model.build_meta(datetime.now(timezone.utc).isoformat(), X, contamination, WINDOW_SIZE, LOOKBACK_DAYS)
    meta["calibration"] = ml_model.calibration_params(raw)
    return {"model": model, "meta": meta}


def metrics(bundle, by_ep_eval, exact, fallback, windows, span_hours):
    onsets = ev.build_onsets(by_ep_eval, exact, fallback, bundle)
    per_type, fp, _lead = ev.evaluate(windows, onsets)
    tp = sum(d["layered"].tp for d in per_type.values())
    fn = sum(d["layered"].fn for d in per_type.values())
    fpc = fp["layered"]
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    precision = tp / (tp + fpc) if (tp + fpc) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    fph = fpc / span_hours if span_hours else 0.0
    return dict(tp=tp, fn=fn, fp=fpc, recall=recall, precision=precision, f1=f1, fph=fph)


def _row(label, m):
    print(f"{label:>13} {m['recall']*100:>7.0f}% {m['precision']*100:>9.0f}% "
          f"{m['f1']:>7.3f} {m['fph']:>7.2f}  {m['tp']}/{m['fn']}/{m['fp']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contams", type=float, nargs="+", default=DEFAULT_CONTAMS)
    parser.add_argument("--thresholds", type=float, nargs="+", default=DEFAULT_THRESHOLDS)
    parser.add_argument("--input", type=Path, default=Path(ev.DEFAULT_INPUT))
    args = parser.parse_args()

    windows = ev.load_windows(args.input)
    if not windows:
        print(f"aucune fenetre d'injection dans {args.input}")
        return 1
    since = min(w.injected_at for w in windows) - ev.PAD
    until = max(w.cleared_at for w in windows) + ev.PAD
    span_hours = (until - since).total_seconds() / 3600

    conn = psycopg2.connect(DB_URL)
    by_ep_eval = ev.load_features_span(conn, since, until)
    exact, fallback = tm.load_baseline_lookup(conn)
    by_ep_train = tm.load_feature_rows(conn, LOOKBACK_DAYS)
    inj_windows = tm.load_injection_windows()
    conn.close()

    X, excluded = tm.build_matrix(by_ep_train, exact, fallback, inj_windows)
    print(f"Entrainement : {X.shape[0]} points (exclus injection: {excluded})  |  "
          f"eval : {len(windows)} injections sur {span_hours:.1f}h")

    header = f"\n{'param':>13} {'Recall':>8} {'Precision':>10} {'F1':>7} {'FP/h':>7}  {'TP/FN/FP':>10}"

    # --- A. Balayage contamination (seuil fixe = ev.FIRE_THRESHOLD par defaut) ----
    print("\n=== A. contamination (seuil calibre = "
          f"{ev.FIRE_THRESHOLD}) ===")
    print(header)
    print("-" * 62)
    for c in args.contams:
        _row(f"c={c:.2f}", metrics(make_bundle(X, c), by_ep_eval, exact, fallback, windows, span_hours))
    print("-" * 62)
    print("(constant : contamination n'affecte pas score_samples -> voir balayage B)")

    # --- B. Balayage du seuil de declenchement (contamination fixe) --------------
    print(f"\n=== B. seuil de declenchement FIRE_THRESHOLD (contamination = {DEFAULT_CONTAM}) ===")
    print(header)
    print("-" * 62)
    bundle = make_bundle(X, DEFAULT_CONTAM)
    orig = ev.FIRE_THRESHOLD
    results = []
    for t in args.thresholds:
        ev.FIRE_THRESHOLD = t
        m = metrics(bundle, by_ep_eval, exact, fallback, windows, span_hours)
        results.append((t, m))
        _row(f"thr={t:.2f}", m)
    ev.FIRE_THRESHOLD = orig
    print("-" * 62)

    best = max(results, key=lambda r: r[1]["f1"])
    print(f"\nMeilleur compromis (F1 max) : seuil={best[0]:.2f}  "
          f"(Recall={best[1]['recall']*100:.0f}%  Precision={best[1]['precision']*100:.0f}%  "
          f"F1={best[1]['f1']:.3f}  FP/h={best[1]['fph']:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
