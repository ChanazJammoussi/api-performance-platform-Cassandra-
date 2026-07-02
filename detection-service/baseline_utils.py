"""
baseline_utils.py -- fonctions et constantes partagees entre detector.py et explainer.py.
"""

ENDPOINT_SLOS = {
    "GET /orders/{order_id}":     {"p99_ms": 300,  "error_rate_5xx": 5},
    "GET /orders":                {"p99_ms": 300,  "error_rate_5xx": 5},
    "POST /payments":             {"p99_ms": 500,  "error_rate_5xx": 5},
    "GET /api/orders/{order_id}": {"p99_ms": 600,  "error_rate_5xx": 5},
    "POST /api/payments":         {"p99_ms": 800,  "error_rate_5xx": 5},
}
DEFAULT_SLOS = {"p99_ms": 500, "error_rate_5xx": 10}


def get_baseline(cur, endpoint_id, metric, dow, hour_bucket):
    cur.execute("""
        SELECT p10, p50, p90
        FROM endpoint_baseline
        WHERE endpoint_id = %s AND metric = %s AND dow = %s AND hour_bucket = %s
        LIMIT 1
    """, (endpoint_id, metric, dow, hour_bucket))
    row = cur.fetchone()
    if row:
        return row
    cur.execute("""
        SELECT MIN(p10), AVG(p50), MAX(p90)
        FROM endpoint_baseline
        WHERE endpoint_id = %s AND metric = %s
    """, (endpoint_id, metric))
    return cur.fetchone()


def compute_deviation(observed, p10, p90):
    if p10 is None or p90 is None:
        return 0.0
    if observed <= p90:
        return 0.0
    return (observed - p90) / (p90 - p10 + 1e-6)
