#!/usr/bin/env python3
"""
baseline_readiness.py -- verdict objectif "baseline PRETE / PAS PRETE" apres les
journees simulees de load.js (mode saisonnier compresse, season.py).

LECTURE SEULE : lit le journal de scripts/run_compressed_days.sh, k6/load.js,
endpoint_features, endpoint_baseline (transaction en lecture seule) et les
ground truth. N'ecrit rien, ne lance rien.

Criteres (tous obligatoires ; chaque echec est une raison explicite) :
  1. journees k6 : au moins --days journees consecutives, toutes terminees avec le
     code 0, chacune demarree au plus --max-start-offset s apres son heure paire UTC ;
  2. continuite des mesures : chaque endpoint attendu mesure toute la periode,
     sans trou > --max-gap s ;
  3. aucune panne injectee (ground truth) pendant la periode ;
  4. cases de baseline : toutes les cases (endpoint x metrique x heure simulee)
     presentes, du bon mode (pas de melange), avec >= --min-samples mesures ;
     bandes valides pour les latences et le trafic (bandes degenerees des taux
     d'erreur signalees sans bloquer, decision du lot 2b-2) ;
  5. forme du trafic : correlation >= --min-corr, pour chaque endpoint, entre le
     rps median de chaque heure simulee et le profil attendu de load.js.

Lancement (dans un conteneur du projet compresse, DATABASE_URL en lecture) :
    python baseline_readiness.py --journal /chemin/journal_journees.tsv
Code de sortie : 0 = PRETE, 1 = PAS PRETE, 2 = erreur de lecture.
"""

import argparse
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from statistics import median

import season

DB_URL = os.environ.get("DATABASE_URL", "postgresql://cassandra:cassandra@localhost:5434/cassandra")

# Seuils par defaut (exposes en options ; decrits dans docs/mode_compresse.md).
DEFAULT_DAYS = 7
DEFAULT_MIN_SAMPLES = 25        # ~33 attendues apres 7 journees (scraper ~62,6 s)
DEFAULT_MAX_START_OFFSET_S = 60
DEFAULT_MAX_GAP_S = 180
DEFAULT_MIN_CORR = 0.9
RATE_WINDOW_S = 300             # fenetre rate[5m] du scraper (scraper.fetch_features)
WARMUP_S = RATE_WINDOW_S        # premiere mesure complete apres 5 min de trafic
# Metriques dont la bande doit etre valide (les taux d'erreur valent souvent 0 :
# bande degeneree attendue, signalee seulement).
BAND_REQUIRED = ("p50_ms", "p95_ms", "p99_ms", "rps")
BAND_EPS = 1e-6

_STAGE_RE = re.compile(r"\{\s*duration\s*:\s*'(\d+)([smh])'\s*,\s*target\s*:\s*(\d+)\s*\}")
_START_RE = re.compile(r"startVUs\s*:\s*(\d+)")


# ---------------------------------------------------------------------------
# Profil attendu de load.js
# ---------------------------------------------------------------------------

def parse_k6_stages(text):
    """(start_vus, [(duree_s, cible)]) lus dans le source de load.js. ValueError si absent."""
    m = _START_RE.search(text)
    stages = [(int(n) * {"s": 1, "m": 60, "h": 3600}[u], int(t)) for n, u, t in _STAGE_RE.findall(text)]
    if not m or not stages:
        raise ValueError("load.js : startVUs ou stages introuvables")
    return int(m.group(1)), stages


def vus_at(t_s, start_vus, stages):
    """Utilisateurs simules a t_s secondes du debut de cycle (rampes lineaires k6)."""
    total = sum(d for d, _ in stages)
    t = t_s % total
    prev = start_vus
    for d, target in stages:
        if t <= d:
            return prev + (target - prev) * (t / d)
        t -= d
        prev = target
    return prev


def expected_slot_profile(start_vus, stages, step_s=10):
    """
    Profil attendu par heure simulee (24 valeurs) : moyenne, sur les instants de
    l'heure simulee, de la moyenne des utilisateurs sur la fenetre rate[5m] qui
    precede chaque instant (meme lissage que le scraper).
    """
    slot_s = season.SLOT_MINUTES * 60
    out = []
    for h in range(season.SLOTS_PER_DAY):
        vals = []
        for t in range(h * slot_s, (h + 1) * slot_s, step_s):
            win = [vus_at(t - u, start_vus, stages) for u in range(0, RATE_WINDOW_S, step_s)]
            vals.append(sum(win) / len(win))
        out.append(sum(vals) / len(vals))
    return out


