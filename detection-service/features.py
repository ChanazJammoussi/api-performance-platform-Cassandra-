"""
features.py -- features derivees "read-time" pour la couche ML (spec 5.2 / 5.4 / 8.3).

Math pure (numpy uniquement) : aucune dependance a la base ni a sklearn, pour que
la MEME fonction serve a l'entrainement (offline, fenetre glissante sur l'historique)
et a l'inference (online, derniere fenetre). Le detecteur et le job d'entrainement
partagent ainsi exactement la meme representation.

INVARIANT SPEC (8.3) : toutes les features sont *endpoint-relatives* (deviations
baseline normalisees, ratios, pentes normalisees). C'est ce qui rend viable un seul
modele Isolation Forest GLOBAL sur tous les endpoints. On n'injecte jamais de niveau
absolu en ms (p99_ms brut) comme feature.

Une "fenetre" est une liste de dicts ordonnee par time croissant (le plus recent en
dernier), chaque dict portant au minimum : p50_ms, p95_ms, p99_ms, rps, error_rate_5xx.
"""

import numpy as np

# Ordre canonique du vecteur de features. detector.py et train_model.py DOIVENT
# produire les colonnes dans cet ordre ; l'artefact memorise FEATURE_NAMES pour
# detecter toute divergence de schema apres un refactor.
FEATURE_NAMES = [
    "baseline_dev_p50",   # deviation signee de p50 vs sa bande saisonniere
    "baseline_dev_p95",   # deviation signee de p95 vs sa bande
    "baseline_dev_p99",   # deviation signee de p99 vs sa bande
    "error_rate_5xx",     # taux d'erreur serveur (deja endpoint-agnostique, en %)
    "latency_slope",      # pente Theil-Sen de p99 normalisee par le niveau attendu
    "p99_over_p50",       # ratio : lourdeur de la queue (deja sans dimension)
    "rps_delta",          # variation relative de charge (nouveaute de trafic, pas panne)
]

# Taille de la fenetre glissante (nombre de cycles ~60s). ~10 min de contexte.
WINDOW_SIZE = 10

# Minimum de points pour estimer une pente ; en-deca la pente vaut 0.
MIN_WINDOW = 3


def theil_sen_slope(y):
    """
    Pente robuste de Theil-Sen : mediane des pentes de toutes les paires de points
    (indices comme abscisse, cadence supposee reguliere). Insensible aux outliers,
    contrairement a une regression OLS. Retourne 0.0 si moins de MIN_WINDOW points.
    """
    y = np.asarray([v for v in y if v is not None and np.isfinite(v)], dtype=float)
    n = len(y)
    if n < MIN_WINDOW:
        return 0.0
    x = np.arange(n)
    slopes = []
    for i in range(n - 1):
        dx = x[i + 1:] - x[i]
        dy = y[i + 1:] - y[i]
        slopes.append(dy / dx)
    if not slopes:
        return 0.0
    return float(np.median(np.concatenate(slopes)))


def signed_deviation(obs, p10, p50, p90):
    """
    Deviation signee de `obs` vs la bande saisonniere [p10, p90], normalisee par la
    largeur de bande (spec 5.3 : 0 dans la bande, distance scalee en dehors).
      obs > p90 -> positif (au-dessus = degradation pour la latence)
      obs < p10 -> negatif (en-dessous)
      dans la bande -> 0
    Contrairement a baseline_utils.compute_deviation (unilaterale, degradation seule),
    cette version est bilaterale : le modele voit les deux directions, la direction
    d'alerte etant geree separement par le direction gating.
    """
    if obs is None or p10 is None or p90 is None:
        return 0.0
    if not (np.isfinite(obs) and np.isfinite(p10) and np.isfinite(p90)):
        return 0.0
    band = p90 - p10
    if band < 1e-6:
        return 0.0
    if obs > p90:
        return (obs - p90) / band
    if obs < p10:
        return (obs - p10) / band
    return 0.0


def _safe_ratio(num, den):
    if num is None or den is None:
        return 0.0
    if not np.isfinite(num) or not np.isfinite(den) or abs(den) < 1e-6:
        return 0.0
    return float(num) / float(den)


