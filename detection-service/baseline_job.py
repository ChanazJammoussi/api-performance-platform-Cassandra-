#!/usr/bin/env python3
"""
baseline_job.py -- recalcule endpoint_baseline depuis endpoint_features.

Logique :
  Pour chaque (endpoint_id, metric, dow, hour_bucket) :
    - Prend les 14 derniers jours de endpoint_features
    - Calcule p10 / p50 / p90 via percentile_cont (SQL)
    - Ignore les buckets avec sample_count < MIN_SAMPLES
    - Upsert dans endpoint_baseline

Lancement : python baseline_job.py          (run-once)
            python baseline_job.py --loop   (toutes les INTERVAL_SECONDS)
"""

import argparse
import os
import logging
import time
import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")

# Nombre minimum de samples par bucket pour que le quantile soit fiable
MIN_SAMPLES = 10

# Fenetre historique utilisee pour le calcul
LOOKBACK_DAYS = 14

# Metriques a calculer et la colonne source correspondante dans endpoint_features
METRICS = {
    "p99_ms":         "p99_ms",
    "p50_ms":         "p50_ms",
    "rps":            "rps",
    "error_rate_5xx": "error_rate_5xx",
    "error_rate_4xx": "error_rate_4xx",
}

UPSERT_SQL = """
INSERT INTO endpoint_baseline
    (endpoint_id, metric, dow, hour_bucket, p10, p50, p90, sample_count, updated_at)
SELECT
    endpoint_id,
    %(metric)s                                          AS metric,
    EXTRACT(DOW FROM time)::smallint                    AS dow,
    EXTRACT(HOUR FROM time)::smallint                   AS hour_bucket,
    percentile_cont(0.10) WITHIN GROUP (ORDER BY {col}) AS p10,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY {col}) AS p50,
    percentile_cont(0.90) WITHIN GROUP (ORDER BY {col}) AS p90,
    COUNT(*)                                            AS sample_count,
    NOW()                                               AS updated_at
FROM endpoint_features
WHERE
    time >= NOW() - INTERVAL '{days} days'
    AND {col} IS NOT NULL
    AND {col} < 'NaN'::float8  -- Exclude NaN values before percentile computation
GROUP BY endpoint_id, dow, hour_bucket
HAVING COUNT(*) >= {min_samples}
ON CONFLICT (endpoint_id, metric, dow, hour_bucket)
DO UPDATE SET
    p10          = EXCLUDED.p10,
    p50          = EXCLUDED.p50,
    p90          = EXCLUDED.p90,
    sample_count = EXCLUDED.sample_count,
    updated_at   = EXCLUDED.updated_at;
"""


def run_baseline(conn):
    cur = conn.cursor()
    total_upserted = 0

    for metric, col in METRICS.items():
        sql = UPSERT_SQL.format(
            col=col,
            days=LOOKBACK_DAYS,
            min_samples=MIN_SAMPLES,
        )
        cur.execute(sql, {"metric": metric})
        upserted = cur.rowcount
        total_upserted += upserted
        log.info(f"  {metric:<20s} : {upserted} buckets upserted")

    conn.commit()
    cur.close()
    log.info(f"Baseline refresh done -- {total_upserted} total upserts")


def run_once():
    conn = psycopg2.connect(DB_URL)
    try:
        log.info("Starting baseline refresh")
        run_baseline(conn)
    finally:
        conn.close()


def run_loop(interval_seconds: int):
    log.info(f"Baseline job starting -- interval={interval_seconds}s")
    while True:
        try:
            conn = psycopg2.connect(DB_URL)
            run_baseline(conn)
            conn.close()
        except Exception as e:
            log.error(f"Baseline refresh failed: {e}")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--loop", action="store_true",
        help="Tourne en boucle toutes les --interval secondes"
    )
    parser.add_argument(
        "--interval", type=int, default=3600,
        help="Intervalle en secondes entre deux refreshs (defaut: 3600)"
    )
    args = parser.parse_args()

    if args.loop:
        run_loop(args.interval)
    else:
        run_once()
