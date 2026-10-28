#!/usr/bin/env python3
"""
evaluate_layered.py -- comparaison LAYERED vs STATIC (spec 9.1 / 9.2 / 9.3).

Le livrable phare du projet (spec 9.3) : une table comparant le detecteur en
couches (baseline + Isolation Forest, avec direction gating) au detecteur static
(seuils SLO, layer 0) sur le meme jeu de fautes injectees.

Methode : plutot que de faire tourner deux detecteurs en parallele, on REJOUE les
deux hors-ligne sur l'historique endpoint_features, avec exactement la meme logique
de score que detector.py, puis on applique la meme hysteresis (M cycles) pour
obtenir les instants de FIRING. Chaque instant est ensuite matche aux fenetres
d'injection ground-truth. Reproductible, deterministe, sans etat live.

Metriques (spec 9.2) par type de faute :
  - detection rate (TP / injections)         static vs layered
  - delai de detection median (injection -> firing)
  - lead time (gradual) : combien de temps le layered fire AVANT le static
  - faux positifs (firing hors de toute fenetre d'injection)

Usage (campagne designee explicitement, format officiel -- voir validation_checks) :
    python evaluate_layered.py --input <campaign_AAAAMMJJ_HHMM>      # ou EVAL_INPUT
    python evaluate_layered.py --input <campagne> --candidate <modele.joblib>
    python evaluate_layered.py --input <campagne> --output rapport.json
  Codes de sortie : 0 resultats officiels ; 2 diagnostic seulement (pas de resultat
  officiel) ; 1 campagne non designee ; 3 --persist demande (toujours refuse, aucune
  ecriture dans ce lot).
"""

import argparse
import hashlib
import io
import json
import os
import statistics
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib
import psycopg2

import ml_model
import season
import validation_checks as vc
from features import compute_features, vectorize, direction_of, WINDOW_SIZE, MIN_WINDOW
from baseline_utils import compute_deviation, pg_dow, ENDPOINT_SLOS, DEFAULT_SLOS
from train_model import (load_baseline_lookup, baseline_for, baseline_lookup_from_rows,
                         BASELINE_METRICS)
from evaluation import load_injection_windows, InjectionWindow  # parsing du ground-truth

DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")

# Racine des ground-truth (surchargée en conteneur : GROUND_TRUTH_DIR=/data/results).
_GT_DIR = os.environ.get(
    "GROUND_TRUTH_DIR",
    os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "scenario-runner", "results"
    )),
)


def latest_campaign_input(gt_dir=None):
    """
    Jeu d'evaluation par defaut : EVAL_INPUT s'il est defini, sinon la campagne la
    plus recente <gt_dir>/campaign_*/ground_truth (les campagnes rangees dans
    archive/ sont ignorees). None si aucune campagne.
    A appeler au moment de l'usage : un process long (trainer) voit ainsi les
    campagnes creees apres son demarrage.
    """
    env = os.environ.get("EVAL_INPUT")
    if env:
        return env
    root = Path(gt_dir) if gt_dir else Path(_GT_DIR)
    campaigns = sorted(p for p in root.glob("campaign_*/ground_truth") if p.is_dir())
    return str(campaigns[-1]) if campaigns else None


DEFAULT_INPUT = latest_campaign_input()  # valeur a l'import, pour compatibilite

M_WINDOWS = 2                     # hysteresis (= PENDING_WINDOWS de detector.py)
FIRE_THRESHOLD = 0.6              # seuil de score combine (= detector.py, tune spec 9.2)
GRACE = timedelta(seconds=120)   # fenetre de grace apres cleared_at (= evaluation.py)
PAD = timedelta(minutes=10)      # marge autour de la campagne pour charger les features


def discover(input_path: Path):
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        return sorted(input_path.glob("*.json"))
    raise FileNotFoundError(f"{input_path} introuvable")


def load_windows(input_path: Path):
    windows = []
    for f in discover(input_path):
        try:
            windows.extend(load_injection_windows(f))
        except (KeyError, ValueError) as e:
            print(f"skip {f.name}: {e}", file=sys.stderr)
    return windows


def load_features_span(conn, since, until):
    """Rows endpoint_features valides sur [since, until], groupees par endpoint (asc)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT endpoint_id, time, rps, p50_ms, p95_ms, p99_ms, error_rate_5xx
        FROM endpoint_features
        WHERE time BETWEEN %s AND %s
          AND p99_ms IS NOT NULL AND p99_ms < 'NaN'::float8
        ORDER BY endpoint_id, time ASC
    """, (since, until))
    by_ep = {}
    for endpoint_id, t, rps, p50, p95, p99, err in cur.fetchall():
        by_ep.setdefault(endpoint_id, []).append({
            "time": t, "rps": rps, "p50_ms": p50, "p95_ms": p95,
            "p99_ms": p99, "error_rate_5xx": err,
        })
    cur.close()
    return by_ep