def _last_valid(window, key):
    for row in reversed(window):
        v = row.get(key)
        if v is not None and np.isfinite(v):
            return float(v)
    return 0.0


def _series(window, key):
    out = []
    for row in window:
        v = row.get(key)
        if v is not None and np.isfinite(v):
            out.append(float(v))
    return out


def direction_of(p99, p10, p90):
    """
    Direction de l'ecart de latence (spec 5.4 / 6.3) :
      p99 > p90 -> 'degradation'
      p99 < p10 -> 'improvement'
      sinon     -> 'normal'
    Sert au direction gating : la couche ML n'alerte que sur 'degradation'.
    """
    if p10 is None or p90 is None or p99 is None or not np.isfinite(p99):
        return "normal"
    if p99 > p90:
        return "degradation"
    if p99 < p10:
        return "improvement"
    return "normal"


def compute_features(window, baselines=None):
    """
    Calcule le dict de features derivees pour une fenetre.

    window    : liste de dicts (time croissant), le plus recent en dernier.
    baselines : dict {metric: (p10, p50, p90)} pour p50_ms / p95_ms / p99_ms / rps,
                baseline saisonniere du point courant (dow x hour). None -> deviations
                a 0 et normalisations en repli sur le niveau observe.

    Retourne un dict {feature_name: float} couvrant FEATURE_NAMES.
    """
    if not window:
        return {name: 0.0 for name in FEATURE_NAMES}

    baselines = baselines or {}

    p99_cur = _last_valid(window, "p99_ms")
    p95_cur = _last_valid(window, "p95_ms")
    p50_cur = _last_valid(window, "p50_ms")
    rps_cur = _last_valid(window, "rps")
    err_cur = _last_valid(window, "error_rate_5xx")

    def band(metric):
        b = baselines.get(metric)
        if not b:
            return (None, None, None)
        return b[0], b[1], b[2]

    p50_10, p50_50, p50_90 = band("p50_ms")
    p95_10, p95_50, p95_90 = band("p95_ms")
    p99_10, p99_50, p99_90 = band("p99_ms")
    rps_10, rps_50, rps_90 = band("rps")

    # Pente Theil-Sen de p99, normalisee par le niveau attendu (baseline p50 de p99)
    # -> croissance relative par cycle, comparable entre endpoints. Repli : niveau
    # observe courant, puis pente brute.
    raw_slope = theil_sen_slope(_series(window, "p99_ms"))
    slope_norm = p99_50 if (p99_50 and p99_50 > 1e-6) else (p50_cur if p50_cur > 1e-6 else None)
    latency_slope = raw_slope / slope_norm if slope_norm else raw_slope

    # Variation de rps sur le dernier cycle, normalisee par la charge attendue
    # (baseline rps) -> detecte une nouveaute de trafic (mitigation faux positifs 13).
    rps_series = _series(window, "rps")
    rps_delta_abs = (rps_series[-1] - rps_series[-2]) if len(rps_series) >= 2 else 0.0
    rps_norm = rps_50 if (rps_50 and rps_50 > 1e-6) else (rps_cur if rps_cur > 1e-6 else None)
    rps_delta = rps_delta_abs / rps_norm if rps_norm else 0.0

    return {
        "baseline_dev_p50": signed_deviation(p50_cur, p50_10, p50_50, p50_90),
        "baseline_dev_p95": signed_deviation(p95_cur, p95_10, p95_50, p95_90),
        "baseline_dev_p99": signed_deviation(p99_cur, p99_10, p99_50, p99_90),
        "error_rate_5xx":   err_cur,
        "latency_slope":    float(latency_slope),
        "p99_over_p50":     _safe_ratio(p99_cur, p50_cur),
        "rps_delta":        float(rps_delta),
    }


def vectorize(feat):
    """Projette un dict de features sur le vecteur ordonne FEATURE_NAMES."""
    return np.array([feat.get(name, 0.0) for name in FEATURE_NAMES], dtype=float)
