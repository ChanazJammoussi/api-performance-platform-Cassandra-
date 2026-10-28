"""
ml_model.py -- wrapper Isolation Forest, calibration, attribution et cycle de vie
de l'artefact (spec 5.4 / 8.3).

Responsabilites :
  - train()            : fit d'un IsolationForest global (features endpoint-relatives)
  - raw_anomaly()      : score brut (plus grand = plus anormal)
  - calibrate()        : mappe le score brut vers [0,1] via la distribution trailing
  - attribute()        : contribution par feature (ablation sur le score) -> top-3
  - save_artifact()    : ecrit un artefact horodate + promeut le pointeur "latest"
  - load_latest()      : charge l'artefact promu (best-effort, detecteur)
  - model_store_state(): etat du depot de modeles pour la promotion (strict, sans ecriture)
  - check_bundle_compat(): compatibilite d'un artefact avec FEATURE_NAMES
  - sanity_gate()      : verifie un shift de distribution sur une fenetre de reference

Format artefact (joblib) : {"model": IsolationForest, "meta": {...}}.
meta contient feature_names, trained_at, n_samples, contamination, window_size,
calibration (percentiles du score brut), feature_medians (pour l'attribution),
lookback_days, sklearn_version, season (mode de l'horloge saisonniere, season.py ;
absent = artefact anterieur, considere comme mode reel).
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
import season

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


def load_previous_versioned_state():
    """
    Comme load_previous_versioned, mais distingue l'absence d'artefact d'un artefact
    illisible : (etat, bundle|None, detail), etat in {"aucun", "illisible", "ok"}.
    """
    versions = sorted(glob.glob(os.path.join(MODEL_DIR, "iforest_2*.joblib")))
    if not versions:
        return "aucun", None, "aucun artefact horodate"
    name = os.path.basename(versions[-1])
    try:
        bundle = joblib.load(versions[-1])
    except Exception as e:
        return "illisible", None, f"{name} : chargement echoue ({e})"
    if not isinstance(bundle, dict) or "model" not in bundle:
        return "illisible", None, f"{name} : structure {{model, meta}} attendue"
    return "ok", bundle, name


# ---------------------------------------------------------------------------
# Etat du depot de modeles et compatibilite (promotion uniquement)
# ---------------------------------------------------------------------------
# Le detecteur garde load_latest() (best-effort). La promotion, elle, exige un
# diagnostic strict : seule une absence REELLE de tout modele autorise
# l'initialisation ; tout etat douteux bloque.

def _finite(v):
    try:
        return bool(np.isfinite(float(v)))
    except (TypeError, ValueError):
        return False


def check_bundle_compat(bundle):
    """
    Liste des problemes de compatibilite d'un artefact {model, meta} avec le code
    actuel (vide = compatible) : feature_names identiques a FEATURE_NAMES (noms ET
    ordre), model.n_features_in_, calibration p50/p99 finie, feature_medians,
    mode saisonnier identique au mode actuel (season.describe()).
    """
    if not isinstance(bundle, dict) or "model" not in bundle or "meta" not in bundle:
        return ["structure : dict {model, meta} attendu"]
    model, meta = bundle["model"], bundle["meta"]
    if not isinstance(meta, dict):
        return ["meta : dict attendu"]
    problems = []
    names = meta.get("feature_names")
    if names is None:
        problems.append("meta.feature_names absent")
    elif list(names) != list(FEATURE_NAMES):
        if sorted(names) == sorted(FEATURE_NAMES):
            problems.append("meta.feature_names : ordre different de FEATURE_NAMES")
        else:
            problems.append(f"meta.feature_names differents de FEATURE_NAMES : {list(names)}")
    n_in = getattr(model, "n_features_in_", None)
    if n_in != len(FEATURE_NAMES):
        problems.append(f"model.n_features_in_={n_in} != {len(FEATURE_NAMES)}")
    calib = meta.get("calibration")
    if not isinstance(calib, dict) or not all(_finite(calib.get(k)) for k in ("p50", "p99")):
        problems.append("meta.calibration : p50 et p99 finis requis")
    medians = meta.get("feature_medians")
    try:
        medians_ok = len(medians) == len(FEATURE_NAMES) and all(_finite(v) for v in medians)
    except TypeError:
        medians_ok = False
    if not medians_ok:
        problems.append(f"meta.feature_medians : {len(FEATURE_NAMES)} valeurs finies requises")
    # Un modele entraine sur une autre horloge saisonniere (baseline differente) est
    # refuse : pas de melange mode reel / journee compressee.
    if not season.same_mode(meta.get("season"), season.describe()):
        problems.append(f"meta.season {meta.get('season')!r} different du mode actuel "
                        f"{season.describe()!r}")
    return problems


def model_store_state():
    """
    Diagnostic du depot de modeles, SANS ecriture : (etat, bundle|None, detail).
      absent         : dossier present et lisible, aucun iforest_*.joblib ni
                       fenetre de reference -> seule situation d'initialisation
      ok             : latest charge et compatible
      dossier_absent : MODEL_DIR introuvable (ex. volume non monte)
      erreur         : MODEL_DIR non lisible, ou erreur inattendue
      incoherent     : pas de latest mais d'autres artefacts / une reference
      illisible      : latest present mais non chargeable ou mal structure
      incompatible   : latest charge mais incompatible (check_bundle_compat)
    """
    try:
        if not os.path.exists(MODEL_DIR):
            return "dossier_absent", None, f"{MODEL_DIR} introuvable"
        if not os.path.isdir(MODEL_DIR):
            return "erreur", None, f"{MODEL_DIR} n'est pas un dossier"
        names = os.listdir(MODEL_DIR)
    except Exception as e:
        return "erreur", None, f"lecture de {MODEL_DIR} impossible : {e}"

    artefacts = sorted(n for n in names if n.startswith("iforest_") and n.endswith(".joblib"))
    if LATEST_NAME not in names:
        if not artefacts and REFERENCE_NAME not in names:
            return "absent", None, "aucun modele ni fenetre de reference"
        presents = artefacts + ([REFERENCE_NAME] if REFERENCE_NAME in names else [])
        return "incoherent", None, f"{LATEST_NAME} absent mais presents : {presents}"

    try:
        bundle = joblib.load(latest_path())
    except Exception as e:
        return "illisible", None, f"chargement de {LATEST_NAME} echoue : {e}"
    if not isinstance(bundle, dict) or "model" not in bundle or "meta" not in bundle:
        return "illisible", None, f"{LATEST_NAME} : structure {{model, meta}} attendue"
    problems = check_bundle_compat(bundle)
    if problems:
        return "incompatible", None, "; ".join(problems)
    return "ok", bundle, "modele en service charge et compatible"


class ReferenceWindowError(Exception):
    """Fenetre de reference existante mais illisible ou mal formee."""


def ensure_reference(X, size=500, random_state=42):
    """
    Fenetre de reference FIXE pour le sanity gate (spec 8.3). Creee au 1er
    entrainement a partir d'un echantillon deterministe de X, puis figee (jamais
    reecrite) pour que les comparaisons de distribution soient stables dans le temps.
    Retourne la matrice de reference. Une reference existante illisible ou mal
    formee leve ReferenceWindowError (jamais recreee en silence).
    """
    path = reference_path()
    if os.path.exists(path):
        try:
            ref = np.load(path)
        except Exception as e:
            raise ReferenceWindowError(f"fenetre de reference illisible ({path}) : {e}") from e
        if ref.ndim != 2 or ref.shape[1] != len(FEATURE_NAMES):
            raise ReferenceWindowError(
                f"fenetre de reference mal formee ({path}) : forme {ref.shape}, "
                f"{len(FEATURE_NAMES)} colonnes attendues")
        return ref
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


def sanity_gate(new_model, previous_bundle, reference_X, strict=False):
    """
    Sanity gate avant promotion (spec 8.3) : compare la distribution de scores du
    nouveau modele a celle de l'ancien, sur la fenetre de reference fixe.

    Retourne (ok: bool, report: dict). Si aucun modele precedent : promotion
    autorisee (premier modele). Sinon on refuse si le KS depasse SANITY_KS_MAX.
    strict=True (un modele existe deja) : reference absente/illisible ou fenetre
    vide -> refus, jamais de reussite par defaut.
    """
    report = {"ks": None, "threshold": SANITY_KS_MAX, "reason": ""}
    if previous_bundle is None:
        if strict:
            report["reason"] = "modele de reference absent ou illisible -> promotion REFUSEE"
            return False, report
        report["reason"] = "premier modele (pas de reference precedente)"
        return True, report
    if reference_X is None or len(reference_X) == 0:
        if strict:
            report["reason"] = "fenetre de reference vide -> promotion REFUSEE"
            return False, report
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
        "season": season.describe(),
    }