def score_point(window_rows, baselines, model_bundle):
    """
    Rejoue le scoring de detector.py pour un point. Retourne (static_fire, layered_fire).
    Parite stricte avec la chaine layer 0/1/2 + direction gating + combinaison.
    """
    point = window_rows[-1]
    p99 = point["p99_ms"]
    b99 = baselines.get("p99_ms")
    if b99:
        p10, _p50b, p90 = b99
    else:
        p10 = p90 = None
    deviation = compute_deviation(p99, p10, p90)
    direction = direction_of(p99, p10, p90)

    slos = ENDPOINT_SLOS.get(point.get("endpoint_id"), DEFAULT_SLOS)
    static_breach = p99 > slos["p99_ms"]
    static_norm = min(1.0, (p99 - slos["p99_ms"]) / (slos["p99_ms"] + 1e-6)) if static_breach else 0.0
    baseline_norm = min(1.0, deviation / 2.0)

    # Signal erreur (SLO 5xx) : commun aux deux detecteurs (le signal error_rate_5xx
    # de detector.py est static dans les deux cas). Indispensable pour les fautes
    # error_burst, qui montent le taux 5xx sans forcement toucher la latence.
    err = point.get("error_rate_5xx")
    error_breach = err is not None and err > slos["error_rate_5xx"]

    ml_norm = 0.0
    if model_bundle is not None and len(window_rows) >= MIN_WINDOW:
        feat = compute_features(window_rows, baselines)
        x = vectorize(feat)
        raw = ml_model.raw_anomaly(model_bundle["model"], x)[0]
        ml_norm = float(ml_model.calibrate(raw, model_bundle["meta"]["calibration"]))
    ml_gated = ml_norm if direction == "degradation" else 0.0

    combined = baseline_norm + (1.0 - baseline_norm) * ml_gated
    if static_breach:
        combined = max(combined, static_norm)

    static_fire = static_breach or error_breach
    layered_fire = static_fire or combined >= FIRE_THRESHOLD
    return static_fire, layered_fire


def replay(points_bool):
    """
    Applique l'hysteresis (M cycles consecutifs vrais) a une serie [(time, bool)].
    Retourne la liste des instants de FIRING (le M-ieme cycle consecutif = onset).
    Un nouvel episode ne redemarre qu'apres etre repasse sous le seuil.
    """
    onsets = []
    run = 0
    fired = False
    for t, b in points_bool:
        if b:
            run += 1
            if run >= M_WINDOWS and not fired:
                onsets.append(t)
                fired = True
        else:
            run = 0
            fired = False
    return onsets


def build_onsets(by_ep, exact, fallback, model_bundle):
    """Rejoue les 2 detecteurs par endpoint -> {endpoint: {'static':[...], 'layered':[...]}}."""
    out = {}
    for endpoint_id, rows in by_ep.items():
        for r in rows:
            r["endpoint_id"] = endpoint_id  # pour le SLO par endpoint
        static_series, layered_series = [], []
        # Watermark : on ignore le dernier point (fenetre incomplete).
        usable = rows[:-1] if len(rows) > 1 else rows
        for i in range(len(usable)):
            window = usable[max(0, i - WINDOW_SIZE + 1): i + 1]
            baselines = baseline_for(exact, fallback, endpoint_id, usable[i]["time"])
            s, l = score_point(window, baselines, model_bundle)
            static_series.append((usable[i]["time"], s))
            layered_series.append((usable[i]["time"], l))
        out[endpoint_id] = {
            "static": replay(static_series),
            "layered": replay(layered_series),
        }
    return out


def match_window(onsets_for_ep, window):
    """Premier onset dans [injected_at, cleared_at + grace] -> delai en s, sinon None."""
    lo, hi = window.injected_at, window.cleared_at + GRACE
    hits = [o for o in onsets_for_ep if lo <= o <= hi]
    if not hits:
        return None
    first = min(hits)
    return (first - window.injected_at).total_seconds()


@dataclass
class Stat:
    tp: int = 0
    fn: int = 0
    delays: list = field(default_factory=list)


