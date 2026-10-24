#!/usr/bin/env python3
"""
train_model.py -- entraine le modele Isolation Forest global (spec 5.4 / 8.3).

Pipeline :
  1. Charge l'historique endpoint_features sur --lookback-days.
  2. Charge endpoint_baseline en memoire (buckets exacts + fallback MIN/AVG/MAX).
  3. Charge les fenetres d'injection (scenario-runner/results/*.json) et les EXCLUT
     de l'entrainement : les fautes injectees sont le TEST SET, jamais l'entrainement
     (spec 8.1 / 8.2). Correctness, pas une option.
  4. Construit la matrice de features endpoint-relatives (features.compute_features)
     par fenetre glissante, en excluant la fenetre la plus recente incomplete.
  5. Fit IsolationForest, calibration sur la distribution trailing.
  6. Sanity gate (shift de distribution vs modele precedent sur fenetre de reference).
  7. Sauvegarde artefact horodate + promotion du pointeur "latest" si le gate passe.

Lancement :
    python train_model.py                       # run-once, promotion si gate OK
    python train_model.py --dry-run             # entraine, n'ecrit rien
    python train_model.py --force               # promeut meme si le gate echoue
    python train_model.py --loop --interval 86400   # refit nightly (spec 8.3)
"""

import argparse
import glob
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import numpy as np
import psycopg2

import ml_model
from features import compute_features, vectorize, WINDOW_SIZE, MIN_WINDOW
from baseline_utils import pg_dow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")

RESULTS_DIR = os.environ.get(
    "GROUND_TRUTH_DIR",
    os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "scenario-runner", "results"
    )),
)

# Metriques dont on a besoin de la baseline saisonniere pour les features derivees.
BASELINE_METRICS = ["p50_ms", "p95_ms", "p99_ms", "rps"]

# Marge autour des fenetres d'injection exclues (residu de la faute dans les rates).
INJECTION_MARGIN = timedelta(minutes=5)

MIN_TRAIN_SAMPLES = 200


def load_feature_rows(conn, lookback_days):
    """Rows endpoint_features valides sur la fenetre, groupees par endpoint (time asc)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT endpoint_id, time, rps, p50_ms, p95_ms, p99_ms, error_rate_5xx
        FROM endpoint_features
        WHERE time >= NOW() - (%s || ' days')::interval
          AND p99_ms IS NOT NULL AND p99_ms < 'NaN'::float8
        ORDER BY endpoint_id, time ASC
    """, (lookback_days,))
    rows = cur.fetchall()
    cur.close()
    by_ep = {}
    for endpoint_id, t, rps, p50, p95, p99, err in rows:
        by_ep.setdefault(endpoint_id, []).append({
            "time": t, "rps": rps, "p50_ms": p50, "p95_ms": p95,
            "p99_ms": p99, "error_rate_5xx": err,
        })
    return by_ep


BASELINE_COLUMNS = ("endpoint_id", "metric", "dow", "hour_bucket", "p10", "p50", "p90")


def load_baseline_lookup(conn):
    """
    Charge endpoint_baseline (base de donnees) en memoire -- voir
    baseline_lookup_from_rows pour la structure renvoyee.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT endpoint_id, metric, dow, hour_bucket, p10, p50, p90
        FROM endpoint_baseline
    """)
    rows = [dict(zip(BASELINE_COLUMNS, t)) for t in cur.fetchall()]
    cur.close()
    return baseline_lookup_from_rows(rows)


def baseline_lookup_from_rows(rows):
    """
    Baseline en memoire a partir de lignes endpoint_baseline (dicts avec les
    colonnes BASELINE_COLUMNS) : lues en base OU dans l'instantane d'une campagne.
      exact[(endpoint, metric, dow, hour)] = (p10, p50, p90)
      fallback[(endpoint, metric)]         = (min p10, avg p50, max p90)  # cf get_baseline
    Valeurs vides ignorees dans les agregats. Fonction pure (aucune lecture en base).
    """
    exact = {}
    agg = {}
    for r in rows:
        ep, metric, p10, p50, p90 = r["endpoint_id"], r["metric"], r["p10"], r["p50"], r["p90"]
        exact[(ep, metric, int(r["dow"]), int(r["hour_bucket"]))] = (p10, p50, p90)
        a = agg.setdefault((ep, metric), {"p10": [], "p50": [], "p90": []})
        if p10 is not None: a["p10"].append(p10)
        if p50 is not None: a["p50"].append(p50)
        if p90 is not None: a["p90"].append(p90)
    fallback = {}
    for key, a in agg.items():
        fallback[key] = (
            min(a["p10"]) if a["p10"] else None,
            (sum(a["p50"]) / len(a["p50"])) if a["p50"] else None,
            max(a["p90"]) if a["p90"] else None,
        )
    return exact, fallback


