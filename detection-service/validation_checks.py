"""
validation_checks.py -- controles stricts d'integrite des campagnes d'evaluation.

Utilise par scenario-runner/run_evaluation_suite.py (preparation et finalisation
des campagnes) et, plus tard, par les evaluations officielles. Bibliotheque
standard uniquement (importable depuis l'hote, sans numpy).

Principe : chaque controle renvoie la LISTE DES RAISONS d'echec (vide = valide).
Aucune donnee manquante ou invalide n'est ignoree en silence ; une campagne qui
ne passe pas les controles est "impossible", jamais "reussie".

Format officiel d'une campagne (scenario-runner/run_evaluation_suite.py) :
  campaign_AAAAMMJJ_HHMM/
    manifest.json        campaign_id, started_at, commit_sha, scenarios, ...,
                         feature_fingerprint, baseline_fingerprint, completed_at
    summary.json         une entree par execution (scenario, tier, status, ...)
    baseline_snapshot.json
    ground_truth/<scenario>_<tier>_<ts>.json   (format scenario-runner/runner.py)
"""

import ast
import hashlib
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CAMPAIGN_NAME_RE = re.compile(r"^campaign_\d{8}_\d{4}$")
TIERS = ("core", "stress")
REQUIRED_MANIFEST_KEYS = ("campaign_id", "started_at", "commit_sha", "scenarios",
                          "feature_names", "feature_fingerprint",
                          "baseline_snapshot", "baseline_fingerprint", "baseline_row_count")
# Champs d'une entree de faute ecrits par scenario-runner/runner.py.
FAULT_FIELDS = ("scenario_id", "fault_type", "target_service", "target_endpoint",
                "injected_at", "cleared_at", "magnitude")
BASELINE_SNAPSHOT_NAME = "baseline_snapshot.json"
BASELINE_SNAPSHOT_FORMAT = 1
# Colonnes d'endpoint_baseline couvertes par l'empreinte : cle + valeurs de bande.
# sample_count / updated_at sont conserves dans l'instantane mais exclus de
# l'empreinte (updated_at change a chaque refresh horaire sans changer la bande).
BASELINE_KEY = ("endpoint_id", "metric", "dow", "hour_bucket")
BASELINE_VALUES = ("p10", "p50", "p90")


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def parse_aware(value, label, reasons):
    """datetime ISO 8601 AVEC fuseau, ou None (raison ajoutee)."""
    if not isinstance(value, str):
        reasons.append(f"{label} : date attendue (chaine ISO 8601), recu {value!r}")
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        reasons.append(f"{label} : date invalide {value!r}")
        return None
    if dt.tzinfo is None:
        reasons.append(f"{label} : date sans fuseau horaire {value!r}")
        return None
    return dt


def _read_json(path, label, reasons):
    """JSON lu en UTF-8 strict (BOM refuse), ou None (raison ajoutee)."""
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        reasons.append(f"{label} : illisible ({e})")
        return None
    if raw.startswith(b"\xef\xbb\xbf"):
        reasons.append(f"{label} : marque BOM UTF-8 interdite")
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        reasons.append(f"{label} : JSON invalide ({e})")
        return None


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

