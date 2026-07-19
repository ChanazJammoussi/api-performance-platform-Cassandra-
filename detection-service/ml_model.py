"""
ml_model.py -- wrapper Isolation Forest, calibration, attribution et cycle de vie
de l'artefact (spec 5.4 / 8.3).

Responsabilites :
  - train()            : fit d'un IsolationForest global (features endpoint-relatives)
  - raw_anomaly()      : score brut (plus grand = plus anormal)
  - calibrate()        : mappe le score brut vers [0,1] via la distribution trailing
  - attribute()        : contribution par feature (ablation sur le score) -> top-3
  - save_artifact()    : ecrit un artefact horodate + promeut le pointeur "latest"
  - load_latest()      : charge l'artefact promu (best-effort)
  - sanity_gate()      : verifie un shift de distribution sur une fenetre de reference

Format artefact (joblib) : {"model": IsolationForest, "meta": {...}}.
meta contient feature_names, trained_at, n_samples, contamination, window_size,
calibration (percentiles du score brut), feature_medians (pour l'attribution),
lookback_days, sklearn_version.
"""

import os
import glob
import json
import logging
import numpy as np
import joblib
import sklearn
from sklearn.ensemble import IsolationForest

from features import FEATURE_NAMES

log = logging.getLogger(__name__)

# Repertoire des artefacts (volume partage trainer <-> detector en conteneur).
MODEL_DIR = os.environ.get(
    "MODEL_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"),
)
# Pointeur vers le dernier artefact promu, charge par le detecteur.
LATEST_NAME = "iforest_latest.joblib"
# Fenetre de reference fixe pour le sanity gate (creee au 1er entrainement).
REFERENCE_NAME = "reference_window.npy"

# Seuil du sanity gate : statistique de Kolmogorov-Smirnov entre la distribution
# de scores du nouveau modele et celle de l'ancien sur la fenetre de reference.
# Au-dela, on refuse la promotion (drift trop important).
SANITY_KS_MAX = 0.35


def latest_path():
    return os.path.join(MODEL_DIR, LATEST_NAME)


def reference_path():
    return os.path.join(MODEL_DIR, REFERENCE_NAME)


# ---------------------------------------------------------------------------
# Entrainement / scoring
# ---------------------------------------------------------------------------

def train(X, contamination=0.02, random_state=42, n_estimators=200):
    """Fit d'un IsolationForest global sur la matrice de features X (n x d)."""
    model = IsolationForest(
        n_estimators=n_estimators,
        contamination=contamination,
        random_state=random_state,
        n_jobs=-1,
    )
    model.fit(X)
    return model


def raw_anomaly(model, X):
    """
    Score brut d'anomalie : plus grand = plus anormal.
    sklearn.score_samples renvoie l'oppose (plus grand = plus normal), on inverse.
    Accepte un vecteur 1D ou une matrice 2D, renvoie toujours un np.ndarray 1D.
    """
    X = np.atleast_2d(X)
    return -model.score_samples(X)


def calibration_params(raw_scores):
    """Percentiles du score brut sur l'historique d'entrainement (pour calibrate)."""
    raw = np.asarray(raw_scores, dtype=float)
    return {
        "p50": float(np.percentile(raw, 50)),
        "p95": float(np.percentile(raw, 95)),
        "p99": float(np.percentile(raw, 99)),
        "max": float(np.max(raw)),
    }


def calibrate(raw, calib):
    """
    Calibre un score brut vers [0,1] contre la distribution trailing (spec 8.3).
    Ancrage : p50 -> 0 (comportement typique), p99 -> 1 (extreme observe a
    l'entrainement). Lineaire borne entre les deux, clampe au-dela.
    """
    lo = calib.get("p50", 0.0)
    hi = calib.get("p99", 1.0)
    raw = np.asarray(raw, dtype=float)
    if hi - lo < 1e-9:
        return np.zeros_like(raw)
    return np.clip((raw - lo) / (hi - lo), 0.0, 1.0)


def attribute(model, x, feature_medians, calib=None, top_k=3):
    """
    Attribution par feature pour un echantillon (approximation interpretable de la
    "per-feature path-depth attribution" du spec 8.3).

    Methode : ablation. Pour chaque feature j, on remplace x[j] par sa mediane
    d'entrainement (valeur "typique") et on mesure la chute du score d'anomalie.
    Une feature dont le retrait fait baisser le score est celle qui tirait
    l'echantillon vers l'anomalie -> contribution positive.

    Retourne une liste [(feature_name, contribution_float), ...] triee par |contrib|
    decroissante, limitee a top_k. Contributions exprimees en score calibre si
    `calib` est fourni, sinon en score brut.
    """
    x = np.asarray(x, dtype=float).ravel()
    base_raw = float(raw_anomaly(model, x)[0])

    contribs = []
    for j, name in enumerate(FEATURE_NAMES):
        x_ablated = x.copy()
        x_ablated[j] = feature_medians[j]
        ablated_raw = float(raw_anomaly(model, x_ablated)[0])
        delta = base_raw - ablated_raw  # >0 : la feature poussait vers l'anomalie
        contribs.append((name, delta))

    if calib is not None:
        span = max(calib.get("p99", 1.0) - calib.get("p50", 0.0), 1e-9)
        contribs = [(n, d / span) for n, d in contribs]

    contribs.sort(key=lambda t: abs(t[1]), reverse=True)
    return [(n, round(d, 4)) for n, d in contribs[:top_k]]


