import json
import math
import os
import time
import logging
import requests
import psycopg2
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Surchargeable via l'environnement pour tourner en conteneur (service names)
# ou sur l'hote (defauts localhost). Voir docker-compose.yml.
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090")
DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")

ENDPOINTS = [
    # Tier gateway : endpoints user-facing (proxy vers les services)
    {"service": "gateway",  "route": "/api/orders/{order_id}", "method": "GET",  "service_tier": "gateway"},
    {"service": "gateway",  "route": "/api/orders",            "method": "GET",  "service_tier": "gateway"},
    {"service": "gateway",  "route": "/api/payments",          "method": "POST", "service_tier": "gateway"},
    # Tier service : endpoints metier internes instrumes par OTel
    {"service": "orders",   "route": "/orders/{order_id}",     "method": "GET",  "service_tier": "service"},
    {"service": "orders",   "route": "/orders",                "method": "GET",  "service_tier": "service"},
    {"service": "payments", "route": "/payments",              "method": "POST", "service_tier": "service"},
]

# Topologie d'appel : source unique de verite partagee avec init.sql.
# Peuplee dans endpoint_relationships au demarrage (idempotent).
RELATIONSHIPS = [
    # parent_id                       child_id                    parent_svc   child_svc   call_type   metadata
    ("GET /api/orders/{order_id}", "GET /orders/{order_id}", "gateway",  "orders",   "direct",  {}),
    ("GET /api/orders",            "GET /orders",            "gateway",  "orders",   "direct",  {}),
    ("POST /api/payments",         "POST /payments",         "gateway",  "payments", "direct",  {}),
    ("POST /payments", "GET /orders/{order_id}", "payments", "orders",   "cascade",
     {"reason": "order_validation", "description": "payments valide la commande avant traitement"}),
]

def query_prometheus(promql):
    try:
        resp = requests.post(
            f"{PROMETHEUS_URL}/api/v1/query",
            data={"query": promql},
            timeout=10,
        )
        data = resp.json()
        if data["status"] != "success":
            return {}
        result = {}
        for item in data["data"]["result"]:
            labels = item["metric"]
            service = labels.get("service_name", "")
            route = labels.get("http_route", "")
            value = float(item["value"][1])
            key = (service, route)
            # Sommer les valeurs pour le meme (service, route) au lieu d'ecraser.
            # Valide pour les rates (rps, error rates).
            # Pour les percentiles, la PromQL utilise sum by (le) avant histogram_quantile,
            # donc une seule serie par (service, route) est retournee -- la somme est un no-op.
            result[key] = result.get(key, 0.0) + value
        return result
    except Exception as e:
        log.error(f"Prometheus query failed: {e}")
        return {}

def fetch_features():
    """Récupère toutes les features depuis Prometheus pour tous les endpoints."""
    window = "5m"

    # p50, p95, p99 -- on agrège les buckets par (service_name, http_route, le)
    # avant de calculer le quantile, pour fusionner les series par http_status_code.
    p50 = query_prometheus(
        f'histogram_quantile(0.50, sum by (service_name, http_route, le) ('
        f'rate(duration_milliseconds_bucket{{span_kind="SPAN_KIND_SERVER"}}[{window}])))'
    )
    p95 = query_prometheus(
        f'histogram_quantile(0.95, sum by (service_name, http_route, le) ('
        f'rate(duration_milliseconds_bucket{{span_kind="SPAN_KIND_SERVER"}}[{window}])))'
    )
    p99 = query_prometheus(
        f'histogram_quantile(0.99, sum by (service_name, http_route, le) ('
        f'rate(duration_milliseconds_bucket{{span_kind="SPAN_KIND_SERVER"}}[{window}])))'
    )

    # RPS -- rate sur calls_total, somme par (service, route) dans query_prometheus
    rps = query_prometheus(
        f'rate(calls_total{{span_kind="SPAN_KIND_SERVER"}}[{window}])'
    )

    # Pourcentage d'erreurs 5xx
    err5xx = query_prometheus(
        f'100 * sum without(http_status_code, status_code) ('
        f'rate(calls_total{{span_kind="SPAN_KIND_SERVER", http_status_code=~"5.."}}[{window}]))'
        f' / sum without(http_status_code, status_code) ('
        f'rate(calls_total{{span_kind="SPAN_KIND_SERVER"}}[{window}]))'
    )

    # Pourcentage d'erreurs 4xx
    err4xx = query_prometheus(
        f'100 * sum without(http_status_code, status_code) ('
        f'rate(calls_total{{span_kind="SPAN_KIND_SERVER", http_status_code=~"4.."}}[{window}]))'
        f' / sum without(http_status_code, status_code) ('
        f'rate(calls_total{{span_kind="SPAN_KIND_SERVER"}}[{window}]))'
    )

    return p50, p95, p99, rps, err5xx, err4xx

def write_features(conn, now, p50, p95, p99, rps, err5xx, err4xx):
    """Ecrit une ligne dans endpoint_features pour chaque endpoint."""
    cur = conn.cursor()
    written = 0
    for ep in ENDPOINTS:
        service = ep["service"]
        route = ep["route"]
        key = (service, route)

        endpoint_id = f"{ep['method']} {route}"
        p99_val = p99.get(key)
        if p99_val is None or not math.isfinite(p99_val):
            log.debug(
                "Skipping endpoint %s: no valid latency metric available",
                endpoint_id
            )
            continue

        row = (
            now,
            endpoint_id,
            service,
            rps.get(key),
            p50.get(key),
            p95.get(key),
            p99_val,
            err5xx.get(key, 0.0),
            err4xx.get(key, 0.0),
            ep.get("service_tier"),
        )

        cur.execute("""
            INSERT INTO endpoint_features
                (time, endpoint_id, service, rps, p50_ms, p95_ms, p99_ms,
                 error_rate_5xx, error_rate_4xx, service_tier)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, row)

        log.debug(
            f"{ep['method']} {route:<30s} "
            f"rps={rps.get(key, 0):.2f}  "
            f"p99={p99_val:.0f}ms  "
            f"err5xx={err5xx.get(key, 0):.1f}%"
        )
        written += 1

    conn.commit()
    cur.close()
    log.info(f"Written {written}/{len(ENDPOINTS)} endpoints at {now}")
    if written == 0:
        log.warning(
            "No endpoint features written this cycle -- "
            "normal during warmup or possible Prometheus/OTel collection issue"
        )

def populate_relationships(conn):
    """Insere la topologie d'appel dans endpoint_relationships (idempotent)."""
    cur = conn.cursor()
    for parent_id, child_id, parent_svc, child_svc, call_type, metadata in RELATIONSHIPS:
        cur.execute("""
            INSERT INTO endpoint_relationships
                (parent_endpoint_id, child_endpoint_id,
                 parent_service, child_service, call_type, metadata)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (parent_id, child_id, parent_svc, child_svc, call_type, json.dumps(metadata)))
    conn.commit()
    cur.close()
    log.info(f"endpoint_relationships peuplee ({len(RELATIONSHIPS)} aretes, idempotent)")


def run():
    conn = psycopg2.connect(DB_URL)
    log.info("Connected to TimescaleDB")
    populate_relationships(conn)

    while True:
        try:
            now = datetime.now(timezone.utc)
            p50, p95, p99, rps, err5xx, err4xx = fetch_features()
            write_features(conn, now, p50, p95, p99, rps, err5xx, err4xx)
        except Exception as e:
            log.error(f"Scrape cycle failed: {e}")
            conn = psycopg2.connect(DB_URL)

        time.sleep(60)

if __name__ == "__main__":
    run()
