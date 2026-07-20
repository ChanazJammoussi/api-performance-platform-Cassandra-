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

Usage :
    python evaluate_layered.py                       # campagne par defaut
    python evaluate_layered.py --input <dir|fichier>
    python evaluate_layered.py --output rapport.json
"""

import argparse
import json
import os
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2

import ml_model
from features import compute_features, vectorize, direction_of, WINDOW_SIZE, MIN_WINDOW
from baseline_utils import compute_deviation, pg_dow, ENDPOINT_SLOS, DEFAULT_SLOS
from train_model import load_baseline_lookup, baseline_for
from evaluation import load_injection_windows  # reutilise le parsing du ground-truth

DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")

# Racine des ground-truth (surchargée en conteneur : GROUND_TRUTH_DIR=/data/results).
_GT_DIR = os.environ.get(
    "GROUND_TRUTH_DIR",
    os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "scenario-runner", "results"
    )),
)
# Jeu d'evaluation par defaut : la campagne structuree (tous types x magnitudes).
DEFAULT_INPUT = os.environ.get(
    "EVAL_INPUT", os.path.join(_GT_DIR, "campaign_20260706_1109", "ground_truth")
)

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(DEFAULT_INPUT))
    parser.add_argument("--output", type=Path, help="Rapport JSON optionnel")
    args = parser.parse_args()

    windows = load_windows(args.input)
    if not windows:
        print(f"aucune fenetre d'injection dans {args.input}", file=sys.stderr)
        return 1
    since = min(w.injected_at for w in windows) - PAD
    until = max(w.cleared_at for w in windows) + PAD
    span_hours = (until - since).total_seconds() / 3600
    print(f"Fenetres d'injection : {len(windows)}  |  span {since:%Y-%m-%d %H:%M} -> {until:%H:%M} "
          f"({span_hours:.1f}h)")

    conn = psycopg2.connect(DB_URL)
    by_ep = load_features_span(conn, since, until)
    exact, fallback = load_baseline_lookup(conn)
    conn.close()
    model_bundle = ml_model.load_latest()
    print(f"Features : {sum(len(v) for v in by_ep.values())} points / {len(by_ep)} endpoints  |  "
          f"modele ML : {'charge' if model_bundle else 'ABSENT (layered=baseline seul)'}")

    onsets = build_onsets(by_ep, exact, fallback, model_bundle)
    per_type, fp, lead_times = evaluate(windows, onsets)
    print_report(per_type, fp, lead_times, span_hours)

    if args.output:
        payload = {
            "input": str(args.input),
            "span_hours": span_hours,
            "false_positives": fp,
            "lead_time_median_s": _med(lead_times),
            "per_fault_type": {
                ft: {
                    "n": d["static"].tp + d["static"].fn,
                    "detection_rate_static": _rate(d["static"]),
                    "detection_rate_layered": _rate(d["layered"]),
                    "median_delay_static_s": _med(d["static"].delays),
                    "median_delay_layered_s": _med(d["layered"].delays),
                }
                for ft, d in per_type.items()
            },
        }
        args.output.write_text(json.dumps(payload, indent=2))
        print(f"\nrapport ecrit dans {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