def pearson(xs, ys):
    """Correlation de Pearson ; None si une serie est constante ou trop courte."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


# ---------------------------------------------------------------------------
# Journal des journees (scripts/run_compressed_days.sh)
# ---------------------------------------------------------------------------

def parse_journal(text):
    """
    Lignes TSV "cycle  heure_paire  debut  fin  code" (dates ISO UTC) ; lignes
    vides et commentaires (#) ignores. ValueError sur une ligne mal formee.
    """
    cycles = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.rstrip("\r").split("\t")
        if len(parts) != 5:
            raise ValueError(f"journal ligne {i} : 5 colonnes attendues, {len(parts)} recues")
        try:
            c = {"cycle": int(parts[0]),
                 "boundary": datetime.fromisoformat(parts[1].replace("Z", "+00:00")),
                 "start": datetime.fromisoformat(parts[2].replace("Z", "+00:00")),
                 "end": datetime.fromisoformat(parts[3].replace("Z", "+00:00")),
                 "exit_code": int(parts[4])}
        except ValueError as e:
            raise ValueError(f"journal ligne {i} : {e}")
        for k in ("boundary", "start", "end"):
            if c[k].tzinfo is None:
                raise ValueError(f"journal ligne {i} : {k} sans fuseau horaire")
        cycles.append(c)
    return cycles


def check_cycles(cycles, *, days, max_start_offset_s):
    """Raisons d'echec des journees k6 (critere 1)."""
    reasons = []
    if len(cycles) < days:
        reasons.append(f"journees k6 : {len(cycles)} terminee(s) dans le journal (< {days})")
    cycle_s = season.CYCLE_MINUTES * 60
    for prev, c in zip(cycles, cycles[1:]):
        if (c["boundary"] - prev["boundary"]).total_seconds() != cycle_s:
            reasons.append(f"journees k6 : cycle {c['cycle']} non consecutif au cycle {prev['cycle']}")
    for c in cycles:
        if c["exit_code"] != 0:
            reasons.append(f"journee k6 {c['cycle']} : code de sortie {c['exit_code']} (0 attendu)")
        if math.floor(c["boundary"].timestamp()) % cycle_s:
            reasons.append(f"journee k6 {c['cycle']} : {c['boundary'].isoformat()} n'est pas une heure paire UTC")
        offset = (c["start"] - c["boundary"]).total_seconds()
        if not 0 <= offset <= max_start_offset_s:
            reasons.append(f"journee k6 {c['cycle']} : demarrage a {offset:.0f} s de son heure paire "
                           f"(0..{max_start_offset_s} s attendus)")
    return reasons


# ---------------------------------------------------------------------------
# Mesures, pannes, baseline
# ---------------------------------------------------------------------------

def check_continuity(times_by_endpoint, endpoints, period_start, period_end, *, max_gap_s):
    """Raisons d'echec de la continuite des mesures (critere 2)."""
    reasons = []
    lo, hi = period_start + timedelta(seconds=WARMUP_S), period_end
    for ep in endpoints:
        ts = sorted(t for t in times_by_endpoint.get(ep, []) if lo <= t <= hi)
        if not ts:
            reasons.append(f"mesures {ep} : aucune mesure sur la periode")
            continue
        edges = [(ts[0] - lo).total_seconds(), (hi - ts[-1]).total_seconds()]
        gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])] + edges
        worst = max(gaps)
        if worst > max_gap_s:
            reasons.append(f"mesures {ep} : trou de {worst:.0f} s (> {max_gap_s} s)")
    return reasons


def check_no_injection(injections, period_start, period_end):
    """Raisons : pannes injectees qui recoupent la periode de collecte (critere 3)."""
    hits = [(a, b) for a, b in injections if a <= period_end and b >= period_start]
    if not hits:
        return []
    return [f"ground truth : {len(hits)} panne(s) injectee(s) pendant la periode de collecte "
            f"(premiere : {min(a for a, _ in hits).isoformat()})"]


