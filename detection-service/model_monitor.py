#!/usr/bin/env python3
"""
model_monitor.py -- surveillance du drift du modele ML en production (audit #10).

Le sanity-gate (train_model.py) compare les distributions AU MOMENT de
l'entrainement. Rien ne surveille la derive APRES deploiement : si le trafic
change, la distribution des scores ML live peut s'ecarter de celle sur laquelle
le modele a ete calibre, degradant silencieusement la detection.

Ce moniteur compare :
  - distribution de REFERENCE : ml_norm sur la fenetre de reference figee
    (reference_window.npy), rejouee par le modele promu courant ;
  - distribution LIVE : ml_norm des dernieres `--hours` heures (anomalies).
via la statistique de Kolmogorov-Smirnov. KS eleve -> drift.

Ecrit une ligne dans model_health (dashboard self-obs) et logue le verdict.

Usage :
    python model_monitor.py
    python model_monitor.py --hours 24 --loop --interval 1800
"""

import argparse
import logging
import os
import time
from datetime import datetime

import numpy as np
import psycopg2

import ml_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")

# KS au-dela duquel on considere qu'il y a drift.
KS_DRIFT_THRESHOLD = 0.30
# Minimum de points live pour un verdict fiable.
MIN_LIVE = 50


def load_live_ml_norm(conn, hours):
    """Valeurs ml_norm des anomalies recentes (score ML calibre, avant gating)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT (contributing_features->>'ml_norm')::float
        FROM anomalies
        WHERE detected_at > now() - make_interval(hours => %s)
          AND contributing_features->>'ml_norm' IS NOT NULL
    """, (hours,))
    vals = [r[0] for r in cur.fetchall() if r[0] is not None and np.isfinite(r[0])]
    cur.close()
    return np.array(vals, dtype=float)


def write_health(conn, row):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO model_health
            (model_trained_at, n_live, ks_drift, ref_mean, live_mean, live_p95,
             live_anomaly_rate, drift_flag)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (row["trained_at"], row["n_live"], row["ks"], row["ref_mean"],
          row["live_mean"], row["live_p95"], row["live_rate"], row["drift"]))
    conn.commit()
    cur.close()


def check(conn, hours):
    bundle = ml_model.load_latest()
    if not bundle:
        log.warning("Aucun modele promu -- drift non evaluable")
        return None
    model, meta = bundle["model"], bundle["meta"]
    calib = meta.get("calibration")
    ref_path = ml_model.reference_path()
    if not calib or not os.path.exists(ref_path):
        log.warning("Calibration ou fenetre de reference absente -- check saute")
        return None

    reference = np.load(ref_path)
    ref_norm = np.asarray(ml_model.calibrate(ml_model.raw_anomaly(model, reference), calib))

    live = load_live_ml_norm(conn, hours)
    if len(live) < MIN_LIVE:
        log.info(f"Trop peu de points live ({len(live)} < {MIN_LIVE}) sur {hours}h -- check saute")
        return None

    ks = ml_model._ks_statistic(ref_norm, live)
    drift = ks > KS_DRIFT_THRESHOLD
    trained_at = meta.get("trained_at")
    row = {
        "trained_at": datetime.fromisoformat(trained_at) if trained_at else None,
        "n_live": int(len(live)),
        "ks": round(float(ks), 4),
        "ref_mean": round(float(np.mean(ref_norm)), 4),
        "live_mean": round(float(np.mean(live)), 4),
        "live_p95": round(float(np.percentile(live, 95)), 4),
        "live_rate": round(float(np.mean(live >= 0.5)), 4),
        "drift": bool(drift),
    }
    write_health(conn, row)
    log.info(
        f"Drift check : KS={row['ks']:.3f} (seuil {KS_DRIFT_THRESHOLD}) | "
        f"ref_mean={row['ref_mean']:.3f} live_mean={row['live_mean']:.3f} "
        f"live_rate={row['live_rate']:.3f} n={row['n_live']} -> "
        f"{'DRIFT' if drift else 'stable'}"
    )
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24, help="Fenetre live (defaut 24h)")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=1800, help="Intervalle en s (defaut 30min)")
    args = parser.parse_args()

    if args.loop:
        log.info(f"Model monitor en boucle -- interval={args.interval}s")
        while True:
            try:
                conn = psycopg2.connect(DB_URL)
                check(conn, args.hours)
                conn.close()
            except Exception as e:
                log.error(f"Cycle de monitoring echoue : {e}")
            time.sleep(args.interval)
    else:
        conn = psycopg2.connect(DB_URL)
        try:
            check(conn, args.hours)
        finally:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
