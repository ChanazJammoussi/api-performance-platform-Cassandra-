#!/usr/bin/env python3
"""
compare_supervised.py -- comparaison SUPERVISE vs NON-SUPERVISE (spec 8.2 / 13,
STRETCH, experience documentee -- PAS destinee a etre shippee).

But (spec 8.2) : demontrer la conscience de l'alternative supervisee et MESURER
l'ecart, tout en justifiant le choix du non-supervise (rarete des labels : entrainer
sur les fautes injectees consomme le jeu de test et surapprend des formes synthetiques).

Modele supervise : gradient boosting a arbres (`HistGradientBoostingClassifier` de
scikit-learn). Le spec cite XGBoost (§10) ; on utilise l'implementation scikit-learn
equivalente pour cette experience, afin d'eviter une dependance lourde/instable
(le wheel xgboost ~154 Mo echoue au telechargement dans cet environnement). Le
resultat -- l'ecart supervise vs non-supervise -- est le meme objectif.

Protocole :
  1. Matrice de features endpoint-relatives (memes features que le detecteur),
     etiquetee : label=1 si le point tombe dans une fenetre d'injection ciblant
     cet endpoint, 0 sinon.
  2. Split TEMPOREL 50/50 (moitie ancienne = train, moitie recente = test).
  3. Supervise entraine sur (X_train, y_train) ; Isolation Forest sur X_train
     (labels ignores).
  4. Metriques sur le TEST : precision / recall / F1 (classe anomalie), ROC-AUC, PR-AUC.

Usage : python compare_supervised.py
"""

import argparse
import glob
import json
import os
from datetime import datetime, timezone, timedelta

import numpy as np
import psycopg2
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import precision_recall_fscore_support, roc_auc_score, average_precision_score

import ml_model
from features import compute_features, vectorize, WINDOW_SIZE, MIN_WINDOW, FEATURE_NAMES
import train_model as tm

DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")
RESULTS_DIR = tm.RESULTS_DIR
LOOKBACK_DAYS = 40  # couvre l'historique injecte + la campagne
MARGIN = timedelta(minutes=2)


def load_labeled_windows():
    """Fenetres d'injection par endpoint : {endpoint_id: [(injected, cleared), ...]}."""
    by_ep = {}
    for path in glob.glob(os.path.join(RESULTS_DIR, "**", "*.json"), recursive=True):
        try:
            with open(path) as f:
                data = json.load(f)
            faults = data["faults"] if isinstance(data, dict) and "faults" in data else data
            if not isinstance(faults, list):
                continue
            for e in faults:
                ia, ca, ep = e.get("injected_at"), e.get("cleared_at"), e.get("target_endpoint")
                if not (ia and ca and ep):
                    continue
                a = datetime.fromisoformat(ia); c = datetime.fromisoformat(ca)
                if a.tzinfo is None: a = a.replace(tzinfo=timezone.utc)
                if c.tzinfo is None: c = c.replace(tzinfo=timezone.utc)
                by_ep.setdefault(ep, []).append((a - MARGIN, c + MARGIN))
        except Exception:
            continue
    return by_ep


def build_labeled(by_ep_rows, exact, fallback, win_by_ep):
    """Features + labels + timestamps (label=1 si dans une fenetre de l'endpoint)."""
    X, y, T = [], [], []
    for ep, rows in by_ep_rows.items():
        intervals = win_by_ep.get(ep, [])
        usable = rows[:-1] if len(rows) > 1 else rows  # watermark
        for i in range(len(usable)):
            w = usable[max(0, i - WINDOW_SIZE + 1): i + 1]
            if len(w) < MIN_WINDOW:
                continue
            t = usable[i]["time"]
            baselines = tm.baseline_for(exact, fallback, ep, t)
            X.append(vectorize(compute_features(w, baselines)))
            y.append(1 if any(a <= t <= c for a, c in intervals) else 0)
            T.append(t)
    return np.array(X, dtype=float), np.array(y, dtype=int), np.array(T)


