"""
Controles d'integrite des campagnes (validation_checks.py).

Uniquement des campagnes SYNTHETIQUES construites dans tmp_path : aucune donnee
reelle du projet n'est lue ni ecrite.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import validation_checks as vc

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[2]
SUITE = REPO / "scenario-runner" / "run_evaluation_suite.py"


# --- fabrique de campagne synthetique ----------------------------------------

def _fault(start, minutes=5, endpoint="GET /orders/{order_id}", fault_type="latency_step", **over):
    f = {
        "scenario_id": "s", "fault_type": fault_type, "target_service": "orders",
        "target_endpoint": endpoint,
        "injected_at": start.isoformat(),
        "cleared_at": (start + timedelta(minutes=minutes)).isoformat(),
        "magnitude": {"latency_ms": 500},
    }
    f.update(over)
    return f


FEATURES = ["a", "b", "c"]
BASE_ROWS = [
    {"endpoint_id": "GET /orders", "metric": "p99_ms", "dow": 0, "hour_bucket": 8,
     "p10": 90.0, "p50": 120.0, "p90": 180.0, "sample_count": 30, "updated_at": "2026-11-01T08:00:00+00:00"},
]


def make_campaign(root, scenarios=("s1", "s2"), name="campaign_20261101_0800", completed=False):
    d = root / name
    (d / "ground_truth").mkdir(parents=True)
    baseline_fp = vc.write_baseline_snapshot(d, BASE_ROWS, T0)
    manifest = {"campaign_id": name, "started_at": T0.isoformat(), "commit_sha": "abc",
                "scenarios": list(scenarios),
                "feature_names": FEATURES, "feature_fingerprint": vc.feature_fingerprint(FEATURES),
                "baseline_snapshot": vc.BASELINE_SNAPSHOT_NAME, "baseline_fingerprint": baseline_fp,
                "baseline_row_count": len(BASE_ROWS)}
    if completed:
        manifest["completed_at"] = NOW.isoformat()
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    entries, k = [], 0
    for sc in scenarios:
        for tier in vc.TIERS:
            gt = f"{sc}_{tier}_20261101_0800.json"
            start = T0 + timedelta(minutes=15 * k)
            (d / "ground_truth" / gt).write_text(json.dumps({"faults": [_fault(start)]}), encoding="utf-8")
            entries.append({"scenario": sc, "tier": tier, "status": "ok", "ground_truth_file": gt})
            k += 1
    (d / "summary.json").write_text(json.dumps(entries), encoding="utf-8")
    return d


def _check(d, require_completed=False):
    return vc.check_campaign(d, now=NOW, require_completed=require_completed)


def _has(result, fragment):
    return any(fragment in r for r in result["reasons"]), result["reasons"]


def _edit_json(path, fn):
    data = json.loads(path.read_text(encoding="utf-8"))
    data = fn(data) or data
    path.write_text(json.dumps(data), encoding="utf-8")


# --- campagne valide -------------------------------------------------------

def test_campagne_valide(tmp_path):
    r = _check(make_campaign(tmp_path))
    assert r["ok"], r["reasons"]
    assert len(r["final_entries"]) == 4 and len(r["windows"]) == 4


def test_campagne_valide_finalisee(tmp_path):
    r = _check(make_campaign(tmp_path, completed=True), require_completed=True)
    assert r["ok"], r["reasons"]


# --- structure et manifeste ------------------------------------------------

def test_dossier_absent(tmp_path):
    assert _has(_check(tmp_path / "campaign_20261101_0800"), "introuvable")[0]


def test_nom_de_campagne_invalide(tmp_path):
    d = make_campaign(tmp_path, name="ma_campagne")
    assert _has(_check(d), "nom de campagne invalide")[0]


def test_ground_truth_dossier_absent(tmp_path):
    d = make_campaign(tmp_path)
    for p in (d / "ground_truth").iterdir():
        p.unlink()
    (d / "ground_truth").rmdir()
    assert _has(_check(d), "ground_truth/ absent")[0]


def test_manifeste_absent(tmp_path):
    d = make_campaign(tmp_path)
    (d / "manifest.json").unlink()
    assert _has(_check(d), "manifest.json absent")[0]


@pytest.mark.parametrize("contenu,fragment", [
    (b"{pas du json", "JSON invalide"),
    (b"\xef\xbb\xbf{}", "BOM"),
    (b"[]", "objet attendu"),
])
def test_manifeste_illisible(tmp_path, contenu, fragment):
    d = make_campaign(tmp_path)
    (d / "manifest.json").write_bytes(contenu)
    r = _check(d)
    assert not r["ok"] and _has(r, fragment)[0], r["reasons"]


def test_manifeste_champs_manquants(tmp_path):
    d = make_campaign(tmp_path)
    _edit_json(d / "manifest.json", lambda m: m.pop("commit_sha") and None)
    assert _has(_check(d), "champs manquants ['commit_sha']")[0]


def test_manifeste_campaign_id_different(tmp_path):
    d = make_campaign(tmp_path)
    _edit_json(d / "manifest.json", lambda m: m.update(campaign_id="campaign_20990101_0000"))
    assert _has(_check(d), "different du dossier")[0]


@pytest.mark.parametrize("scenarios", [[], ["s1", "s1"], "s1"])
def test_manifeste_scenarios_invalides(tmp_path, scenarios):
    d = make_campaign(tmp_path)
    _edit_json(d / "manifest.json", lambda m: m.update(scenarios=scenarios))
    assert not _check(d)["ok"]


def test_manifeste_date_sans_fuseau(tmp_path):
    d = make_campaign(tmp_path)
    _edit_json(d / "manifest.json", lambda m: m.update(started_at="2026-11-01T08:00:00"))
    assert _has(_check(d), "sans fuseau")[0]


@pytest.mark.parametrize("cle", ["feature_names", "feature_fingerprint", "baseline_snapshot",
                                 "baseline_fingerprint", "baseline_row_count"])
def test_manifeste_empreintes_obligatoires(tmp_path, cle):
    d = make_campaign(tmp_path)
    _edit_json(d / "manifest.json", lambda m: m.pop(cle) and None)
    assert _has(_check(d), f"champs manquants ['{cle}']")[0]


def test_empreinte_features_incoherente(tmp_path):
    d = make_campaign(tmp_path)
    _edit_json(d / "manifest.json", lambda m: m.update(feature_names=["c", "b", "a"]))
    assert _has(_check(d), "feature_fingerprint incoherente")[0]


def test_features_du_code_actuel_differentes(tmp_path):
    d = make_campaign(tmp_path)
    src = tmp_path / "features.py"
    src.write_text("FEATURE_NAMES = ['a', 'b', 'autre']\n", encoding="utf-8")
    assert _has(vc.check_campaign(d, now=NOW, require_completed=False, features_py=src),
                "features du code actuel differentes")[0]
    src.write_text("FEATURE_NAMES = ['a', 'b', 'c']\n", encoding="utf-8")
    assert vc.check_campaign(d, now=NOW, require_completed=False, features_py=src)["ok"]


def test_instantane_baseline_absent_de_la_campagne(tmp_path):
    d = make_campaign(tmp_path)
    (d / vc.BASELINE_SNAPSHOT_NAME).unlink()
    assert _has(_check(d), f"{vc.BASELINE_SNAPSHOT_NAME} absent")[0]


def test_instantane_baseline_modifie_dans_la_campagne(tmp_path):
    d = make_campaign(tmp_path)
    p = d / vc.BASELINE_SNAPSHOT_NAME
    snap = json.loads(p.read_text(encoding="utf-8"))
    snap["rows"][0]["p50"] = 1.0
    p.write_text(json.dumps(snap), encoding="utf-8")
    assert _has(_check(d), "empreinte")[0]


def test_empreinte_baseline_du_manifeste_differente(tmp_path):
    d = make_campaign(tmp_path)
    _edit_json(d / "manifest.json", lambda m: m.update(baseline_fingerprint="0" * 64))
    assert _has(_check(d), "differente de celle du manifeste")[0]


def test_nom_d_instantane_inattendu(tmp_path):
    d = make_campaign(tmp_path)
    _edit_json(d / "manifest.json", lambda m: m.update(baseline_snapshot="autre.json"))
    assert _has(_check(d), "baseline_snapshot doit valoir")[0]


def test_nombre_de_lignes_de_baseline_incoherent(tmp_path):
    d = make_campaign(tmp_path)
    _edit_json(d / "manifest.json", lambda m: m.update(baseline_row_count=99))
    assert _has(_check(d), "baseline_row_count")[0]


def test_finalisation_refusee_sans_instantane(tmp_path):
    d = make_campaign(tmp_path)
    (d / vc.BASELINE_SNAPSHOT_NAME).unlink()
    avant = (d / "manifest.json").read_bytes()
    ok, reasons = vc.finalize_campaign(d, now=NOW)
    assert not ok and (d / "manifest.json").read_bytes() == avant


def test_finalisation_refusee_si_features_ont_change(tmp_path):
    d = make_campaign(tmp_path)
    src = tmp_path / "features.py"
    src.write_text("FEATURE_NAMES = ['x']\n", encoding="utf-8")
    ok, reasons = vc.finalize_campaign(d, now=NOW, features_py=src)
    assert not ok and any("features du code actuel" in r for r in reasons)


def test_completed_at_exige_pour_la_campagne_officielle(tmp_path):
    d = make_campaign(tmp_path)
    assert _has(_check(d, require_completed=True), "completed_at absent")[0]


# --- summary et executions -------------------------------------------------

def test_summary_absent(tmp_path):
    d = make_campaign(tmp_path)
    (d / "summary.json").unlink()
    assert _has(_check(d), "summary.json absent")[0]


@pytest.mark.parametrize("contenu", [b"{", b"{}", b"[1, 2]"])
def test_summary_invalide(tmp_path, contenu):
    d = make_campaign(tmp_path)
    (d / "summary.json").write_bytes(contenu)
    assert not _check(d)["ok"]


def _summary(d):
    return json.loads((d / "summary.json").read_text(encoding="utf-8"))


def _write_summary(d, entries):
    (d / "summary.json").write_text(json.dumps(entries), encoding="utf-8")


def test_execution_manquante(tmp_path):
    d = make_campaign(tmp_path)
    entries = _summary(d)
    (d / "ground_truth" / entries[-1]["ground_truth_file"]).unlink()
    _write_summary(d, entries[:-1])
    assert _has(_check(d), "execution manquante : s2/stress")[0]


def test_execution_supplementaire(tmp_path):
    d = make_campaign(tmp_path)
    _write_summary(d, _summary(d) + [{"scenario": "inconnu", "tier": "core", "status": "ok"}])
    assert _has(_check(d), "execution supplementaire")[0]


def test_tier_invalide(tmp_path):
    d = make_campaign(tmp_path)
    _write_summary(d, _summary(d) + [{"scenario": "s1", "tier": "moyen", "status": "ok"}])
    assert _has(_check(d), "scenario/tier invalides")[0]


@pytest.mark.parametrize("statut", ["error", "ok_no_metrics"])
def test_derniere_execution_non_ok(tmp_path, statut):
    d = make_campaign(tmp_path)
    entries = _summary(d)
    entries[0]["status"] = statut
    _write_summary(d, entries)
    r = _check(d)
    assert _has(r, f"statut '{statut}'")[0], r["reasons"]


def test_deux_executions_ok_pour_une_paire(tmp_path):
    d = make_campaign(tmp_path)
    entries = _summary(d)
    _write_summary(d, entries + [dict(entries[0])])
    assert _has(_check(d), "doublon")[0]


def test_erreur_puis_ok_sans_ground_truth_residuel_est_valide(tmp_path):
    # Une tentative echouee AVANT toute injection (pas de fichier) puis un 'ok'.
    d = make_campaign(tmp_path)
    entries = _summary(d)
    _write_summary(d, [{"scenario": "s1", "tier": "core", "status": "error",
                        "error": "preconditions"}] + entries)
    r = _check(d)
    assert r["ok"], r["reasons"]


def test_erreur_avec_ground_truth_residuel_est_signalee(tmp_path):
    # Tentative echouee APRES injection : son fichier reste -> fichier supplementaire.
    d = make_campaign(tmp_path)
    (d / "ground_truth" / "s1_core_20261101_0700.json").write_text(
        json.dumps({"faults": [_fault(T0 - timedelta(hours=1))]}), encoding="utf-8")
    assert _has(_check(d), "fichier supplementaire")[0]


def test_entree_ok_sans_ground_truth_file(tmp_path):
    d = make_campaign(tmp_path)
    entries = _summary(d)
    del entries[0]["ground_truth_file"]
    _write_summary(d, entries)
    assert _has(_check(d), "ground_truth_file absent")[0]


# --- fichiers ground truth -------------------------------------------------

def test_ground_truth_reference_mais_absent(tmp_path):
    d = make_campaign(tmp_path)
    (d / "ground_truth" / _summary(d)[0]["ground_truth_file"]).unlink()
    assert _has(_check(d), "reference par summary.json mais absent")[0]


def test_ground_truth_supplementaire(tmp_path):
    d = make_campaign(tmp_path)
    (d / "ground_truth" / "intrus.json").write_text("{}", encoding="utf-8")
    assert _has(_check(d), "ground_truth/intrus.json : fichier supplementaire")[0]


def test_ground_truth_sous_dossier_inattendu(tmp_path):
    d = make_campaign(tmp_path)
    (d / "ground_truth" / "sous_dossier").mkdir()
    assert _has(_check(d), "entree inattendue")[0]


def _gt_path(d):
    return d / "ground_truth" / _summary(d)[0]["ground_truth_file"]


@pytest.mark.parametrize("contenu,fragment", [
    (b"{oups", "JSON invalide"),
    (b"\xef\xbb\xbf{\"faults\": []}", "BOM"),
    (b"[]", "objet {\"faults\""),
    (b"{\"faults\": []}", "aucune faute"),
    (b"{\"faults\": [1]}", "objet attendu"),
])
def test_ground_truth_fichier_invalide(tmp_path, contenu, fragment):
    d = make_campaign(tmp_path)
    _gt_path(d).write_bytes(contenu)
    r = _check(d)
    assert _has(r, fragment)[0], r["reasons"]


@pytest.mark.parametrize("over,fragment", [
    ({"injected_at": None}, "date attendue"),
    ({"injected_at": "hier"}, "date invalide"),
    ({"injected_at": "2026-11-01T08:00:00"}, "sans fuseau"),
    ({"target_endpoint": ""}, "champs vides"),
    ({"magnitude": 500}, "magnitude"),
])
def test_ground_truth_champs_invalides(tmp_path, over, fragment):
    d = make_campaign(tmp_path)
    _gt_path(d).write_text(json.dumps({"faults": [_fault(T0, **over)]}), encoding="utf-8")
    r = _check(d)
    assert _has(r, fragment)[0], r["reasons"]


def test_ground_truth_champ_manquant(tmp_path):
    d = make_campaign(tmp_path)
    f = _fault(T0)
    del f["cleared_at"]
    _gt_path(d).write_text(json.dumps({"faults": [f]}), encoding="utf-8")
    assert _has(_check(d), "champs manquants ['cleared_at']")[0]


def test_ground_truth_fin_avant_debut(tmp_path):
    d = make_campaign(tmp_path)
    _gt_path(d).write_text(json.dumps({"faults": [_fault(T0, minutes=-1)]}), encoding="utf-8")
    assert _has(_check(d), "<= injected_at")[0]


def test_ground_truth_panne_en_cours(tmp_path):
    d = make_campaign(tmp_path)
    _gt_path(d).write_text(json.dumps({"faults": [_fault(NOW - timedelta(minutes=2), minutes=30)]}),
                           encoding="utf-8")
    assert _has(_check(d), "dans le futur")[0]


def test_ground_truth_fin_apres_completed_at(tmp_path):
    d = make_campaign(tmp_path)
    _edit_json(d / "manifest.json", lambda m: m.update(completed_at=(T0 + timedelta(minutes=3)).isoformat()))
    assert _has(_check(d, require_completed=True), "apres la fin de campagne")[0]


def test_fenetres_en_double(tmp_path):
    d = make_campaign(tmp_path)
    entries = _summary(d)
    contenu = (d / "ground_truth" / entries[0]["ground_truth_file"]).read_text(encoding="utf-8")
    (d / "ground_truth" / entries[1]["ground_truth_file"]).write_text(contenu, encoding="utf-8")
    assert _has(_check(d), "fenetre en double")[0]


def test_fenetres_qui_se_chevauchent(tmp_path):
    d = make_campaign(tmp_path)
    entries = _summary(d)
    (d / "ground_truth" / entries[1]["ground_truth_file"]).write_text(
        json.dumps({"faults": [_fault(T0 + timedelta(minutes=3))]}), encoding="utf-8")
    r = _check(d)
    ok, reasons = _has(r, "se chevauchent")
    assert ok, reasons
    assert entries[0]["ground_truth_file"] in " ".join(reasons)


def test_memes_dates_autre_endpoint_est_un_chevauchement():
    a = {**_fault(T0), "injected": T0, "cleared": T0 + timedelta(minutes=5), "source": "a.json"}
    b = {**_fault(T0, endpoint="POST /payments"), "injected": T0,
         "cleared": T0 + timedelta(minutes=5), "source": "b.json"}
    reasons = vc.check_windows_overlap([a, b])
    assert any("se chevauchent" in r for r in reasons) and not any("double" in r for r in reasons)


def test_fenetres_qui_se_touchent_ne_se_chevauchent_pas():
    a = {**_fault(T0), "injected": T0, "cleared": T0 + timedelta(minutes=5), "source": "a.json"}
    b = {**_fault(T0), "injected": T0 + timedelta(minutes=5),
         "cleared": T0 + timedelta(minutes=9), "source": "b.json"}
    assert vc.check_windows_overlap([a, b]) == []


# --- finalisation ------------------------------------------------------------

def test_finalisation_ecrit_completed_at(tmp_path):
    d = make_campaign(tmp_path)
    ok, reasons = vc.finalize_campaign(d, now=NOW)
    assert ok, reasons
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    assert m["completed_at"] == NOW.isoformat()
    assert not list(d.glob("*.tmp"))
    assert _check(d, require_completed=True)["ok"]


def test_finalisation_refusee_si_invalide_et_manifeste_intact(tmp_path):
    d = make_campaign(tmp_path)
    entries = _summary(d)
    entries[0]["status"] = "ok_no_metrics"
    _write_summary(d, entries)
    avant = (d / "manifest.json").read_bytes()
    ok, reasons = vc.finalize_campaign(d, now=NOW)
    assert not ok and reasons
    assert (d / "manifest.json").read_bytes() == avant


def test_finalisation_non_repetee(tmp_path):
    d = make_campaign(tmp_path)
    assert vc.finalize_campaign(d, now=NOW)[0]
    avant = (d / "manifest.json").read_bytes()
    ok, reasons = vc.finalize_campaign(d, now=NOW + timedelta(hours=1))
    assert not ok and "deja present" in reasons[0]
    assert (d / "manifest.json").read_bytes() == avant


# --- empreintes --------------------------------------------------------------

def test_extraction_et_empreinte_des_features(tmp_path):
    src = tmp_path / "features.py"
    src.write_text("import numpy as np\nFEATURE_NAMES = ['a', 'b', 'c']\n", encoding="utf-8")
    names = vc.extract_feature_names(src)
    assert names == ["a", "b", "c"]
    assert vc.feature_fingerprint(names) != vc.feature_fingerprint(["c", "b", "a"])  # ordre compte


def test_extraction_features_reelles_identique_au_module():
    import features
    assert vc.extract_feature_names(REPO / "detection-service" / "features.py") == features.FEATURE_NAMES


def test_extraction_features_absentes(tmp_path):
    src = tmp_path / "features.py"
    src.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        vc.extract_feature_names(src)


def _rows():
    return [
        {"endpoint_id": "GET /orders", "metric": "p99_ms", "dow": 0, "hour_bucket": 8,
         "p10": 90.0, "p50": 120.0, "p90": 180.0, "sample_count": 30, "updated_at": "2026-11-01T08:00:00+00:00"},
        {"endpoint_id": "GET /orders", "metric": "p95_ms", "dow": 0, "hour_bucket": 8,
         "p10": 80.0, "p50": 100.0, "p90": 150.0, "sample_count": 30, "updated_at": "2026-11-01T08:00:00+00:00"},
    ]


def test_empreinte_baseline_independante_de_l_ordre():
    rows = _rows()
    assert vc.baseline_fingerprint(rows) == vc.baseline_fingerprint(list(reversed(rows)))


def test_empreinte_baseline_detecte_une_valeur_modifiee():
    rows = _rows()
    autre = [dict(r) for r in rows]
    autre[0]["p90"] = 181.0
    assert vc.baseline_fingerprint(rows) != vc.baseline_fingerprint(autre)


def test_empreinte_baseline_ignore_refresh_sans_changement_de_bande():
    rows = _rows()
    autre = [dict(r, sample_count=99, updated_at="2026-11-02T00:00:00+00:00") for r in rows]
    assert vc.baseline_fingerprint(rows) == vc.baseline_fingerprint(autre)


@pytest.mark.parametrize("alter", [
    lambda rs: rs[0].update(p90=float("nan")),
    lambda rs: rs[0].pop("p50"),
    lambda rs: rs.append(dict(rs[0])),
])
def test_empreinte_baseline_refuse_donnees_invalides(alter):
    rows = [dict(r) for r in _rows()]
    alter(rows)
    with pytest.raises(ValueError):
        vc.baseline_fingerprint(rows)


def test_instantane_baseline_aller_retour(tmp_path):
    fp = vc.write_baseline_snapshot(tmp_path, _rows(), NOW)
    rows, reasons = vc.check_baseline_snapshot(tmp_path, fp)
    assert reasons == [] and len(rows) == 2


def test_instantane_jamais_ecrase(tmp_path):
    vc.write_baseline_snapshot(tmp_path, _rows(), NOW)
    avant = (tmp_path / vc.BASELINE_SNAPSHOT_NAME).read_bytes()
    with pytest.raises(FileExistsError):
        vc.write_baseline_snapshot(tmp_path, [], NOW)
    assert (tmp_path / vc.BASELINE_SNAPSHOT_NAME).read_bytes() == avant


def test_instantane_absent(tmp_path):
    assert vc.check_baseline_snapshot(tmp_path, "x")[1] == [f"{vc.BASELINE_SNAPSHOT_NAME} absent"]


def test_instantane_modifie_detecte(tmp_path):
    fp = vc.write_baseline_snapshot(tmp_path, _rows(), NOW)
    p = tmp_path / vc.BASELINE_SNAPSHOT_NAME
    snap = json.loads(p.read_text(encoding="utf-8"))
    snap["rows"][0]["p90"] = 999.0
    p.write_text(json.dumps(snap), encoding="utf-8")
    rows, reasons = vc.check_baseline_snapshot(tmp_path, fp)
    assert rows == [] and any("empreinte" in r for r in reasons)


def test_instantane_empreinte_differente_du_manifeste(tmp_path):
    vc.write_baseline_snapshot(tmp_path, _rows(), NOW)
    assert any("manifeste" in r for r in vc.check_baseline_snapshot(tmp_path, "autre")[1])


# --- completude de la baseline -------------------------------------------------

def _win(start=T0, minutes=5, endpoint="GET /orders", fault_type="latency_step", source="w.json"):
    return {"target_endpoint": endpoint, "fault_type": fault_type, "injected": start,
            "cleared": start + timedelta(minutes=minutes), "source": source}


def test_completude_exacte_fallback_absente():
    # T0 = dimanche 1er novembre 2026, 08h UTC -> creneau (0, 8).
    w = _win(T0 + timedelta(minutes=10))
    report, reasons = vc.baseline_completeness(
        _rows(), [w], ["p99_ms", "p95_ms", "rps"], context_s=600, grace_s=120)
    assert report[0]["metrics"] == {"p99_ms": "exact", "p95_ms": "exact", "rps": "absent"}
    assert reasons and "rps" in reasons[0]


def test_completude_creneau_manquant_donne_fallback():
    w = _win(T0 + timedelta(minutes=55))       # deborde sur le creneau 9h, absent
    report, reasons = vc.baseline_completeness(_rows(), [w], ["p99_ms"], context_s=600, grace_s=120)
    assert report[0]["metrics"]["p99_ms"] == "fallback" and reasons == []


def test_completude_metrique_p95_manquante():
    rows = [r for r in _rows() if r["metric"] != "p95_ms"]
    _, reasons = vc.baseline_completeness(rows, [_win(T0 + timedelta(minutes=10))], ["p95_ms"],
                                          context_s=600, grace_s=120)
    assert reasons and "p95_ms" in reasons[0]


# --- couverture des fenetres ---------------------------------------------------

PARAMS = dict(min_context=3, min_points=2, context_s=600, grace_s=120, scrape_s=60)


def _minutes(start, end):
    t, out = start, []
    while t <= end:
        out.append(t)
        t += timedelta(minutes=1)
    return out


def test_couverture_fenetre_evaluable():
    w = _win(T0, minutes=5)
    pts = {"GET /orders": _minutes(T0 - timedelta(minutes=10), T0 + timedelta(minutes=7))}
    r = vc.window_coverage([w], pts, **PARAMS)[0]
    assert r["evaluable"], r["reasons"]
    assert r["completeness"] == 1.0


def test_couverture_aucune_mesure():
    r = vc.window_coverage([_win()], {}, **PARAMS)[0]
    assert not r["evaluable"] and "aucune mesure sur l'endpoint vise" in r["reasons"]


def test_couverture_mesures_sur_un_autre_endpoint_seulement():
    pts = {"POST /payments": _minutes(T0 - timedelta(minutes=10), T0 + timedelta(minutes=7))}
    r = vc.window_coverage([_win()], pts, **PARAMS)[0]
    assert not r["evaluable"] and "aucune mesure sur l'endpoint vise" in r["reasons"]


def test_couverture_panne_trop_courte():
    w = {**_win(T0), "cleared": T0 + timedelta(seconds=0.5)}
    pts = {"GET /orders": _minutes(T0 - timedelta(minutes=10), T0 + timedelta(minutes=3))}
    r = vc.window_coverage([w], pts, min_context=3, min_points=3, context_s=600, grace_s=60, scrape_s=60)[0]
    assert any("trop courte" in x for x in r["reasons"])


def test_couverture_contexte_insuffisant():
    pts = {"GET /orders": _minutes(T0, T0 + timedelta(minutes=7))}
    r = vc.window_coverage([_win()], pts, **PARAMS)[0]
    assert any("contexte insuffisant" in x for x in r["reasons"])


def test_couverture_mesures_insuffisantes_et_trous():
    pts = {"GET /orders": _minutes(T0 - timedelta(minutes=10), T0 - timedelta(minutes=1)) + [T0]}
    r = vc.window_coverage([_win()], pts, **PARAMS)[0]
    assert any("mesures insuffisantes" in x for x in r["reasons"])
    assert r["completeness"] < 1.0


def test_resume_de_couverture():
    ok = _win(T0, source="a.json")
    ko = _win(T0, endpoint="POST /payments", fault_type="error_burst", source="b.json")
    pts = {"GET /orders": _minutes(T0 - timedelta(minutes=10), T0 + timedelta(minutes=7))}
    s = vc.coverage_summary(vc.window_coverage([ok, ko], pts, **PARAMS))
    assert s["total"] == 2 and s["evaluable"] == 1
    assert s["by_fault_type"]["error_burst"] == {"evaluable": 0, "total": 1}
    assert s["by_endpoint"]["GET /orders"] == {"evaluable": 1, "total": 1}


# --- cablage de la suite de campagne (statique : yaml absent de l'image de test) --

def _suite_src():
    return SUITE.read_text(encoding="utf-8")


def _method(src, name):
    start = src.index(f"def {name}(")
    nxt = src.find("\n    def ", start + 1)
    return src[start: nxt if nxt != -1 else len(src)]


def test_suite_reprise_ne_compte_que_ok():
    body = _method(_suite_src(), "_already_done")
    assert 'e.get("status") == "ok"' in body and "ok_no_metrics" not in body.split("return")[1]


def test_suite_verification_finale_ne_compte_que_ok_et_finalise():
    body = _method(_suite_src(), "_print_final_verification")
    assert 'e.get("status") == "ok"' in body
    assert "vc.finalize_campaign(self.campaign_dir, features_py=FEATURES_PY)" in body


def test_suite_ground_truth_controle_strict():
    src = _suite_src()
    body = src[src.index("def verify_ground_truth"): src.index("def verify_ground_truth") + 600]
    assert "vc.check_ground_truth_file(gt_file)" in body


def test_suite_manifeste_avec_empreintes_et_instantane_en_lecture_seule():
    src = _suite_src()
    manifest = _method(src, "_write_manifest")
    for key in ('"feature_fingerprint"', '"baseline_fingerprint"', '"baseline_snapshot"', '"scenarios"'):
        assert key in manifest
    snap = _method(src, "_snapshot_baseline")
    assert "set_session(readonly=True)" in snap and "vc.write_baseline_snapshot" in snap
    import re
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER)\b", snap.upper())


# --- lot 2b-1 : empreinte de la liste des fenetres ----------------------------

def _gt_win(start=T0, minutes=5, endpoint="GET /orders", source="a.json", **over):
    w = {**_fault(start, minutes, endpoint=endpoint), "injected": start,
         "cleared": start + timedelta(minutes=minutes), "source": source}
    w.update(over)
    return w


def test_empreinte_fenetres_independante_de_l_ordre():
    a, b = _gt_win(source="a.json"), _gt_win(T0 + timedelta(minutes=10), source="b.json")
    assert vc.window_list_fingerprint([a, b]) == vc.window_list_fingerprint([b, a])


@pytest.mark.parametrize("over", [
    {"target_endpoint": "POST /payments"},
    {"cleared": T0 + timedelta(minutes=5, seconds=1)},
    {"magnitude": {"latency_ms": 501}},
    {"fault_type": "error_burst"},
    {"source": "autre.json"},
])
def test_empreinte_fenetres_change_si_un_champ_change(over):
    assert vc.window_list_fingerprint([_gt_win()]) != vc.window_list_fingerprint([_gt_win(**over)])


def test_empreinte_fenetres_meme_instant_autre_fuseau():
    paris = timezone(timedelta(hours=1))
    w_utc = _gt_win()
    w_paris = _gt_win(injected=T0.astimezone(paris), cleared=(T0 + timedelta(minutes=5)).astimezone(paris))
    assert vc.window_list_fingerprint([w_utc]) == vc.window_list_fingerprint([w_paris])


@pytest.mark.parametrize("over", [
    {"injected": datetime(2026, 11, 1, 8, 0)},       # sans fuseau
    {"cleared": "2026-11-01T08:05:00+00:00"},         # pas un datetime
])
def test_empreinte_fenetres_refuse_dates_invalides(over):
    with pytest.raises(ValueError):
        vc.window_list_fingerprint([_gt_win(**over)])


def test_empreinte_fenetres_refuse_champ_manquant():
    w = _gt_win()
    del w["target_endpoint"]
    with pytest.raises(ValueError):
        vc.window_list_fingerprint([w])


# --- lot 2b-1 : plages de baseline valides, degenerees, manquantes -------------

@pytest.mark.parametrize("bande", [
    {"p10": 120.0, "p90": 120.0},              # largeur nulle
    {"p10": None, "p90": 180.0},               # borne vide
    {"p10": 90.0, "p90": float("nan")},        # non finie
])
def test_completude_bande_degeneree_signalee_sans_bloquer(bande):
    rows = [dict(r, **bande) if r["metric"] == "p99_ms" else r for r in _rows()]
    report, reasons = vc.baseline_completeness(rows, [_win(T0 + timedelta(minutes=10))], ["p99_ms", "p95_ms"],
                                               context_s=600, grace_s=120)
    r = report[0]
    assert r["metrics"] == {"p99_ms": "degenere", "p95_ms": "exact"}
    assert r["degeneres"] == ["p99_ms"]
    assert r["slots"]["p99_ms"] == {"total": 1, "exact_valides": 0, "exact_degeneres": 1, "manquants": 0}
    assert reasons == []            # signale, la decision de bloquer relevera du lot 2b-2


def test_completude_compte_les_creneaux_manquants():
    report, _ = vc.baseline_completeness(_rows(), [_win(T0 + timedelta(minutes=55))], ["p99_ms"],
                                         context_s=600, grace_s=120)
    assert report[0]["slots"]["p99_ms"] == {"total": 2, "exact_valides": 1, "exact_degeneres": 0, "manquants": 1}
    assert report[0]["metrics"]["p99_ms"] == "fallback"


def test_completude_compatibilite_statut_exact_et_absent():
    report, reasons = vc.baseline_completeness(_rows(), [_win(T0 + timedelta(minutes=10))], ["p99_ms", "rps"],
                                               context_s=600, grace_s=120)
    assert report[0]["metrics"] == {"p99_ms": "exact", "rps": "absent"}
    assert report[0]["slots"]["rps"] == {"total": 1, "exact_valides": 0, "exact_degeneres": 0, "manquants": 1}
    assert reasons and "rps" in reasons[0]


def test_completude_refuse_date_sans_fuseau():
    w = _win(datetime(2026, 11, 1, 8, 0))
    with pytest.raises(ValueError):
        vc.baseline_completeness(_rows(), [w], ["p99_ms"], context_s=600, grace_s=120)


# --- lot 2b-1 : options de window_coverage --------------------------------------

def _pts():
    return {"GET /orders": _minutes(T0 - timedelta(minutes=10), T0 + timedelta(minutes=7))}


def test_couverture_comportement_par_defaut_inchange():
    r = vc.window_coverage([_win()], _pts(), **PARAMS)[0]
    assert (r["context_points"], r["during_points"], r["expected_points"]) == (10, 8, 8)


def test_couverture_exclusion_de_la_derniere_mesure():
    defaut = vc.window_coverage([_win()], _pts(), **PARAMS)[0]
    sans_derniere = vc.window_coverage([_win()], _pts(), exclude_last_point=True, **PARAMS)[0]
    assert sans_derniere["during_points"] == defaut["during_points"] - 1


def test_couverture_exclusion_sans_effet_sur_une_seule_mesure():
    # Comme le rejeu (rows[:-1] seulement si plus d'une ligne).
    pts = {"GET /orders": [T0 + timedelta(minutes=1)]}
    r = vc.window_coverage([_win()], pts, exclude_last_point=True, **PARAMS)[0]
    assert r["during_points"] == 1


def test_couverture_plus_grand_trou():
    pts = {"GET /orders": [T0 + timedelta(minutes=m) for m in (0, 1, 6, 7)]}
    r = vc.window_coverage([_win()], pts, **PARAMS)[0]
    assert r["max_gap_s"] == 300.0


def test_couverture_plus_grand_trou_indefini_sous_deux_mesures():
    r = vc.window_coverage([_win()], {"GET /orders": [T0]}, **PARAMS)[0]
    assert r["max_gap_s"] is None


def test_couverture_refuse_mesure_sans_fuseau():
    with pytest.raises(ValueError):
        vc.window_coverage([_win()], {"GET /orders": [datetime(2026, 11, 1, 8, 1)]}, **PARAMS)


def test_couverture_et_disponibilite_refusent_fenetre_sans_fuseau():
    w = _win(datetime(2026, 11, 1, 8, 0))
    with pytest.raises(ValueError):
        vc.window_coverage([w], _pts(), **PARAMS)
    with pytest.raises(ValueError):
        vc.metric_coverage([w], {}, ["p99_ms"], context_s=600, grace_s=120)


# --- lot 2b-1 : disponibilite des metriques --------------------------------------

def _row(t, **vals):
    base = {"p50_ms": 50.0, "p95_ms": 90.0, "p99_ms": 120.0, "rps": 10.0}
    base.update(vals)
    return {"time": t, **base}


def test_metric_coverage_compte_les_valeurs_valides():
    rows = {"GET /orders": [
        _row(T0 - timedelta(minutes=2)),
        _row(T0 - timedelta(minutes=1), p95_ms=None),
        _row(T0 + timedelta(minutes=1), p50_ms=float("nan")),
        _row(T0 + timedelta(minutes=2), p95_ms=None, rps=float("inf")),
    ]}
    r = vc.metric_coverage([_win()], rows, ["p50_ms", "p95_ms", "p99_ms", "rps"], context_s=600, grace_s=120)[0]
    assert r["metrics"]["p99_ms"] == {"context_valides": 2, "pendant_valides": 2, "pendant_total": 2}
    assert r["metrics"]["p95_ms"] == {"context_valides": 1, "pendant_valides": 1, "pendant_total": 2}
    assert r["metrics"]["p50_ms"] == {"context_valides": 2, "pendant_valides": 1, "pendant_total": 2}
    assert r["metrics"]["rps"] == {"context_valides": 2, "pendant_valides": 1, "pendant_total": 2}


def test_metric_coverage_metrique_absente_des_lignes():
    rows = {"GET /orders": [{"time": T0 + timedelta(minutes=1), "p99_ms": 100.0}]}
    r = vc.metric_coverage([_win()], rows, ["p95_ms"], context_s=600, grace_s=120)[0]
    assert r["metrics"]["p95_ms"] == {"context_valides": 0, "pendant_valides": 0, "pendant_total": 1}


def test_metric_coverage_aucune_ligne_pour_l_endpoint():
    r = vc.metric_coverage([_win()], {}, ["p99_ms"], context_s=600, grace_s=120)[0]
    assert r["metrics"]["p99_ms"] == {"context_valides": 0, "pendant_valides": 0, "pendant_total": 0}


def test_metric_coverage_exclusion_de_la_derniere_ligne():
    rows = {"GET /orders": [_row(T0 + timedelta(minutes=m)) for m in (1, 2, 3)]}
    r = vc.metric_coverage([_win()], rows, ["p99_ms"], context_s=600, grace_s=120, exclude_last_point=True)[0]
    assert r["metrics"]["p99_ms"]["pendant_total"] == 2


def test_bande_de_secours_valide_comptee():
    w = _win(T0 + timedelta(minutes=55))          # creneaux 8h (present) et 9h (absent)
    rows = _rows() + [dict(_rows()[0], hour_bucket=10, p10=70.0, p90=200.0)]
    report, reasons = vc.fallback_band_report(rows, [w], ["p99_ms"], context_s=600, grace_s=120)
    m = report[0]["metrics"]["p99_ms"]
    assert m == {"creneaux_secours": 1, "bande_secours": [70.0, 200.0], "valide": True}
    assert reasons == []


def test_bande_de_secours_non_utilisee():
    report, reasons = vc.fallback_band_report(_rows(), [_win(T0 + timedelta(minutes=10))], ["p99_ms"],
                                              context_s=600, grace_s=120)
    assert report[0]["metrics"]["p99_ms"]["valide"] is None and reasons == []


@pytest.mark.parametrize("alter", [
    {"p10": 120.0, "p90": 120.0},                 # largeur nulle
    {"p10": float("nan")},                        # non finie
    {"p90": float("inf")},
])
def test_bande_de_secours_inutilisable(alter):
    w = _win(T0 + timedelta(minutes=55))
    rows = [dict(r, **alter) if r["metric"] == "p99_ms" else r for r in _rows()]
    report, reasons = vc.fallback_band_report(rows, [w], ["p99_ms"], context_s=600, grace_s=120)
    assert report[0]["metrics"]["p99_ms"]["valide"] is False
    assert reasons and "inutilisable" in reasons[0]


def test_bande_de_secours_absente():
    w = _win(T0 + timedelta(minutes=10))
    report, reasons = vc.fallback_band_report(_rows(), [w], ["rps"], context_s=600, grace_s=120)
    assert report[0]["metrics"]["rps"] == {"creneaux_secours": 1, "bande_secours": None, "valide": False}
    assert reasons and "absente" in reasons[0]


def test_metric_coverage_refuse_date_sans_fuseau():
    with pytest.raises(ValueError):
        vc.metric_coverage([_win()], {"GET /orders": [_row(datetime(2026, 11, 1, 8, 1))]}, ["p99_ms"],
                           context_s=600, grace_s=120)


# --- mode saisonnier (season.py) : campagnes et cases ----------------------------

import season  # noqa: E402

SEASON_C = {"mode": "compresse", "cycle_minutes": 120, "slot_minutes": 5}


def _set_manifest_season(d, value):
    def edit(m):
        m["season"] = value
    _edit_json(d / "manifest.json", edit)


def _replace_snapshot(d, rows):
    (d / vc.BASELINE_SNAPSHOT_NAME).unlink()
    fp = vc.write_baseline_snapshot(d, rows, T0)

    def edit(m):
        m.update(baseline_fingerprint=fp, baseline_row_count=len(rows))
    _edit_json(d / "manifest.json", edit)


def test_campagne_historique_sans_mode_valide_en_mode_reel(tmp_path):
    assert _check(make_campaign(tmp_path))["ok"]


def test_campagne_sans_mode_refusee_en_mode_compresse(tmp_path, monkeypatch):
    monkeypatch.setattr(season, "MODE", season.COMPRESSE)
    assert _has(_check(make_campaign(tmp_path)), "mode saisonnier de la campagne")[0]


def test_campagne_compressee_valide_en_mode_compresse(tmp_path, monkeypatch):
    monkeypatch.setattr(season, "MODE", season.COMPRESSE)
    d = make_campaign(tmp_path)
    _set_manifest_season(d, SEASON_C)
    r = _check(d)
    assert r["ok"], r["reasons"]


def test_campagne_compressee_refusee_en_mode_reel(tmp_path):
    d = make_campaign(tmp_path)
    _set_manifest_season(d, SEASON_C)
    assert _has(_check(d), "different du mode actuel")[0]


@pytest.mark.parametrize("value", ["compresse", {"mode": "hebdo"}, {}])
def test_mode_de_campagne_invalide(tmp_path, value):
    d = make_campaign(tmp_path)
    _set_manifest_season(d, value)
    assert _has(_check(d), "season invalide")[0]


def test_melange_de_modes_dans_l_instantane(tmp_path, monkeypatch):
    monkeypatch.setattr(season, "MODE", season.COMPRESSE)
    d = make_campaign(tmp_path)
    _set_manifest_season(d, SEASON_C)
    _replace_snapshot(d, BASE_ROWS + [dict(BASE_ROWS[0], dow=3, hour_bucket=14)])
    assert _has(_check(d), "incompatible(s) avec le mode saisonnier")[0]


def test_finalisation_refusee_si_le_mode_a_change(tmp_path, monkeypatch):
    d = make_campaign(tmp_path)                       # campagne lancee en mode reel
    monkeypatch.setattr(season, "MODE", season.COMPRESSE)
    ok, reasons = vc.finalize_campaign(d, now=NOW)
    assert not ok and any("mode saisonnier" in r for r in reasons)
    assert "completed_at" not in json.loads((d / "manifest.json").read_text(encoding="utf-8"))


def test_creneaux_par_pas_de_5_minutes_en_mode_compresse(monkeypatch):
    monkeypatch.setattr(season, "MODE", season.COMPRESSE)
    start = datetime(2026, 11, 1, 8, 2, tzinfo=timezone.utc)      # heure simulee 0
    assert vc._hour_slots(start, start + timedelta(minutes=11)) == [(0, 0), (0, 1), (0, 2)]


def test_completude_baseline_mode_compresse(monkeypatch):
    monkeypatch.setattr(season, "MODE", season.COMPRESSE)
    w = {"source": "f.json", "target_endpoint": "GET /orders", "fault_type": "latency_step",
         "injected": datetime(2026, 11, 1, 8, 40, tzinfo=timezone.utc),      # heure simulee 8
         "cleared": datetime(2026, 11, 1, 8, 42, tzinfo=timezone.utc)}
    report, _ = vc.baseline_completeness(BASE_ROWS, [w], ["p99_ms"], context_s=0, grace_s=0)
    assert report[0]["metrics"]["p99_ms"] == "exact"
    report, _ = vc.baseline_completeness(BASE_ROWS, [w], ["p99_ms"], context_s=600, grace_s=0)
    assert report[0]["metrics"]["p99_ms"] == "fallback"           # heures simulees 6 et 7 absentes


def test_suite_inscrit_le_mode_dans_le_manifeste():
    src = SUITE.read_text(encoding="utf-8")
    assert '"season": season.describe(),' in src and "import season" in src