def baseline_for(exact, fallback, endpoint_id, dt):
    """Baselines {metric: (p10,p50,p90)} pour un point, bucket exact puis fallback."""
    dow, hour = pg_dow(dt), dt.hour
    out = {}
    for m in BASELINE_METRICS:
        b = exact.get((endpoint_id, m, dow, hour))
        if b is None:
            b = fallback.get((endpoint_id, m))
        out[m] = b
    return out


def load_injection_windows():
    """Fenetres [injected_at, cleared_at] de toutes les injections ground-truth."""
    windows = []
    for path in glob.glob(os.path.join(RESULTS_DIR, "**", "*.json"), recursive=True):
        try:
            with open(path) as f:
                data = json.load(f)
            faults = data["faults"] if isinstance(data, dict) and "faults" in data else data
            if not isinstance(faults, list):
                continue
            for e in faults:
                ia, ca = e.get("injected_at"), e.get("cleared_at")
                if not ia or not ca:
                    continue
                a = datetime.fromisoformat(ia)
                b = datetime.fromisoformat(ca)
                if a.tzinfo is None: a = a.replace(tzinfo=timezone.utc)
                if b.tzinfo is None: b = b.replace(tzinfo=timezone.utc)
                windows.append((a - INJECTION_MARGIN, b + INJECTION_MARGIN))
        except Exception as e:
            log.warning(f"Ground truth illisible {path}: {e}")
    return windows


def in_injection(dt, windows):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return any(a <= dt <= b for a, b in windows)


def build_matrix(by_ep, exact, fallback, windows):
    """
    Matrice de features endpoint-relatives par fenetre glissante. Exclut :
      - les points dans une fenetre d'injection (test set),
      - la fenetre la plus recente incomplete (watermark, spec 5.2),
      - les fenetres trop courtes (< MIN_WINDOW) pour une pente fiable.
    """
    X = []
    excluded_injection = 0
    for endpoint_id, rows in by_ep.items():
        # Watermark : on ignore le dernier point (fenetre potentiellement incomplete).
        usable = rows[:-1] if len(rows) > 1 else rows
        for i in range(len(usable)):
            point = usable[i]
            if in_injection(point["time"], windows):
                excluded_injection += 1
                continue
            window = usable[max(0, i - WINDOW_SIZE + 1): i + 1]
            if len(window) < MIN_WINDOW:
                continue
            baselines = baseline_for(exact, fallback, endpoint_id, point["time"])
            feat = compute_features(window, baselines)
            X.append(vectorize(feat))
    return np.array(X, dtype=float), excluded_injection


# --- Garde-fous de promotion (audit #13) -----------------------------------
RECALL_TOLERANCE = 0.05      # baisse de recall toleree vs modele courant
FP_TOLERANCE_REL = 0.50      # hausse relative de FP toleree
FP_TOLERANCE_ABS = 2         # + marge absolue (petits nombres)
MAX_DATA_AGE_HOURS = 24      # au-dela, donnees stale -> promotion deconseillee


def data_freshness_hours(conn):
    """Age (heures) de la derniere ligne endpoint_features, ou None si vide."""
    cur = conn.cursor()
    cur.execute("SELECT EXTRACT(EPOCH FROM (now() - max(time)))/3600 FROM endpoint_features")
    row = cur.fetchone()
    cur.close()
    return float(row[0]) if row and row[0] is not None else None


def _recall_fp(ev, windows, by_ep, exact, fallback, bundle):
    """(recall, faux positifs) du detecteur layered avec ce modele, sur la campagne."""
    onsets = ev.build_onsets(by_ep, exact, fallback, bundle)
    per_type, fp, _ = ev.evaluate(windows, onsets)
    tp = sum(d["layered"].tp for d in per_type.values())
    fn = sum(d["layered"].fn for d in per_type.values())
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return recall, fp["layered"]


# Statuts de validation autorisant la promotion. "initialisation" = premier modele,
# promu sans reference : il n'est PAS valide (consigne dans meta["validation"]).
PROMOTABLE_VALIDATION = ("ok", "initialisation")


def _windows_with_data(windows, by_ep):
    """Nombre de fenetres d'injection contenant au moins une mesure endpoint_features."""
    times = [r["time"] for rows in by_ep.values() for r in rows]
    return sum(1 for w in windows if any(w.injected_at <= t <= w.cleared_at for t in times))