def metrics(y, pred, score):
    p, r, f, _ = precision_recall_fscore_support(y, pred, average="binary", zero_division=0)
    two = len(set(y)) > 1
    return {
        "precision": p, "recall": r, "f1": f,
        "roc_auc": roc_auc_score(y, score) if two else None,
        "pr_auc": average_precision_score(y, score) if two else None,
    }


def _row(name, m):
    def f(v): return f"{v:.3f}" if isinstance(v, float) else "-"
    print(f"{name:<28} {f(m['precision']):>9} {f(m['recall']):>7} {f(m['f1']):>7} "
          f"{f(m['roc_auc']):>8} {f(m['pr_auc']):>7}")


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()

    conn = psycopg2.connect(DB_URL)
    by_ep_rows = tm.load_feature_rows(conn, LOOKBACK_DAYS)
    exact, fallback = tm.load_baseline_lookup(conn)
    conn.close()
    win_by_ep = load_labeled_windows()

    X, y, T = build_labeled(by_ep_rows, exact, fallback, win_by_ep)
    if len(X) == 0 or y.sum() == 0:
        print("Pas assez de donnees etiquetees (aucune injection dans la fenetre).")
        return 1

    order = np.argsort(T)
    X, y = X[order], y[order]
    cut = len(X) // 2
    Xtr, ytr, Xte, yte = X[:cut], y[:cut], X[cut:], y[cut:]
    print(f"Echantillons : train={len(Xtr)} (pos={int(ytr.sum())})  "
          f"test={len(Xte)} (pos={int(yte.sum())})  features={len(FEATURE_NAMES)}")
    if ytr.sum() == 0 or yte.sum() == 0:
        print("Split temporel sans positifs des deux cotes -- comparaison impossible.")
        return 1

    # --- Supervise : gradient boosting a arbres (equivalent XGBoost) ---
    clf = HistGradientBoostingClassifier(
        max_iter=300, max_depth=4, learning_rate=0.1,
        class_weight="balanced", random_state=42,
    )
    clf.fit(Xtr, ytr)
    sup_proba = clf.predict_proba(Xte)[:, 1]
    sup_pred = (sup_proba >= 0.5).astype(int)
    m_sup = metrics(yte, sup_pred, sup_proba)

    # --- Non supervise : Isolation Forest (labels ignores a l'entrainement) ---
    iforest = ml_model.train(Xtr, contamination=0.02)
    if_score = ml_model.raw_anomaly(iforest, Xte)          # plus grand = plus anormal
    if_pred = (iforest.predict(Xte) == -1).astype(int)
    m_if = metrics(yte, if_pred, if_score)

    print(f"\n{'modele':<28} {'precision':>9} {'recall':>7} {'f1':>7} {'roc_auc':>8} {'pr_auc':>7}")
    print("-" * 70)
    _row("Isolation Forest (non-sup.)", m_if)
    _row("Gradient boosting (sup.)", m_sup)
    print("-" * 70)

    # Importance des features (permutation) pour le modele supervise, top 5.
    try:
        pi = permutation_importance(clf, Xte, yte, n_repeats=5, random_state=42, scoring="f1")
        imp = sorted(zip(FEATURE_NAMES, pi.importances_mean), key=lambda t: t[1], reverse=True)
        print("\nTop features (supervise, permutation) :",
              ", ".join(f"{n}={v:.3f}" for n, v in imp[:5]))
    except Exception as e:
        print(f"(importance non calculee : {e})")

    gap = None
    if m_sup["pr_auc"] is not None and m_if["pr_auc"] is not None:
        gap = m_sup["pr_auc"] - m_if["pr_auc"]
        print(f"\nEcart PR-AUC (sup - non-sup) : {gap:+.3f}")

    print("\nLecture (spec 8.2) : le supervise obtient generalement de meilleures metriques"
          "\nsur ce test etiquete, mais (a) il exige des labels d'incidents rares, (b) il"
          "\nsurapprend les formes de fautes SYNTHETIQUES injectees, et (c) l'entrainer"
          "\nconsommerait le jeu de test. Le non-supervise est retenu en production ; cette"
          "\ncomparaison mesure l'ecart sans le shipper.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