def _band_ok(r):
    p10, p90 = r.get("p10"), r.get("p90")
    ok = all(isinstance(v, (int, float)) and math.isfinite(v) for v in (p10, p90))
    return ok and (p90 - p10) >= BAND_EPS


def expected_slots(mode=None):
    """Toutes les cases (dow, heure) attendues pour le mode."""
    if season._mode(mode) == season.COMPRESSE:
        return [(0, h) for h in range(season.SLOTS_PER_DAY)]
    return [(d, h) for d in range(7) for h in range(24)]


def check_baseline(rows, endpoints, metrics, *, min_samples, mode=None):
    """(raisons, infos) sur les cases de baseline (critere 4)."""
    reasons = list(season.rows_mode_reasons(rows, mode))
    infos = []
    by_key = {(r["endpoint_id"], r["metric"], int(r["dow"]), int(r["hour_bucket"])): r for r in rows}
    slots = expected_slots(mode)
    missing, few, bad_band, degenerate = [], [], [], []
    for ep in endpoints:
        for m in metrics:
            for d, h in slots:
                r = by_key.get((ep, m, d, h))
                if r is None:
                    missing.append((ep, m, d, h))
                    continue
                if (r.get("sample_count") or 0) < min_samples:
                    few.append((ep, m, d, h, r.get("sample_count")))
                if not _band_ok(r):
                    (bad_band if m in BAND_REQUIRED else degenerate).append((ep, m, d, h))
    expected = len(endpoints) * len(metrics) * len(slots)
    if missing:
        reasons.append(f"baseline : {expected - len(missing)}/{expected} cases presentes "
                       f"(exemple manquant : {missing[0]})")
    if few:
        worst = min(few, key=lambda x: x[4] or 0)
        reasons.append(f"baseline : {len(few)} case(s) avec moins de {min_samples} mesures "
                       f"(minimum {worst[4]} pour {worst[:4]})")
    if bad_band:
        reasons.append(f"baseline : {len(bad_band)} bande(s) invalide(s) pour {BAND_REQUIRED} "
                       f"(exemple : {bad_band[0]})")
    if degenerate:
        infos.append(f"baseline : {len(degenerate)} bande(s) degeneree(s) de taux d'erreur "
                     f"(attendu si aucune erreur ; signale sans bloquer)")
    return reasons, infos


def check_shape(rows, endpoints, profile, *, min_corr):
    """(raisons, correlations) : forme du trafic par endpoint (critere 5, mode compresse)."""
    reasons, corrs = [], {}
    by_key = {(r["endpoint_id"], int(r["hour_bucket"])): r.get("p50")
              for r in rows if r["metric"] == "rps" and int(r["dow"]) == 0}
    for ep in endpoints:
        xs = [by_key.get((ep, h)) for h in range(season.SLOTS_PER_DAY)]
        if any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in xs):
            reasons.append(f"forme du trafic {ep} : rps median absent pour certaines heures simulees")
            corrs[ep] = None
            continue
        c = pearson(xs, profile)
        corrs[ep] = None if c is None else round(c, 3)
        if c is None or c < min_corr:
            reasons.append(f"forme du trafic {ep} : correlation {corrs[ep]} avec load.js (< {min_corr})")
    return reasons, corrs