# ---------------------------------------------------------------------------
# Cycle de vie de l'artefact
# ---------------------------------------------------------------------------

def save_artifact(model, meta, promote=True):
    """
    Ecrit un artefact horodate `iforest_<stamp>.joblib` (l'ancien est conserve,
    jamais ecrase), et si promote=True copie vers le pointeur `latest`.
    Retourne le chemin de l'artefact horodate.
    """
    os.makedirs(MODEL_DIR, exist_ok=True)
    stamp = meta["trained_at"].replace(":", "").replace("-", "").replace("T", "_")[:15]
    versioned = os.path.join(MODEL_DIR, f"iforest_{stamp}.joblib")
    bundle = {"model": model, "meta": meta}
    joblib.dump(bundle, versioned)
    log.info(f"Artefact ecrit : {versioned}")
    if promote:
        joblib.dump(bundle, latest_path())
        log.info(f"Artefact promu (latest) : {latest_path()}")
    return versioned


def load_latest():
    """Charge le bundle promu {model, meta}, ou None si absent (best-effort)."""
    path = latest_path()
    if not os.path.exists(path):
        return None
    try:
        return joblib.load(path)
    except Exception as e:
        log.error(f"Chargement artefact echoue ({path}): {e}")
        return None


def load_previous_versioned():
    """Charge l'artefact horodate le plus recent (l'ancien modele), hors `latest`."""
    versions = sorted(glob.glob(os.path.join(MODEL_DIR, "iforest_2*.joblib")))
    if not versions:
        return None
    try:
        return joblib.load(versions[-1])
    except Exception as e:
        log.error(f"Chargement artefact precedent echoue : {e}")
        return None


def ensure_reference(X, size=500, random_state=42):
    """
    Fenetre de reference FIXE pour le sanity gate (spec 8.3). Creee au 1er
    entrainement a partir d'un echantillon deterministe de X, puis figee (jamais
    reecrite) pour que les comparaisons de distribution soient stables dans le temps.
    Retourne la matrice de reference.
    """
    path = reference_path()
    if os.path.exists(path):
        return np.load(path)
    os.makedirs(MODEL_DIR, exist_ok=True)
    X = np.asarray(X, dtype=float)
    n = min(size, len(X))
    rng = np.random.default_rng(random_state)
    idx = rng.choice(len(X), size=n, replace=False)
    ref = X[idx]
    np.save(path, ref)
    log.info(f"Fenetre de reference creee ({n} points) : {path}")
    return ref


def _ks_statistic(a, b):
    """Statistique de Kolmogorov-Smirnov (max ecart entre CDF empiriques), sans scipy."""
    a = np.sort(np.asarray(a, dtype=float))
    b = np.sort(np.asarray(b, dtype=float))
    grid = np.concatenate([a, b])
    cdf_a = np.searchsorted(a, grid, side="right") / len(a)
    cdf_b = np.searchsorted(b, grid, side="right") / len(b)
    return float(np.max(np.abs(cdf_a - cdf_b)))


def sanity_gate(new_model, previous_bundle, reference_X):
    """
    Sanity gate avant promotion (spec 8.3) : compare la distribution de scores du
    nouveau modele a celle de l'ancien, sur la fenetre de reference fixe.

    Retourne (ok: bool, report: dict). Si aucun modele precedent : promotion
    autorisee (premier modele). Sinon on refuse si le KS depasse SANITY_KS_MAX.
    """
    report = {"ks": None, "threshold": SANITY_KS_MAX, "reason": ""}
    if previous_bundle is None:
        report["reason"] = "premier modele (pas de reference precedente)"
        return True, report
    if reference_X is None or len(reference_X) == 0:
        report["reason"] = "fenetre de reference vide -> promotion par defaut"
        return True, report

    new_scores = raw_anomaly(new_model, reference_X)
    old_scores = raw_anomaly(previous_bundle["model"], reference_X)
    ks = _ks_statistic(new_scores, old_scores)
    report["ks"] = round(ks, 4)
    ok = ks <= SANITY_KS_MAX
    report["reason"] = (
        f"KS={ks:.3f} <= {SANITY_KS_MAX} : distribution stable, promotion OK"
        if ok else
        f"KS={ks:.3f} > {SANITY_KS_MAX} : shift de distribution, promotion REFUSEE"
    )
    return ok, report


def build_meta(trained_at, X, contamination, window_size, lookback_days):
    """Assemble les metadonnees d'artefact (calibration + medians pour attribution)."""
    return {
        "feature_names": FEATURE_NAMES,
        "trained_at": trained_at,
        "n_samples": int(len(X)),
        "contamination": float(contamination),
        "window_size": int(window_size),
        "lookback_days": int(lookback_days),
        "feature_medians": [float(m) for m in np.median(np.asarray(X), axis=0)],
        "sklearn_version": sklearn.__version__,
    }
