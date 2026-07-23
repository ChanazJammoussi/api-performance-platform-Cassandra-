"""
ttd.py -- heuristique d'alerte precoce Time-To-Degradation (spec 8.4, STRETCH).

Pour un endpoint qui se degrade (tendance p99 haussiere) mais n'a pas encore
franchi le seuil SLO, on extrapole la serie p99 recente vers le SLO par une
regression robuste de Theil-Sen, et on estime le temps restant avant breach.

Advisory UNIQUEMENT (spec 8.4) : c'est une extrapolation de tendance, jamais une
prediction garantie. Pas de modele supervise (raretes des labels). L'intervalle
[low, high] vient de la dispersion des pentes par paires (robustesse Theil-Sen).
"""

import numpy as np

# Cadence de scrape/detection : une "cycle" = 60s. La pente est en ms/cycle,
# donc ms/minute ici.
CYCLE_SECONDS = 60

# Pente minimale (ms/min) pour considerer une tendance haussiere significative.
MIN_SLOPE = 0.5

# On ne surface pas un TTD au-dela de cet horizon (trop incertain pour etre utile).
MAX_HORIZON_MIN = 120


def _finite(series):
    return [float(v) for v in series if v is not None and np.isfinite(v)]


def _pairwise_slopes(y):
    """Pentes de toutes les paires (i<j), en ms/cycle. Base de Theil-Sen."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n < 3:
        return np.array([])
    x = np.arange(n)
    out = []
    for i in range(n - 1):
        out.append((y[i + 1:] - y[i]) / (x[i + 1:] - x[i]))
    return np.concatenate(out)


def estimate_ttd(p99_series, slo_p99, cycle_seconds=CYCLE_SECONDS):
    """
    Estime le temps avant franchissement du SLO p99.

    p99_series : valeurs p99 recentes (ordre chronologique, la plus recente en
                 dernier). slo_p99 : seuil SLO.

    Retour (ou None si pas de tendance haussiere exploitable) :
    {
        "ttd_minutes": float,          # estimation ponctuelle (mediane des pentes)
        "ttd_low":     float | None,   # borne basse (pente rapide -> breach plus tot)
        "ttd_high":    float | None,   # borne haute (pente lente -> breach plus tard)
        "slope_ms_per_min": float,     # pente mediane
        "already_breaching": bool,
    }
    """
    y = _finite(p99_series)
    if len(y) < 3 or slo_p99 is None:
        return None

    slopes = _pairwise_slopes(y)
    if slopes.size == 0:
        return None

    slope = float(np.median(slopes))              # ms/cycle = ms/min (cycle=60s)
    slope_per_min = slope * 60.0 / cycle_seconds
    current = y[-1]

    if current >= slo_p99:
        return {
            "ttd_minutes": 0.0, "ttd_low": 0.0, "ttd_high": 0.0,
            "slope_ms_per_min": round(slope_per_min, 2), "already_breaching": True,
        }

    if slope_per_min < MIN_SLOPE:
        return None  # pas (ou trop peu) de tendance haussiere : pas d'alerte precoce

    def ttd_for(s_per_cycle):
        s_per_min = s_per_cycle * 60.0 / cycle_seconds
        if s_per_min < MIN_SLOPE:
            return None
        minutes = (slo_p99 - current) / s_per_min
        return minutes if minutes >= 0 else None

    point = ttd_for(slope)
    if point is None or point > MAX_HORIZON_MIN:
        return None

    # Intervalle : pente rapide (p75) -> breach plus tot (ttd_low) ; pente lente
    # (p25) -> breach plus tard (ttd_high), None si la pente basse n'est plus haussiere.
    ttd_low = ttd_for(float(np.percentile(slopes, 75)))
    ttd_high = ttd_for(float(np.percentile(slopes, 25)))

    return {
        "ttd_minutes": round(point, 1),
        "ttd_low": round(ttd_low, 1) if ttd_low is not None else None,
        "ttd_high": round(ttd_high, 1) if ttd_high is not None else None,
        "slope_ms_per_min": round(slope_per_min, 2),
        "already_breaching": False,
    }