def assess(*, cycles, times_by_endpoint, injections, rows, endpoints, metrics, profile,
           days=DEFAULT_DAYS, min_samples=DEFAULT_MIN_SAMPLES,
           max_start_offset_s=DEFAULT_MAX_START_OFFSET_S, max_gap_s=DEFAULT_MAX_GAP_S,
           min_corr=DEFAULT_MIN_CORR, mode=None):
    """Verdict complet (fonction pure). Renvoie {pret, raisons, infos, details}."""
    reasons, infos, details = [], [], {"mode": season.describe(mode)}
    if season._mode(mode) != season.COMPRESSE:
        reasons.append(f"mode saisonnier {season._mode(mode)!r} : ce verdict concerne le mode compresse")
    reasons += check_cycles(cycles, days=days, max_start_offset_s=max_start_offset_s)
    if cycles:
        period_start, period_end = cycles[0]["boundary"], cycles[-1]["end"]
        details["periode"] = [period_start.isoformat(), period_end.isoformat()]
        reasons += check_continuity(times_by_endpoint, endpoints, period_start, period_end,
                                    max_gap_s=max_gap_s)
        reasons += check_no_injection(injections, period_start, period_end)
    r, i = check_baseline(rows, endpoints, metrics, min_samples=min_samples, mode=mode)
    reasons += r
    infos += i
    samples = [x.get("sample_count") for x in rows if x.get("sample_count") is not None]
    details["mesures_par_case"] = {"min": min(samples) if samples else None,
                                   "mediane": median(samples) if samples else None}
    if season._mode(mode) == season.COMPRESSE:
        r, corrs = check_shape(rows, endpoints, profile, min_corr=min_corr)
        reasons += r
        details["correlations"] = corrs
    details["journees"] = len(cycles)
    return {"pret": not reasons, "raisons": reasons, "infos": infos, "details": details}


# ---------------------------------------------------------------------------
# Lecture (CLI)
# ---------------------------------------------------------------------------

def _read_db(period_start, period_end):
    import psycopg2
    conn = psycopg2.connect(DB_URL)
    try:
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute("""
            SELECT endpoint_id, time FROM endpoint_features
            WHERE time BETWEEN %s AND %s AND p99_ms IS NOT NULL AND p99_ms < 'NaN'::float8
        """, (period_start, period_end))
        times = {}
        for ep, t in cur.fetchall():
            times.setdefault(ep, []).append(t)
        cur.execute("""
            SELECT endpoint_id, metric, dow, hour_bucket, p10, p50, p90, sample_count
            FROM endpoint_baseline
        """)
        cols = ("endpoint_id", "metric", "dow", "hour_bucket", "p10", "p50", "p90", "sample_count")
        rows = [dict(zip(cols, t)) for t in cur.fetchall()]
        cur.close()
        return times, rows
    finally:
        conn.close()


def main(argv=None):
    from baseline_utils import ENDPOINT_SLOS
    import baseline_job

    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--journal", required=True, help="journal TSV de scripts/run_compressed_days.sh")
    p.add_argument("--load-js", default=os.path.join(here, "..", "k6", "load.js"))
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    p.add_argument("--max-start-offset", type=int, default=DEFAULT_MAX_START_OFFSET_S)
    p.add_argument("--max-gap", type=int, default=DEFAULT_MAX_GAP_S)
    p.add_argument("--min-corr", type=float, default=DEFAULT_MIN_CORR)
    args = p.parse_args(argv)
    try:
        with open(args.journal, encoding="utf-8") as f:
            cycles = parse_journal(f.read())
        with open(args.load_js, encoding="utf-8") as f:
            start_vus, stages = parse_k6_stages(f.read())
        if sum(d for d, _ in stages) != season.CYCLE_MINUTES * 60:
            raise ValueError(f"load.js dure {sum(d for d, _ in stages)} s, "
                             f"{season.CYCLE_MINUTES * 60} s attendues (une journee simulee)")
        if not cycles:
            raise ValueError("journal vide")
        times, rows = _read_db(cycles[0]["boundary"], cycles[-1]["end"])
        starts, ends = baseline_job.load_injection_windows()
    except (OSError, ValueError) as e:
        print(f"ERREUR : {e}", file=sys.stderr)
        return 2
    res = assess(cycles=cycles, times_by_endpoint=times, injections=list(zip(starts, ends)),
                 rows=rows, endpoints=sorted(ENDPOINT_SLOS), metrics=list(baseline_job.METRICS),
                 profile=expected_slot_profile(start_vus, stages), days=args.days,
                 min_samples=args.min_samples, max_start_offset_s=args.max_start_offset,
                 max_gap_s=args.max_gap, min_corr=args.min_corr)
    print("BASELINE PRETE" if res["pret"] else "BASELINE PAS PRETE")
    for r in res["raisons"]:
        print(f"  - {r}")
    for i in res["infos"]:
        print(f"  (info) {i}")
    for k, v in res["details"].items():
        print(f"  {k} : {v}")
    return 0 if res["pret"] else 1


if __name__ == "__main__":
    sys.exit(main())