def validate_promotion(conn, candidate_bundle, store=None):
    """
    Rejoue l'evaluation layered avec le modele CANDIDAT et le modele COURANT sur la
    campagne d'evaluation la plus recente, et renvoie (statut, rapport) :
      initialisation : aucun modele courant -> premier modele, NON valide
      ok             : le candidat ne degrade ni le recall ni les FP (tolerances)
      regression     : le candidat degrade la detection
      impossible     : pas de campagne, campagne vide, ou aucune mesure dans ses
                       fenetres d'injection
    Une validation impossible n'est jamais une reussite.
    store : diagnostic (etat, bundle, detail) de ml_model.model_store_state(), pris
    AVANT toute ecriture du cycle (sinon la fenetre de reference fraichement creee
    ferait paraitre le depot incoherent) ; recalcule si absent.
    Complementaire du sanity-gate (qui, lui, ne regarde que la distribution des scores).
    Import paresseux d'evaluate_layered pour eviter l'import circulaire.
    """
    from pathlib import Path
    import evaluate_layered as ev

    state, current, detail = store if store is not None else ml_model.model_store_state()
    if state == "absent":
        return "initialisation", ("validation : initialisation du premier modele, NON validee "
                                  "(aucun modele de reference)")
    if state != "ok":
        return "impossible", f"validation impossible : etat du modele en service '{state}' ({detail})"
    problems = ml_model.check_bundle_compat(candidate_bundle)
    if problems:
        return "impossible", f"validation impossible : candidat incompatible ({'; '.join(problems)})"

    input_path = ev.latest_campaign_input()
    if input_path is None:
        return "impossible", ("validation impossible : aucune campagne d'evaluation "
                              "(campaign_*/ground_truth)")
    windows = ev.load_windows(Path(input_path))
    if not windows:
        return "impossible", f"validation impossible : aucune fenetre d'injection dans {input_path}"

    since = min(w.injected_at for w in windows) - ev.PAD
    until = max(w.cleared_at for w in windows) + ev.PAD
    by_ep = ev.load_features_span(conn, since, until)
    covered = _windows_with_data(windows, by_ep)
    if covered == 0:
        return "impossible", (f"validation impossible : aucune mesure dans les fenetres "
                              f"d'injection de {input_path}")
    exact, fallback = load_baseline_lookup(conn)

    r_cand, fp_cand = _recall_fp(ev, windows, by_ep, exact, fallback, candidate_bundle)
    r_cur, fp_cur = _recall_fp(ev, windows, by_ep, exact, fallback, current)

    recall_ok = r_cand >= r_cur - RECALL_TOLERANCE
    fp_ok = fp_cand <= fp_cur + max(FP_TOLERANCE_ABS, int(fp_cur * FP_TOLERANCE_REL))
    ok = recall_ok and fp_ok
    report = (f"validation ({input_path}, {covered}/{len(windows)} fenetres avec mesures) : "
              f"recall {r_cur:.2f}->{r_cand:.2f}, FP {fp_cur}->{fp_cand} -> "
              + ("OK" if ok else "REGRESSION"))
    return ("ok" if ok else "regression"), report


def run_validation(conn, candidate_bundle, store=None):
    """validate_promotion, une erreur donnant le statut 'impossible' (jamais une reussite)."""
    try:
        return validate_promotion(conn, candidate_bundle, store)
    except Exception as e:
        return "impossible", f"validation impossible (erreur : {e})"


def decide_promotion(ok_gate, val_status, fresh, force):
    """
    (promouvoir, raisons_du_refus). Promotion si sanity-gate, validation
    (ok ou initialisation) et fraicheur sont satisfaits ; --force (option manuelle,
    jamais appliquee automatiquement) passe outre.
    """
    reasons = [name for name, ok in (
        ("sanity-gate", ok_gate),
        (f"validation-{val_status}", val_status in PROMOTABLE_VALIDATION),
        ("fraicheur", fresh),
    ) if not ok]
    return (not reasons) or force, reasons