def check_ground_truth_file(path, now=None, not_after=None):
    """
    Controle strict d'un fichier ground truth. Renvoie (fenetres, raisons).
    fenetres : dicts des champs de faute + injected/cleared (datetime) + source.
    now       : une fin de panne posterieure (panne en cours / fin provisoire) est refusee.
    not_after : borne superieure (ex. completed_at de la campagne).
    """
    path = Path(path)
    label = path.name
    reasons = []
    data = _read_json(path, label, reasons)
    if data is None:
        return [], reasons
    if not isinstance(data, dict) or not isinstance(data.get("faults"), list):
        return [], [f"{label} : objet {{\"faults\": [...]}} attendu"]
    if not data["faults"]:
        return [], [f"{label} : aucune faute enregistree"]

    now = now or datetime.now(timezone.utc)
    windows = []
    for i, fault in enumerate(data["faults"]):
        where = f"{label} faute #{i}"
        if not isinstance(fault, dict):
            reasons.append(f"{where} : objet attendu")
            continue
        missing = [k for k in FAULT_FIELDS if k not in fault]
        if missing:
            reasons.append(f"{where} : champs manquants {missing}")
            continue
        bad = [k for k in ("scenario_id", "fault_type", "target_service", "target_endpoint")
               if not isinstance(fault[k], str) or not fault[k]]
        if bad:
            reasons.append(f"{where} : champs vides ou non textuels {bad}")
        if not isinstance(fault["magnitude"], dict):
            reasons.append(f"{where} : magnitude doit etre un objet")
        ia = parse_aware(fault["injected_at"], f"{where} injected_at", reasons)
        ca = parse_aware(fault["cleared_at"], f"{where} cleared_at", reasons)
        if ia is None or ca is None:
            continue
        if ca <= ia:
            reasons.append(f"{where} : cleared_at ({ca.isoformat()}) <= injected_at ({ia.isoformat()})")
            continue
        if ca > now:
            reasons.append(f"{where} : cleared_at dans le futur ({ca.isoformat()}) -- panne en cours "
                           f"ou fin provisoire")
            continue
        if not_after is not None and ca > not_after:
            reasons.append(f"{where} : cleared_at ({ca.isoformat()}) apres la fin de campagne "
                           f"({not_after.isoformat()})")
            continue
        windows.append({**fault, "injected": ia, "cleared": ca, "source": label})
    return windows, reasons


def check_windows_overlap(windows):
    """Raisons pour les fenetres en double ou qui se chevauchent (toutes cibles confondues)."""
    def key(w):
        return (w["injected"], w["cleared"], w["fault_type"], w["target_endpoint"])

    reasons = []
    seen = {}
    for w in windows:
        if key(w) in seen:
            reasons.append(f"fenetre en double : {w['source']} et {seen[key(w)]} "
                           f"({w['injected'].isoformat()} -> {w['cleared'].isoformat()})")
        else:
            seen[key(w)] = w["source"]
    ordered = sorted(windows, key=lambda w: (w["injected"], w["cleared"]))
    latest = None   # fenetre active la plus tardive vue jusqu'ici
    for w in ordered:
        # Doublon exact deja signale ci-dessus ; tout autre recouvrement = chevauchement.
        if latest is not None and w["injected"] < latest["cleared"] and key(w) != key(latest):
            reasons.append(
                f"fenetres qui se chevauchent : {latest['source']} "
                f"({latest['injected'].isoformat()} -> {latest['cleared'].isoformat()}) et "
                f"{w['source']} ({w['injected'].isoformat()} -> {w['cleared'].isoformat()})")
        if latest is None or w["cleared"] > latest["cleared"]:
            latest = w
    return reasons


# ---------------------------------------------------------------------------
# Campagne : structure, manifeste, summary, coherence
# ---------------------------------------------------------------------------

def _check_fingerprints(d, manifest, features_py, reasons):
    """Empreintes du manifeste : features coherentes, instantane de baseline coherent."""
    names = manifest.get("feature_names")
    if "feature_names" in manifest:
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            reasons.append("manifest.json : feature_names doit etre une liste non vide de noms")
        elif manifest.get("feature_fingerprint") != feature_fingerprint(names):
            reasons.append("manifest.json : feature_fingerprint incoherente avec feature_names")
        elif features_py is not None:
            try:
                current = extract_feature_names(features_py)
            except (OSError, ValueError, SyntaxError) as e:
                reasons.append(f"features du code actuel illisibles ({e})")
            else:
                if feature_fingerprint(current) != manifest.get("feature_fingerprint"):
                    reasons.append("features du code actuel differentes de celles de la campagne")
    if "baseline_fingerprint" not in manifest:
        return
    if manifest.get("baseline_snapshot") != BASELINE_SNAPSHOT_NAME:
        reasons.append(f"manifest.json : baseline_snapshot doit valoir {BASELINE_SNAPSHOT_NAME!r}")
        return
    rows, snap_reasons = check_baseline_snapshot(d, manifest["baseline_fingerprint"])
    reasons.extend(snap_reasons)
    if not snap_reasons and manifest.get("baseline_row_count") != len(rows):
        reasons.append(f"manifest.json : baseline_row_count ({manifest.get('baseline_row_count')}) "
                       f"different du nombre de lignes de l'instantane ({len(rows)})")


