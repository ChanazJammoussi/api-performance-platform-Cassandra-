"""
prom_metrics.py -- metriques Prometheus natives du service de detection (audit #15).

Toute la self-observabilite existante est derivee du DB (dashboard SQL). Ce module
expose en plus des metriques Prometheus propres au detecteur (duree de cycle,
alertes par etat, appels LLM, fraicheur), scrapees par Prometheus -> alerting natif
possible et boucle "monitoring du monitoring" fermee.

Import defensif : si prometheus_client est absent (image obsolete), on retombe sur
des stubs no-op pour que le detecteur ne crashe JAMAIS a cause des metriques.
"""

import os
import logging

log = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
    _ENABLED = True
except Exception:  # pragma: no cover - chemin de repli
    _ENABLED = False

    class _Noop:
        def labels(self, *a, **k):
            return self

        def inc(self, *a, **k):
            pass

        def set(self, *a, **k):
            pass

        def observe(self, *a, **k):
            pass

    def Counter(*a, **k):    # noqa: N802
        return _Noop()

    def Gauge(*a, **k):      # noqa: N802
        return _Noop()

    def Histogram(*a, **k):  # noqa: N802
        return _Noop()

    def start_http_server(*a, **k):
        pass


CYCLE_SECONDS = Histogram(
    "cassandra_detector_cycle_seconds", "Duree d'un cycle de detection (s)",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
ENDPOINTS_SCORED = Gauge(
    "cassandra_detector_endpoints_scored", "Endpoints scores au dernier cycle")
ALERTS_STATE = Gauge(
    "cassandra_detector_alerts", "Alertes par etat", labelnames=["state"])
ANOMALY_WRITES = Counter(
    "cassandra_detector_anomaly_writes_total", "Ecritures dans l'anomaly store")
CYCLE_ERRORS = Counter(
    "cassandra_detector_cycle_errors_total", "Cycles de detection en erreur")
LLM_CALLS = Counter(
    "cassandra_llm_calls_total", "Appels au LLM par resultat", labelnames=["result"])
SCRAPE_FRESHNESS = Gauge(
    "cassandra_scrape_freshness_seconds", "Age de la derniere feature scrapee (s)")


def start_metrics_server(default_port=9101):
    """Demarre le serveur HTTP /metrics (thread de fond). No-op si lib absente."""
    if not _ENABLED:
        log.warning("prometheus_client absent -- endpoint /metrics desactive")
        return False
    port = int(os.environ.get("METRICS_PORT", default_port))
    start_http_server(port)
    log.info(f"Endpoint /metrics Prometheus expose sur :{port}")
    return True