def train_and_maybe_promote(conn, args):
    by_ep = load_feature_rows(conn, args.lookback_days)
    if not by_ep:
        log.error("Aucune donnee endpoint_features sur la fenetre -- entrainement annule")
        return False
    exact, fallback = load_baseline_lookup(conn)
    windows = load_injection_windows()
    log.info(f"Endpoints={len(by_ep)}  baselines_exacts={len(exact)}  fenetres_injection={len(windows)}")

    X, excluded = build_matrix(by_ep, exact, fallback, windows)
    log.info(f"Matrice features : {X.shape}  (points exclus car injection: {excluded})")
    if len(X) < MIN_TRAIN_SAMPLES:
        log.error(f"Trop peu d'echantillons ({len(X)} < {MIN_TRAIN_SAMPLES}) -- entrainement annule")
        return False

    # --- Etat du depot de modeles, AVANT toute ecriture (ensure_reference et
    # save_artifact creent le dossier). Seuls "absent" (1er modele) et "ok"
    # permettent de continuer ; tout etat douteux arrete le cycle sans rien ecrire,
    # --force compris.
    store = ml_model.model_store_state()
    store_state, _current, store_detail = store
    if store_state not in ("absent", "ok"):
        log.error(f"Depot de modeles '{store_state}' : {store_detail} -- cycle arrete, aucune ecriture")
        return False
    if store_state == "ok" and not os.path.exists(ml_model.reference_path()):
        log.error("Modele en service sans fenetre de reference -- cycle arrete, aucune ecriture")
        return False
    prev_state, previous, prev_detail = ml_model.load_previous_versioned_state()
    if store_state == "ok" and prev_state != "ok":
        log.error(f"Artefact precedent '{prev_state}' : {prev_detail} -- cycle arrete, aucune ecriture")
        return False

    model = ml_model.train(X, contamination=args.contamination)
    raw = ml_model.raw_anomaly(model, X)
    calib = ml_model.calibration_params(raw)

    trained_at = datetime.now(timezone.utc).isoformat()
    meta = ml_model.build_meta(trained_at, X, args.contamination, WINDOW_SIZE, args.lookback_days)
    meta["calibration"] = calib

    # Candidat incompatible avec le code actuel : arret sans ecriture, --force compris.
    problems = ml_model.check_bundle_compat({"model": model, "meta": meta})
    if problems:
        log.error(f"Candidat incompatible ({'; '.join(problems)}) -- cycle arrete, aucune ecriture")
        return False

    # --- Garde-fous de promotion (dans l'ordre) ---
    # 1. Sanity gate : shift de distribution des scores vs modele precedent
    #    (strict des qu'un modele existe : pas de reussite par defaut).
    try:
        reference = ml_model.ensure_reference(X)
    except ml_model.ReferenceWindowError as e:
        log.error(f"{e} -- cycle arrete, aucune ecriture")
        return False
    ok_gate, gate_report = ml_model.sanity_gate(model, previous, reference,
                                                strict=(store_state == "ok"))
    log.info(f"Sanity gate : {gate_report['reason']}")

    # 2. Validation d'eval : le candidat ne doit pas degrader la detection.
    candidate = {"model": model, "meta": meta}
    val_status, val_report = run_validation(conn, candidate, store)
    (log.info if val_status == "ok" else log.warning)(val_report)
    meta["validation"] = {"status": val_status, "report": val_report}

    # 3. Fraicheur : ne pas promouvoir un modele entraine sur des donnees stale.
    age = data_freshness_hours(conn)
    fresh = age is None or age <= MAX_DATA_AGE_HOURS
    if not fresh:
        log.warning(f"Donnees stale (derniere feature il y a {age:.1f}h > {MAX_DATA_AGE_HOURS}h)")

    if args.dry_run:
        log.info(f"--dry-run : aucun artefact ecrit (gate={ok_gate} validation={val_status} fresh={fresh})")
        return True

    promote, reasons = decide_promotion(ok_gate, val_status, fresh, args.force)
    if not promote:
        log.warning(f"Promotion REFUSEE ({', '.join(reasons)}) -- artefact conserve, latest inchange")
    elif reasons:
        log.warning(f"Garde-fous non satisfaits ({', '.join(reasons)}) mais --force : promotion forcee")
    elif val_status == "initialisation":
        log.warning("Promotion INITIALE du premier modele : initialisation, NON validee")

    path = ml_model.save_artifact(model, meta, promote=promote)
    if not promote:
        log.warning(f"Artefact NON promu : {path}")
    log.info(f"Entrainement termine : n={meta['n_samples']} contamination={args.contamination} "
             f"calib(p50={calib['p50']:.3f}, p99={calib['p99']:.3f})")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--contamination", type=float, default=0.02)
    parser.add_argument("--dry-run", action="store_true", help="Entraine sans rien ecrire")
    parser.add_argument("--force", action="store_true", help="Promeut meme si le sanity gate echoue")
    parser.add_argument("--loop", action="store_true", help="Refit periodique (nightly)")
    parser.add_argument("--interval", type=int, default=86400, help="Intervalle en secondes (defaut 24h)")
    args = parser.parse_args()

    if args.loop:
        log.info(f"Trainer en boucle -- interval={args.interval}s")
        while True:
            try:
                conn = psycopg2.connect(DB_URL)
                train_and_maybe_promote(conn, args)
                conn.close()
            except Exception as e:
                log.error(f"Cycle d'entrainement echoue : {e}")
            time.sleep(args.interval)
    else:
        conn = psycopg2.connect(DB_URL)
        try:
            train_and_maybe_promote(conn, args)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