def magnitude_of(window):
    """Magnitude derivee du nom de fichier de campagne : 'core' | 'stress' | 'all'."""
    src = (window.source_file or "").lower()
    if "stress" in src:
        return "stress"
    if "core" in src:
        return "core"
    return "all"


def group_stats(windows, onsets, keyfn):
    """Stats {cle: {'static':Stat,'layered':Stat}} pour une fonction de cle arbitraire."""
    groups = {}
    for w in windows:
        eps = onsets.get(w.target_endpoint, {"static": [], "layered": []})
        ds = match_window(eps["static"], w)
        dl = match_window(eps["layered"], w)
        g = groups.setdefault(keyfn(w), {"static": Stat(), "layered": Stat()})
        for name, d in (("static", ds), ("layered", dl)):
            if d is not None:
                g[name].tp += 1
                g[name].delays.append(d)
            else:
                g[name].fn += 1
    return groups


def evaluate(windows, onsets):
    """Calcule les stats par type de faute pour static et layered + FP + lead time."""
    per_type = {}          # fault_type -> {'static':Stat,'layered':Stat}
    lead_times = []        # secondes gagnees par le layered sur le static (fautes vues par les 2)
    matched_onsets = {"static": set(), "layered": set()}

    for w in windows:
        eps = onsets.get(w.target_endpoint, {"static": [], "layered": []})
        d_static = match_window(eps["static"], w)
        d_layered = match_window(eps["layered"], w)

        pt = per_type.setdefault(w.fault_type, {"static": Stat(), "layered": Stat()})
        for name, d in (("static", d_static), ("layered", d_layered)):
            if d is not None:
                pt[name].tp += 1
                pt[name].delays.append(d)
            else:
                pt[name].fn += 1

        if d_static is not None and d_layered is not None:
            lead_times.append(d_static - d_layered)  # >0 : layered plus tot

    # Faux positifs : un onset n'est FP que si AUCUNE injection n'est active a cet
    # instant (tous endpoints confondus). Cela exclut les cascades legitimes (ex.
    # payments qui fire pendant une faute orders) : ce ne sont pas des faux positifs,
    # c'est de la propagation reelle. Le spec (9.2) mesure les FP en charge sans faute.
    active = [(w.injected_at, w.cleared_at + GRACE) for w in windows]

    def during_fault(t):
        return any(a <= t <= b for a, b in active)

    fp = {"static": 0, "layered": 0}
    for endpoint_id, dets in onsets.items():
        for name in ("static", "layered"):
            for o in dets[name]:
                if not during_fault(o):
                    fp[name] += 1

    return per_type, fp, lead_times


def _rate(s):
    tot = s.tp + s.fn
    return s.tp / tot if tot else None


def _med(xs):
    return statistics.median(xs) if xs else None


def print_report(per_type, fp, lead_times, span_hours):
    print(f"\n{'fault_type':<24} {'n':>3}  {'DR static':>9} {'DR layered':>10}  "
          f"{'delay_s stat':>12} {'delay_s lay':>11}")
    print("-" * 76)
    tot = {"static": Stat(), "layered": Stat()}
    for ft in sorted(per_type):
        s, l = per_type[ft]["static"], per_type[ft]["layered"]
        n = s.tp + s.fn
        tot["static"].tp += s.tp; tot["static"].fn += s.fn; tot["static"].delays += s.delays
        tot["layered"].tp += l.tp; tot["layered"].fn += l.fn; tot["layered"].delays += l.delays
        print(f"{ft:<24} {n:>3}  {_fmt(_rate(s)):>9} {_fmt(_rate(l)):>10}  "
              f"{_fmtn(_med(s.delays)):>12} {_fmtn(_med(l.delays)):>11}")
    print("-" * 76)
    print(f"{'TOTAL':<24} {tot['static'].tp + tot['static'].fn:>3}  "
          f"{_fmt(_rate(tot['static'])):>9} {_fmt(_rate(tot['layered'])):>10}  "
          f"{_fmtn(_med(tot['static'].delays)):>12} {_fmtn(_med(tot['layered'].delays)):>11}")
    print()
    print(f"Faux positifs      static={fp['static']}  layered={fp['layered']}  "
          f"(sur ~{span_hours:.1f}h)")
    if span_hours > 0:
        print(f"FP / heure         static={fp['static']/span_hours:.2f}  "
              f"layered={fp['layered']/span_hours:.2f}")
    if lead_times:
        print(f"Lead time median   layered fire {_med(lead_times):.0f}s avant static "
              f"(n={len(lead_times)}, >0 = layered plus tot)")


def _fmt(v):
    return f"{v*100:.0f}%" if isinstance(v, float) else "-"