def check_campaign(campaign_dir, now=None, require_completed=True, features_py=None):
    """
    Controle complet d'une campagne au format officiel. Renvoie un dict :
      ok (bool), reasons (list), manifest, final_entries {(scenario, tier): entry},
      windows (fenetres des executions retenues).
    require_completed=False : controle avant finalisation (completed_at pas encore ecrit).
    features_py : chemin de features.py du code actuel ; si fourni, les features de
                  la campagne doivent etre identiques (noms et ordre).
    """
    d = Path(campaign_dir)
    reasons = []
    out = {"ok": False, "reasons": reasons, "manifest": None, "final_entries": {}, "windows": []}

    if not d.is_dir():
        reasons.append(f"{d} : dossier de campagne introuvable")
        return out
    if not CAMPAIGN_NAME_RE.match(d.name):
        reasons.append(f"{d.name} : nom de campagne invalide (campaign_AAAAMMJJ_HHMM attendu)")
    gt_dir = d / "ground_truth"
    if not gt_dir.is_dir():
        reasons.append("ground_truth/ absent")

    manifest = _read_json(d / "manifest.json", "manifest.json", reasons) \
        if (d / "manifest.json").exists() else None
    if manifest is None and not (d / "manifest.json").exists():
        reasons.append("manifest.json absent")
    completed = None
    scenarios = []
    if manifest is not None:
        if not isinstance(manifest, dict):
            reasons.append("manifest.json : objet attendu")
            manifest = None
        else:
            out["manifest"] = manifest
            missing = [k for k in REQUIRED_MANIFEST_KEYS if k not in manifest]
            if missing:
                reasons.append(f"manifest.json : champs manquants {missing}")
            if manifest.get("campaign_id") != d.name:
                reasons.append(f"manifest.json : campaign_id {manifest.get('campaign_id')!r} "
                               f"different du dossier {d.name!r}")
            if "started_at" in manifest:
                parse_aware(manifest["started_at"], "manifest.json started_at", reasons)
            sc = manifest.get("scenarios")
            if "scenarios" in manifest:
                if (not isinstance(sc, list) or not sc
                        or not all(isinstance(s, str) and s for s in sc)):
                    reasons.append("manifest.json : scenarios doit etre une liste non vide de noms")
                elif len(set(sc)) != len(sc):
                    reasons.append("manifest.json : scenarios en double")
                else:
                    scenarios = sc
            _check_fingerprints(d, manifest, features_py, reasons)
            if require_completed:
                if "completed_at" not in manifest:
                    reasons.append("manifest.json : completed_at absent (campagne non finalisee)")
                else:
                    completed = parse_aware(manifest["completed_at"],
                                            "manifest.json completed_at", reasons)

    summary_path = d / "summary.json"
    entries = None
    if not summary_path.exists():
        reasons.append("summary.json absent")
    else:
        entries = _read_json(summary_path, "summary.json", reasons)
        if entries is not None and (not isinstance(entries, list)
                                    or not all(isinstance(e, dict) for e in entries)):
            reasons.append("summary.json : liste d'objets attendue")
            entries = None

    final = {}
    if entries is not None:
        by_pair = defaultdict(list)
        for i, e in enumerate(entries):
            sc_name, tier = e.get("scenario"), e.get("tier")
            if not isinstance(sc_name, str) or tier not in TIERS:
                reasons.append(f"summary.json entree #{i} : scenario/tier invalides ({sc_name!r}, {tier!r})")
                continue
            if scenarios and sc_name not in scenarios:
                reasons.append(f"summary.json entree #{i} : execution supplementaire "
                               f"{sc_name}/{tier} (scenario absent du manifeste)")
                continue
            by_pair[(sc_name, tier)].append(e)
        for sc_name in scenarios:
            for tier in TIERS:
                runs = by_pair.get((sc_name, tier), [])
                if not runs:
                    reasons.append(f"execution manquante : {sc_name}/{tier}")
                    continue
                oks = [r for r in runs if r.get("status") == "ok"]
                last = runs[-1]
                if last.get("status") != "ok":
                    reasons.append(f"{sc_name}/{tier} : derniere execution au statut "
                                   f"{last.get('status')!r} (seul 'ok' est valide)")
                    continue
                if len(oks) > 1:
                    reasons.append(f"{sc_name}/{tier} : {len(oks)} executions 'ok' (doublon)")
                    continue
                if not isinstance(last.get("ground_truth_file"), str):
                    reasons.append(f"{sc_name}/{tier} : ground_truth_file absent de l'entree 'ok'")
                    continue
                final[(sc_name, tier)] = last
    out["final_entries"] = final

    if gt_dir.is_dir():
        present = {p.name for p in gt_dir.iterdir() if p.is_file()}
        referenced = {e["ground_truth_file"] for e in final.values()}
        for name in sorted(referenced - present):
            reasons.append(f"ground_truth/{name} : reference par summary.json mais absent")
        for name in sorted(present - referenced):
            reasons.append(f"ground_truth/{name} : fichier supplementaire non reference par une "
                           f"execution 'ok' retenue")
        others = sorted(p.name for p in gt_dir.iterdir() if not p.is_file())
        for name in others:
            reasons.append(f"ground_truth/{name} : entree inattendue (pas un fichier)")
        windows = []
        for name in sorted(referenced & present):
            w, r = check_ground_truth_file(gt_dir / name, now=now, not_after=completed)
            windows.extend(w)
            reasons.extend(r)
        reasons.extend(check_windows_overlap(windows))
        out["windows"] = windows

    out["ok"] = not reasons
    return out