def _fmtn(v):
    return f"{v:.0f}" if isinstance(v, (int, float)) else "-"


def persist(conn, input_set, per_type, fp, span_hours, mag_groups=None):
    """Ecrit dans eval_runs : par fault_type (magnitude='all'), OVERALL, et par
    (fault_type, magnitude) pour la courbe de sensibilite."""
    run_id = str(uuid.uuid4())
    cur = conn.cursor()

    def ins(ft, mag, s, l, n, fps=None, fpl=None, fphs=None, fphl=None):
        cur.execute("""
            INSERT INTO eval_runs (run_id, input_set, fault_type, magnitude, n,
                                   dr_static, dr_layered, delay_static_s, delay_layered_s,
                                   fp_static, fp_layered, fp_per_hour_static, fp_per_hour_layered,
                                   span_hours)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (run_id, input_set, ft, mag, n, _rate(s), _rate(l),
              _med(s.delays), _med(l.delays), fps, fpl, fphs, fphl, span_hours))

    tot_tp = tot_fn = ov_static = ov_static_fn = 0
    all_ds, all_dl = [], []
    for ft in sorted(per_type):
        s, l = per_type[ft]["static"], per_type[ft]["layered"]
        ins(ft, "all", s, l, s.tp + s.fn)
        tot_tp += l.tp; tot_fn += l.fn
        ov_static += s.tp; ov_static_fn += s.fn
        all_ds += s.delays; all_dl += l.delays

    # Sensibilite : lignes par (fault_type, magnitude).
    for (ft, mag), g in sorted((mag_groups or {}).items()):
        if mag == "all":
            continue
        s, l = g["static"], g["layered"]
        ins(ft, mag, s, l, s.tp + s.fn)

    # Ligne OVERALL (faux positifs globaux).
    dr_static_all = ov_static / (ov_static + ov_static_fn) if (ov_static + ov_static_fn) else None
    dr_layered_all = tot_tp / (tot_tp + tot_fn) if (tot_tp + tot_fn) else None
    cur.execute("""
        INSERT INTO eval_runs (run_id, input_set, fault_type, magnitude, n, dr_static, dr_layered,
                               delay_static_s, delay_layered_s, fp_static, fp_layered,
                               fp_per_hour_static, fp_per_hour_layered, span_hours)
        VALUES (%s,%s,'OVERALL','all',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (run_id, input_set, ov_static + ov_static_fn, dr_static_all, dr_layered_all,
          _med(all_ds), _med(all_dl), fp["static"], fp["layered"],
          fp["static"] / span_hours if span_hours else None,
          fp["layered"] / span_hours if span_hours else None, span_hours))
    conn.commit()
    cur.close()
    print(f"\nResultats persistes dans eval_runs (run_id={run_id})")


def print_sensitivity(mag_groups):
    """Courbe de sensibilite : detection rate par (fault_type, magnitude)."""
    print(f"\n{'fault_type':<20} {'magnitude':>9} {'n':>3}  {'DR static':>9} {'DR layered':>10}")
    print("-" * 58)
    for (ft, mag) in sorted(mag_groups):
        s, l = mag_groups[(ft, mag)]["static"], mag_groups[(ft, mag)]["layered"]
        print(f"{ft:<20} {mag:>9} {s.tp + s.fn:>3}  {_fmt(_rate(s)):>9} {_fmt(_rate(l)):>10}")


# ---------------------------------------------------------------------------
# Evaluation officielle / diagnostique d'une campagne (lot 2b-2)
# ---------------------------------------------------------------------------
# Principes :
#  - campagne designee explicitement, validee par validation_checks (format officiel) ;
#  - baseline reconstruite UNIQUEMENT depuis l'instantane de la campagne (jamais
#    endpoint_baseline en base) ;
#  - couverture alignee sur le rejeu : derniere ligne de chaque endpoint ignoree
#    (build_onsets), MIN_WINDOW lignes de contexte, M_WINDOWS lignes pendant la panne ;
#  - resultats OFFICIELS seulement si TOUTES les fenetres sont evaluables et le
#    denominateur des FP est non nul ; sinon resultats DIAGNOSTIQUES uniquement ;
#  - C (modele en service) : aucun chiffre tant que son identite n'est pas verifiable.

FEATURES_PY = Path(__file__).resolve().parent / "features.py"
GRACE_S = int(GRACE.total_seconds())
# Mesures lues par score_point pour S (static_breach, error_breach) et B (deviation p99).
SB_METRICS = ("p99_ms", "error_rate_5xx")
# Baselines utilisees : B -> p99 (compute_deviation, direction_of) ; N/C -> BASELINE_METRICS.
B_BASELINE_METRICS = ("p99_ms",)
C_RESULT = {
    "statut": "non_verifiable",
    "raison": "identite du modele reellement charge par le service non disponible "
              "(prevue au lot 2b-5) : aucun chiffre n'est fourni pour C",
}
PERSIST_REFUSAL = (
    "--persist refuse, aucune ecriture : C (modele en service) n'est pas identifiable et "
    "eval_runs ne peut pas representer fidelement S, B, C et N sans changement de schema")


def _window_obj(w):
    """Fenetre validation_checks -> InjectionWindow (reutilise match_window / magnitude_of)."""
    return InjectionWindow(scenario_id=w.get("scenario_id"), fault_type=w["fault_type"],
                           target_endpoint=w["target_endpoint"], injected_at=w["injected"],
                           cleared_at=w["cleared"], magnitude=w.get("magnitude", {}),
                           source_file=w["source"])


def _fault_ranges(windows):
    return [(w["injected"], w["cleared"] + GRACE) for w in windows]


def observable_hours_outside_faults(by_ep, gt_windows, scrape_s):
    """
    Denominateur des faux positifs : heures observables HORS PANNE.
      - on ne garde que les lignes reellement rejouees (derniere ligne de chaque
        endpoint exclue, comme build_onsets) ;
      - on retire celles qui tombent dans une fenetre de panne [injected, cleared + 120 s]
        (TOUTES les pannes de la ground truth, evaluables ou non) ;
      - on compte les intervalles de collecte distincts (scrape_s) contenant au moins une
        de ces lignes, tous endpoints confondus.
    Renvoie (heures, intervalles). 0 -> taux de FP impossible a calculer.
    """
    ranges = _fault_ranges(gt_windows)
    buckets = set()
    for rows in by_ep.values():
        usable = rows[:-1] if len(rows) > 1 else rows
        for r in usable:
            t = r["time"]
            if any(a <= t <= b for a, b in ranges):
                continue
            buckets.add(int(t.timestamp() // scrape_s))
    return len(buckets) * scrape_s / 3600.0, len(buckets)


def configuration_stats(onsets, series, compared, gt_windows, hours):
    """
    Indicateurs d'une configuration (series 'static' = S, 'layered' = B ou N) sur la
    liste `compared` de fenetres (identique pour toutes les configurations).
      recall          = detectees / fenetres comparees
      delai_median_s  = mediane (premiere alerte - injected_at), fenetres detectees
      fp              = alertes hors de TOUTE fenetre de panne (+120 s)
      fp_par_heure    = fp / heures observables hors panne (None si heures = 0)
      alertes_pendant_panne_non_comparee : alertes pendant une panne reelle dont la
                        fenetre n'est pas comparee -- ni vrai ni faux positif
    """
    compared_ids = {id(w) for w in compared}
    per_type, per_mag, delays = {}, {}, []
    tp = 0
    for w in compared:
        d = match_window(onsets.get(w["target_endpoint"], {}).get(series, []), _window_obj(w))
        hit = d is not None
        tp += hit
        if hit:
            delays.append(d)
        for agg, key in ((per_type, w["fault_type"]),
                         (per_mag, f"{w['fault_type']}/{magnitude_of(_window_obj(w))}")):
            s = agg.setdefault(key, {"detectees": 0, "total": 0, "delais": []})
            s["total"] += 1
            if hit:
                s["detectees"] += 1
                s["delais"].append(d)
    all_ranges = _fault_ranges(gt_windows)
    other_ranges = [(w["injected"], w["cleared"] + GRACE) for w in gt_windows if id(w) not in compared_ids]
    fp = ambiguous = 0
    for dets in onsets.values():
        for o in dets.get(series, []):
            if any(a <= o <= b for a, b in other_ranges):
                ambiguous += 1
            elif not any(a <= o <= b for a, b in all_ranges):
                fp += 1

    def fmt(s):
        return {"detectees": s["detectees"], "total": s["total"],
                "recall": s["detectees"] / s["total"], "delai_median_s": _med(s["delais"])}

    return {
        "fenetres": len(compared), "detectees": tp,
        "recall": tp / len(compared) if compared else None,
        "delai_median_s": _med(delays),
        "fp": fp, "fp_par_heure": (fp / hours) if hours else None,
        "alertes_pendant_panne_non_comparee": ambiguous,
        "par_type": {k: fmt(v) for k, v in sorted(per_type.items())},
        "par_type_et_niveau": {k: fmt(v) for k, v in sorted(per_mag.items())},
    }


def load_candidate(path):
    """
    Candidat N : (bundle | None, sha256 | None, raisons). Les octets sont lus UNE fois ;
    l'empreinte est celle de ces octets, et le modele est charge depuis ces memes octets.
    Le fichier est un artefact joblib fourni explicitement (a ne charger que de confiance).
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        return None, None, [f"candidat illisible ({e})"]
    sha = hashlib.sha256(raw).hexdigest()
    try:
        bundle = joblib.load(io.BytesIO(raw))
    except Exception as e:
        return None, sha, [f"candidat non chargeable ({e})"]
    problems = ml_model.check_bundle_compat(bundle)
    return (bundle if not problems else None), sha, problems


def _window_diagnostics(windows, coverage, metric_cov, completeness, fallback, need_ml):
    """Evaluabilite de chaque fenetre pour S/B et pour N, avec les raisons precises."""
    out = []
    for w, cov, mc, comp, fb in zip(windows, coverage, metric_cov, completeness, fallback):
        why_sb = list(cov["reasons"])
        for m in SB_METRICS:
            n = mc["metrics"][m]["pendant_valides"]
            if n < M_WINDOWS:
                why_sb.append(f"{m} : {n} valeur(s) valide(s) pendant la panne (< {M_WINDOWS})")
        if cov["during_points"] < cov["expected_points"] and (
                cov["during_points"] < M_WINDOWS or cov["context_points"] < MIN_WINDOW):
            why_sb.append(f"trous dans les mesures : {cov['during_points']}/{cov['expected_points']} "
                          f"intervalles presents pendant la panne (plus grand ecart "
                          f"{cov['max_gap_s']} s)")

        def baseline_reasons(metrics):
            r = []
            for m in metrics:
                st = comp["metrics"][m]
                if st in ("absent", "degenere"):
                    r.append(f"baseline {m} {st} ({comp['slots'][m]})")
                elif fb["metrics"][m]["creneaux_secours"] and not fb["metrics"][m]["valide"]:
                    r.append(f"baseline {m} : bande de secours inutilisable")
            return r

        why_sb += baseline_reasons(B_BASELINE_METRICS)
        why_ml = list(why_sb)
        if need_ml:
            for m in BASELINE_METRICS:
                if m in SB_METRICS:
                    continue
                n = mc["metrics"][m]["pendant_valides"]
                if n < M_WINDOWS:
                    why_ml.append(f"{m} : {n} valeur(s) valide(s) pendant la panne (< {M_WINDOWS})")
            why_ml += baseline_reasons([m for m in BASELINE_METRICS if m not in B_BASELINE_METRICS])
        out.append({
            "source": w["source"], "fault_type": w["fault_type"], "target_endpoint": w["target_endpoint"],
            "evaluable_SB": not why_sb, "raisons_SB": why_sb,
            "evaluable_N": (not why_ml) if need_ml else None, "raisons_N": why_ml if need_ml else None,
            "contexte": cov["context_points"], "pendant": cov["during_points"],
            "attendus": cov["expected_points"], "completude": cov["completeness"],
            "plus_grand_ecart_s": cov["max_gap_s"],
            # Case saisonniere (dow, heure) au debut de la panne : en mode compresse,
            # heure simulee de load.js (creux / pic de charge).
            "case_saisonniere": list(season.slot(w["injected"])),
            "creneaux_secours": {m: fb["metrics"][m]["creneaux_secours"] for m in BASELINE_METRICS},
            "baseline": comp["metrics"],
        })
    return out


def evaluate_campaign(conn, campaign_dir, *, candidate_path=None):
    """
    Evalue une campagne au format officiel. N'interroge que endpoint_features (lecture) ;
    la baseline vient exclusivement de l'instantane. Renvoie un dict :
      statut   : officiel | couverture_insuffisante | fp_non_calculable |
                 campagne_invalide | aucune_mesure
      raisons  : pourquoi il n'y a pas de resultat officiel
      officiel : {S, B, C, N, identite} si statut == officiel, sinon None
      diagnostic : fenetres (evaluables ou non, raisons), couverture, baseline,
                   resultats PARTIELS sur les fenetres evaluables -- jamais officiels
    """
    res = {"campagne": str(campaign_dir), "statut": None, "raisons": [],
           "officiel": None, "diagnostic": {}, "mode_saisonnier": season.describe()}
    diag = res["diagnostic"]
    chk = vc.check_campaign(campaign_dir, require_completed=True, features_py=FEATURES_PY)
    manifest = chk["manifest"] or {}
    scrape_s = manifest.get("scrape_interval")
    if chk["ok"] and not (isinstance(scrape_s, int) and scrape_s > 0):
        chk["reasons"].append("manifest.json : scrape_interval entier positif requis")
    if chk["reasons"]:
        res.update(statut="campagne_invalide", raisons=list(chk["reasons"]))
        return res
    windows = chk["windows"]

    rows, snap_reasons = vc.check_baseline_snapshot(campaign_dir, manifest["baseline_fingerprint"])
    if snap_reasons:   # deja controle par check_campaign ; garde-fou
        res.update(statut="campagne_invalide", raisons=snap_reasons)
        return res
    exact, fallback = baseline_lookup_from_rows(rows)
    context_s = WINDOW_SIZE * scrape_s
    completeness, _ = vc.baseline_completeness(rows, windows, list(BASELINE_METRICS),
                                               context_s=context_s, grace_s=GRACE_S)
    fb_report, _ = vc.fallback_band_report(rows, windows, list(BASELINE_METRICS),
                                           context_s=context_s, grace_s=GRACE_S)

    since = min(w["injected"] for w in windows) - PAD
    until = max(w["cleared"] for w in windows) + PAD
    by_ep = load_features_span(conn, since, until)
    diag["periode_mesures"] = [since.isoformat(), until.isoformat()]
    diag["lignes_par_endpoint"] = {ep: len(v) for ep, v in sorted(by_ep.items())}
    if not any(by_ep.values()):
        res.update(statut="aucune_mesure",
                   raisons=["aucune mesure valide (endpoint_features) sur la periode de la campagne"])
        return res

    coverage = vc.window_coverage(
        windows, {ep: [r["time"] for r in v] for ep, v in by_ep.items()},
        min_context=MIN_WINDOW, min_points=M_WINDOWS, context_s=context_s, grace_s=GRACE_S,
        scrape_s=scrape_s, exclude_last_point=True)
    metric_cov = vc.metric_coverage(windows, by_ep, list(BASELINE_METRICS) + ["error_rate_5xx"],
                                    context_s=context_s, grace_s=GRACE_S, exclude_last_point=True)

    candidate, cand_sha, cand_reasons = (None, None, [])
    if candidate_path is not None:
        candidate, cand_sha, cand_reasons = load_candidate(candidate_path)
    per_window = _window_diagnostics(windows, coverage, metric_cov, completeness, fb_report,
                                     need_ml=candidate is not None)
    diag["fenetres"] = per_window
    diag["couverture_par_endpoint"] = vc.coverage_summary(
        [dict(c, evaluable=p["evaluable_SB"]) for c, p in zip(coverage, per_window)])["by_endpoint"]
    hours, intervals = observable_hours_outside_faults(by_ep, windows, scrape_s)
    diag["heures_observables_hors_panne"] = hours
    diag["intervalles_observables_hors_panne"] = intervals

    eval_sb = [w for w, p in zip(windows, per_window) if p["evaluable_SB"]]
    eval_n = [w for w, p in zip(windows, per_window) if p["evaluable_N"]] if candidate is not None else []
    compared = eval_n if candidate is not None else eval_sb

    def run(compared_windows):
        base = build_onsets(by_ep, exact, fallback, None)
        out = {
            "identite": {
                "empreinte_fenetres_campagne": vc.window_list_fingerprint(windows),
                "empreinte_fenetres_comparees": vc.window_list_fingerprint(compared_windows),
                "empreinte_baseline": manifest["baseline_fingerprint"],
                "periode_mesures": diag["periode_mesures"],
                "heures_observables_hors_panne": hours,
            },
            "S": configuration_stats(base, "static", compared_windows, windows, hours),
            "B": configuration_stats(base, "layered", compared_windows, windows, hours),
            "C": dict(C_RESULT),
        }
        if candidate_path is None:
            out["N"] = {"statut": "non_fourni"}
        elif candidate is None:
            out["N"] = {"statut": "incompatible", "sha256": cand_sha, "raisons": cand_reasons}
        else:
            n_onsets = build_onsets(by_ep, exact, fallback, candidate)
            out["N"] = dict(configuration_stats(n_onsets, "layered", compared_windows, windows, hours),
                            statut="evalue", sha256=cand_sha)
        return out

    complete = len(compared) == len(windows)
    if complete and hours > 0:
        res.update(statut="officiel", officiel=run(compared))
        return res
    if not complete:
        res["statut"] = "couverture_insuffisante"
        res["raisons"].append(f"{len(windows) - len(compared)} fenetre(s) sur {len(windows)} non "
                              f"evaluable(s) : aucun resultat officiel (voir diagnostic)")
    else:
        res["statut"] = "fp_non_calculable"
        res["raisons"].append("aucune heure observable hors panne : taux de faux positifs "
                              "impossible a calculer")
    if compared:
        diag["resultats_partiels"] = dict(run(compared), avertissement=(
            "RESULTATS DIAGNOSTIQUES PARTIELS -- calcules sur les seules fenetres evaluables ; "
            "ce n'est PAS une evaluation officielle complete"))
    return res


def _print_config(name, c):
    if c.get("statut") in ("non_verifiable", "non_fourni", "incompatible"):
        detail = c.get("raison") or "; ".join(c.get("raisons", [])) or ""
        print(f"  {name:<2} : {c['statut']}  {detail}")
        return
    fph = "impossible" if c["fp_par_heure"] is None else f"{c['fp_par_heure']:.2f}"
    print(f"  {name:<2} : recall {_fmt(c['recall'])} ({c['detectees']}/{c['fenetres']})  "
          f"delai median {_fmt(c['delai_median_s'])} s  FP {c['fp']} ({fph} /h observable)  "
          f"alertes pendant pannes non comparees {c['alertes_pendant_panne_non_comparee']}")


def print_campaign_result(res):
    print(f"Campagne : {res['campagne']}")
    print(f"Mode     : {res.get('mode_saisonnier')}")
    print(f"Statut   : {res['statut']}")
    if res["officiel"]:
        o = res["officiel"]
        print("\n=== RESULTATS OFFICIELS ===")
        print(f"  fenetres comparees : {o['S']['fenetres']}  |  heures observables hors panne : "
              f"{o['identite']['heures_observables_hors_panne']:.2f}")
        for k in ("S", "B", "C", "N"):
            _print_config(k, o[k])
    else:
        print("\n=== AUCUN RESULTAT OFFICIEL ===")
        for r in res["raisons"]:
            print(f"  - {r}")
    d = res["diagnostic"]
    if d.get("fenetres"):
        print("\n=== DIAGNOSTIC (non officiel) ===")
        for f in d["fenetres"]:
            etat = "evaluable" if f["evaluable_SB"] else "NON evaluable"
            print(f"  {f['source']:<40} {f['fault_type']:<16} {etat:<14} "
                  f"pendant {f['pendant']}/{f['attendus']}  ecart max {f['plus_grand_ecart_s']} s  "
                  f"secours {sum(f['creneaux_secours'].values())}  case {f.get('case_saisonniere')}")
            for r in f["raisons_SB"]:
                print(f"      - {r}")
        print(f"  heures observables hors panne : {d.get('heures_observables_hors_panne', 0):.2f}")
    if d.get("resultats_partiels"):
        p = d["resultats_partiels"]
        print(f"\n  {p['avertissement']}")
        for k in ("S", "B", "C", "N"):
            _print_config(k, p[k])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=None,
                        help="Dossier de campagne (campaign_AAAAMMJJ_HHMM) ; defaut : EVAL_INPUT. "
                             "Aucune selection automatique.")
    parser.add_argument("--candidate", type=Path, default=None,
                        help="Artefact joblib du modele candidat N (optionnel)")
    parser.add_argument("--output", type=Path, help="Rapport JSON optionnel (officiel + diagnostic)")
    parser.add_argument("--persist", action="store_true",
                        help="Refuse dans ce lot (aucune ecriture dans eval_runs)")
    args = parser.parse_args(argv)

    campaign = args.input or (Path(os.environ["EVAL_INPUT"]) if os.environ.get("EVAL_INPUT") else None)
    if campaign is None:
        print("campagne explicite requise : --input <dossier de campagne> ou EVAL_INPUT "
              "(aucune selection automatique)", file=sys.stderr)
        return 1

    conn = psycopg2.connect(DB_URL)
    try:
        conn.set_session(readonly=True)
        res = evaluate_campaign(conn, campaign, candidate_path=args.candidate)
    finally:
        conn.close()
    print_campaign_result(res)

    if args.output:
        args.output.write_text(json.dumps(res, indent=2, default=str))
        print(f"\nrapport ecrit dans {args.output}")
    if args.persist:
        print(f"\n{PERSIST_REFUSAL}", file=sys.stderr)
        return 3
    return 0 if res["statut"] == "officiel" else 2


if __name__ == "__main__":
    raise SystemExit(main())