def finalize_campaign(campaign_dir, now=None, features_py=None):
    """
    Ecrit completed_at dans manifest.json UNIQUEMENT si la campagne passe tous les
    controles (require_completed=False), empreintes et instantane compris.
    features_py : features du code actuel, qui doivent etre celles du manifeste.
    Ecriture atomique. Renvoie (finalisee, raisons).
    Ne touche jamais aux fichiers ground truth ni au summary.
    """
    now = now or datetime.now(timezone.utc)
    result = check_campaign(campaign_dir, now=now, require_completed=False, features_py=features_py)
    if not result["ok"]:
        return False, result["reasons"]
    manifest = dict(result["manifest"])
    if "completed_at" in manifest:
        return False, ["manifest.json : completed_at deja present (non reecrit)"]
    manifest["completed_at"] = now.isoformat()
    path = Path(campaign_dir) / "manifest.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return True, []


# ---------------------------------------------------------------------------
# Empreintes : features et baseline
# ---------------------------------------------------------------------------

def _sha256_json(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def extract_feature_names(features_py):
    """FEATURE_NAMES lu dans le source de features.py (sans importer numpy)."""
    tree = ast.parse(Path(features_py).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "FEATURE_NAMES" for t in node.targets):
            names = ast.literal_eval(node.value)
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ValueError("FEATURE_NAMES doit etre une liste de noms")
            return names
    raise ValueError(f"FEATURE_NAMES introuvable dans {features_py}")


def feature_fingerprint(names):
    """Empreinte de la liste ORDONNEE des features (l'ordre compte pour le modele)."""
    return _sha256_json(list(names))


def _baseline_reasons(rows):
    reasons = []
    for i, r in enumerate(rows):
        missing = [k for k in BASELINE_KEY + BASELINE_VALUES if k not in r]
        if missing:
            reasons.append(f"baseline ligne #{i} : colonnes manquantes {missing}")
            continue
        for k in BASELINE_VALUES:
            v = r[k]
            if v is not None and not (isinstance(v, (int, float)) and math.isfinite(v)):
                reasons.append(f"baseline ligne #{i} {r['endpoint_id']}/{r['metric']} : {k} non fini ({v!r})")
    keys = [tuple(r.get(k) for k in BASELINE_KEY) for r in rows]
    dup = len(keys) - len(set(keys))
    if dup:
        reasons.append(f"baseline : {dup} ligne(s) en double sur la cle {BASELINE_KEY}")
    return reasons


def baseline_fingerprint(rows):
    """
    Empreinte stable de la baseline : cle (endpoint, metrique, jour, heure) + bande
    (p10, p50, p90). Independante de l'ordre des lignes ; leve ValueError si les
    lignes sont invalides (jamais d'empreinte sur des donnees douteuses).
    """
    reasons = _baseline_reasons(rows)
    if reasons:
        raise ValueError("; ".join(reasons))
    canon = sorted(
        [r["endpoint_id"], r["metric"], int(r["dow"]), int(r["hour_bucket"])]
        + [None if r[k] is None else float(r[k]) for k in BASELINE_VALUES]
        for r in rows)
    return _sha256_json(canon)


def build_baseline_snapshot(rows, taken_at):
    """Instantane serialisable de endpoint_baseline (toutes colonnes) + empreinte."""
    clean = []
    for r in rows:
        row = dict(r)
        if isinstance(row.get("updated_at"), datetime):
            row["updated_at"] = row["updated_at"].isoformat()
        clean.append(row)
    return {
        "format": BASELINE_SNAPSHOT_FORMAT,
        "taken_at": taken_at.isoformat(),
        "row_count": len(clean),
        "fingerprint": baseline_fingerprint(clean),
        "rows": clean,
    }


def write_baseline_snapshot(campaign_dir, rows, taken_at):
    """Ecrit baseline_snapshot.json (jamais d'ecrasement). Renvoie l'empreinte."""
    path = Path(campaign_dir) / BASELINE_SNAPSHOT_NAME
    snapshot = build_baseline_snapshot(rows, taken_at)
    with open(path, "x", encoding="utf-8") as f:   # "x" : echec si le fichier existe
        json.dump(snapshot, f, indent=1, allow_nan=False)
    return snapshot["fingerprint"]


def check_baseline_snapshot(campaign_dir, expected_fingerprint):
    """(lignes, raisons) : instantane present, bien forme, empreinte recalculee = attendue."""
    reasons = []
    path = Path(campaign_dir) / BASELINE_SNAPSHOT_NAME
    if not path.exists():
        return [], [f"{BASELINE_SNAPSHOT_NAME} absent"]
    snap = _read_json(path, BASELINE_SNAPSHOT_NAME, reasons)
    if snap is None:
        return [], reasons
    if not isinstance(snap, dict) or snap.get("format") != BASELINE_SNAPSHOT_FORMAT \
            or not isinstance(snap.get("rows"), list):
        return [], [f"{BASELINE_SNAPSHOT_NAME} : format inconnu"]
    rows = snap["rows"]
    try:
        fp = baseline_fingerprint(rows)
    except ValueError as e:
        return [], [f"{BASELINE_SNAPSHOT_NAME} : {e}"]
    if fp != snap.get("fingerprint"):
        reasons.append(f"{BASELINE_SNAPSHOT_NAME} : empreinte recalculee differente de l'empreinte stockee")
    if fp != expected_fingerprint:
        reasons.append(f"{BASELINE_SNAPSHOT_NAME} : empreinte differente de celle du manifeste")
    return (rows if not reasons else []), reasons


# ---------------------------------------------------------------------------
# Completude de la baseline et couverture des fenetres (fonctions pures)
# ---------------------------------------------------------------------------

# Largeur minimale d'une bande utilisable : meme valeur que features.signed_deviation
# (bande < 1e-6 -> deviation forcee a 0). Ce n'est pas un seuil de campagne.
BAND_EPS = 1e-6


def _require_aware(dt, label):
    """Leve ValueError si dt n'est pas un datetime avec fuseau explicite."""
    if not isinstance(dt, datetime):
        raise ValueError(f"{label} : datetime attendu, recu {type(dt).__name__}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{label} : date sans fuseau horaire ({dt.isoformat()})")
    return dt


def _is_finite_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def window_list_fingerprint(windows):
    """
    Empreinte SHA-256 stable d'une liste de fenetres (celles de check_campaign) :
    independante de l'ordre ; dates converties en UTC (meme instant = meme empreinte) ;
    dates sans fuseau refusees (ValueError). Sert a prouver que plusieurs
    detecteurs ont ete evalues sur exactement les memes fenetres.
    """
    canon = []
    for i, w in enumerate(windows):
        for k in ("source", "fault_type", "target_endpoint", "injected", "cleared"):
            if k not in w:
                raise ValueError(f"fenetre #{i} : champ {k!r} manquant")
        injected = _require_aware(w["injected"], f"fenetre #{i} injected").astimezone(timezone.utc)
        cleared = _require_aware(w["cleared"], f"fenetre #{i} cleared").astimezone(timezone.utc)
        canon.append([w["source"], w.get("scenario_id"), w["fault_type"], w.get("target_service"),
                      w["target_endpoint"], w.get("magnitude"),
                      injected.isoformat(), cleared.isoformat()])
    canon.sort(key=lambda c: json.dumps(c, sort_keys=True))
    return _sha256_json(canon)


def _pg_dow(dt):
    """Jour de semaine convention PostgreSQL EXTRACT(DOW) : 0 = dimanche."""
    return (dt.weekday() + 1) % 7


def _hour_slots(start, end):
    """Creneaux (dow, heure) UTC touches par [start, end]."""
    slots = []
    t = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = end.astimezone(timezone.utc)
    while t <= end:
        slot = (_pg_dow(t), t.hour)
        if slot not in slots:
            slots.append(slot)
        t += timedelta(hours=1)
    return slots


def _band_is_valid(p10, p90):
    """Bande utilisable par features.signed_deviation : bornes finies, largeur >= BAND_EPS."""
    return _is_finite_number(p10) and _is_finite_number(p90) and (p90 - p10) >= BAND_EPS


def baseline_completeness(rows, windows, metrics, *, context_s, grace_s):
    """
    Pour chaque fenetre et chaque metrique, statut de la baseline de l'endpoint vise
    sur les creneaux (jour, heure) de [injected - context_s, cleared + grace_s] :
      exact    : tous les creneaux ont une bande valide
      degenere : au moins une bande exacte inutilisable (borne vide ou non finie,
                 largeur < BAND_EPS) -- signale, sans bloquer (decision du lot 2b-2)
      fallback : au moins un creneau manque mais la metrique existe pour l'endpoint
      absent   : aucune ligne pour (endpoint, metrique)
    Chaque entree du rapport donne aussi "slots" (total, exact_valides,
    exact_degeneres, manquants) et "degeneres" (metriques concernees).
    Renvoie (rapport, raisons) ; raisons = fenetres avec une metrique absente
    (inchange). Dates sans fuseau refusees (ValueError).
    """
    exact = {(r["endpoint_id"], r["metric"], int(r["dow"]), int(r["hour_bucket"])):
             _band_is_valid(r.get("p10"), r.get("p90")) for r in rows}
    known = {(r["endpoint_id"], r["metric"]) for r in rows}
    report, reasons = [], []
    for i, w in enumerate(windows):
        _require_aware(w["injected"], f"fenetre #{i} injected")
        _require_aware(w["cleared"], f"fenetre #{i} cleared")
        ep = w["target_endpoint"]
        slots = _hour_slots(w["injected"] - timedelta(seconds=context_s),
                            w["cleared"] + timedelta(seconds=grace_s))
        status, counts = {}, {}
        for m in metrics:
            valid = [exact[(ep, m, d, h)] for d, h in slots if (ep, m, d, h) in exact]
            counts[m] = {"total": len(slots), "exact_valides": sum(valid),
                         "exact_degeneres": len(valid) - sum(valid),
                         "manquants": len(slots) - len(valid)}
            if (ep, m) not in known:
                status[m] = "absent"
            elif counts[m]["exact_degeneres"]:
                status[m] = "degenere"
            elif counts[m]["manquants"]:
                status[m] = "fallback"
            else:
                status[m] = "exact"
        report.append({"source": w["source"], "target_endpoint": ep, "metrics": status,
                       "slots": counts, "degeneres": [m for m, s in status.items() if s == "degenere"]})
        absents = [m for m, s in status.items() if s == "absent"]
        if absents:
            reasons.append(f"{w['source']} ({ep}) : baseline absente pour {absents}")
    return report, reasons


def fallback_band_report(rows, windows, metrics, *, context_s, grace_s):
    """
    Bande de secours de l'endpoint vise (min p10, max p90 de toutes ses lignes, comme
    train_model.baseline_lookup_from_rows) pour chaque (fenetre, metrique) dont au
    moins un creneau (jour, heure) n'a pas de ligne exacte. Une bande est valide si
    toutes les bornes contributives sont finies et si sa largeur >= BAND_EPS.
    Renvoie (rapport, raisons) :
      rapport : [{source, target_endpoint, metrics: {m: {creneaux_secours,
                 bande_secours [p10, p90] | None, valide True/False/None}}}]
                 (valide = None si aucun creneau n'utilise la bande de secours)
      raisons : fenetres dont un creneau devrait utiliser une bande de secours
                absente ou inutilisable.
    """
    exact = set()
    bounds = defaultdict(lambda: {"p10": [], "p90": []})
    for r in rows:
        key = (r["endpoint_id"], r["metric"])
        exact.add((*key, int(r["dow"]), int(r["hour_bucket"])))
        for k in ("p10", "p90"):
            if r.get(k) is not None:
                bounds[key][k].append(r[k])
    report, reasons = [], []
    for i, w in enumerate(windows):
        _require_aware(w["injected"], f"fenetre #{i} injected")
        _require_aware(w["cleared"], f"fenetre #{i} cleared")
        ep = w["target_endpoint"]
        slots = _hour_slots(w["injected"] - timedelta(seconds=context_s),
                            w["cleared"] + timedelta(seconds=grace_s))
        out = {}
        for m in metrics:
            n = sum(1 for d, h in slots if (ep, m, d, h) not in exact)
            b = bounds.get((ep, m))
            band, valid = None, None
            if b and b["p10"] and b["p90"]:
                band = [min(b["p10"]), max(b["p90"])]
            if n:
                finite = bool(b) and all(_is_finite_number(v) for v in b["p10"] + b["p90"])
                valid = band is not None and finite and _band_is_valid(*band)
                if not valid:
                    reasons.append(f"{w['source']} ({ep}) : {n} creneau(x) {m} sans ligne exacte et "
                                   f"bande de secours {'absente' if band is None else 'inutilisable'}")
            out[m] = {"creneaux_secours": n, "bande_secours": band, "valide": valid}
        report.append({"source": w["source"], "target_endpoint": ep, "metrics": out})
    return report, reasons


def _endpoint_times(times, label, exclude_last_point):
    """Dates triees (fuseau obligatoire) ; derniere retiree comme le rejeu (rows[:-1] si > 1)."""
    for t in times:
        _require_aware(t, label)
    times = sorted(times)
    if exclude_last_point and len(times) > 1:
        times = times[:-1]
    return times


def window_coverage(windows, points_by_endpoint, *, min_context, min_points,
                    context_s, grace_s, scrape_s, exclude_last_point=False):
    """
    Couverture de chaque fenetre sur l'endpoint VISE. points_by_endpoint :
    {endpoint: [datetime des mesures valides]}. Parametres obligatoires (issus du
    code appelant : MIN_WINDOW, M_WINDOWS, intervalle de collecte) -- aucun seuil
    de campagne (q, R, K) n'est applique ici : la completude est seulement mesuree.
    Une fenetre est evaluable si : contexte >= min_context, mesures pendant
    >= min_points, et duree (fenetre + grace) >= min_points x scrape_s.
    exclude_last_point=True : ignore la derniere mesure de chaque endpoint, comme le
    rejeu (watermark). max_gap_s : plus grand ecart entre deux mesures consecutives
    pendant la fenetre (None sous deux mesures), mesure sans seuil.
    Renvoie une liste de dicts {source, target_endpoint, fault_type, evaluable,
    reasons, context_points, during_points, expected_points, completeness, max_gap_s}.
    Dates sans fuseau refusees (ValueError).
    """
    report = []
    for i, w in enumerate(windows):
        _require_aware(w["injected"], f"fenetre #{i} injected")
        _require_aware(w["cleared"], f"fenetre #{i} cleared")
        ep = w["target_endpoint"]
        pts = _endpoint_times(points_by_endpoint.get(ep, []), f"mesure {ep}", exclude_last_point)
        start, end = w["injected"], w["cleared"] + timedelta(seconds=grace_s)
        ctx = [t for t in pts if start - timedelta(seconds=context_s) <= t < start]
        during = [t for t in pts if start <= t <= end]
        span = (end - start).total_seconds()
        expected = int(span // scrape_s) + 1   # une mesure par intervalle, bornes incluses
        why = []
        if not pts:
            why.append("aucune mesure sur l'endpoint vise")
        if span < min_points * scrape_s:
            why.append(f"panne trop courte ({span:.0f}s < {min_points} x {scrape_s}s)")
        if len(ctx) < min_context:
            why.append(f"contexte insuffisant avant la panne ({len(ctx)} < {min_context})")
        if len(during) < min_points:
            why.append(f"mesures insuffisantes pendant la panne ({len(during)} < {min_points})")
        gaps = [(b - a).total_seconds() for a, b in zip(during, during[1:])]
        report.append({
            "source": w["source"], "target_endpoint": ep, "fault_type": w["fault_type"],
            "evaluable": not why, "reasons": why,
            "context_points": len(ctx), "during_points": len(during),
            "expected_points": expected, "completeness": round(len(during) / expected, 3),
            "max_gap_s": max(gaps) if gaps else None,
        })
    return report


def metric_coverage(windows, rows_by_endpoint, metrics, *, context_s, grace_s,
                    exclude_last_point=False):
    """
    Disponibilite des valeurs VALIDES (non vides, finies) de chaque metrique sur
    l'endpoint vise, avant la panne ([injected - context_s, injected[) et pendant
    ([injected, cleared + grace_s]). rows_by_endpoint : {endpoint: [dicts avec
    "time" et les metriques]}. Mesure seulement : aucune metrique n'est imposee.
    Renvoie [{source, target_endpoint, fault_type, metrics: {m: {context_valides,
    pendant_valides, pendant_total}}}]. Dates sans fuseau refusees (ValueError).
    """
    report = []
    for i, w in enumerate(windows):
        _require_aware(w["injected"], f"fenetre #{i} injected")
        _require_aware(w["cleared"], f"fenetre #{i} cleared")
        ep = w["target_endpoint"]
        rows = rows_by_endpoint.get(ep, [])
        for r in rows:
            _require_aware(r.get("time"), f"ligne {ep}")
        rows = sorted(rows, key=lambda r: r["time"])
        if exclude_last_point and len(rows) > 1:
            rows = rows[:-1]
        start, end = w["injected"], w["cleared"] + timedelta(seconds=grace_s)
        ctx = [r for r in rows if start - timedelta(seconds=context_s) <= r["time"] < start]
        during = [r for r in rows if start <= r["time"] <= end]
        report.append({
            "source": w["source"], "target_endpoint": ep, "fault_type": w["fault_type"],
            "metrics": {m: {"context_valides": sum(_is_finite_number(r.get(m)) for r in ctx),
                            "pendant_valides": sum(_is_finite_number(r.get(m)) for r in during),
                            "pendant_total": len(during)} for m in metrics},
        })
    return report


def coverage_summary(report):
    """Totaux par type de faute et par endpoint (evaluables / total)."""
    by_type, by_ep = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for r in report:
        for agg, key in ((by_type, r["fault_type"]), (by_ep, r["target_endpoint"])):
            agg[key][1] += 1
            agg[key][0] += int(r["evaluable"])
    return {
        "total": len(report),
        "evaluable": sum(int(r["evaluable"]) for r in report),
        "by_fault_type": {k: {"evaluable": v[0], "total": v[1]} for k, v in by_type.items()},
        "by_endpoint": {k: {"evaluable": v[0], "total": v[1]} for k, v in by_ep.items()},
    }
