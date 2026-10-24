"""Identite et tracabilite du modele charge par le detecteur (lot 2b-5). Artefacts synthetiques."""
import os
from pathlib import Path

import joblib
import numpy as np

import ml_model
import model_identity as mi
import prom_metrics as pm
from features import FEATURE_NAMES

DETECTOR = Path(__file__).resolve().parents[1] / "detector.py"


def _bundle(seed=0, names=None):
    X = np.random.default_rng(seed).normal(size=(200, len(FEATURE_NAMES)))
    model = ml_model.train(X, contamination=0.02)
    meta = ml_model.build_meta(f"2026-10-0{seed + 1}T00:00:00+00:00", X, 0.02, 10, 14)
    meta["calibration"] = ml_model.calibration_params(ml_model.raw_anomaly(model, X))
    if names is not None:
        meta["feature_names"] = names
    return {"model": model, "meta": meta}


def _write(path, bundle, mtime):
    joblib.dump(bundle, path)
    os.utime(path, (mtime, mtime))


def _tracker(tmp_path):
    p = tmp_path / "iforest_latest.joblib"
    return p, mi.ModelTracker(path_fn=lambda: str(p))


def test_identite_reproductible_et_sensible_au_contenu(tmp_path):
    p = tmp_path / "a.joblib"
    _write(p, _bundle(0), 1000)
    a, b = mi.load_artifact(p), mi.load_artifact(p)
    assert a["statut"] == "charge" and a["sha256"] == b["sha256"]
    _write(p, _bundle(1), 1000)
    assert mi.load_artifact(p)["sha256"] != a["sha256"]


def test_chargement_reussi_expose_identite_sans_chemin(tmp_path):
    p, t = _tracker(tmp_path)
    _write(p, _bundle(0), 1000)
    assert t.refresh()["resultat"] == "charge"
    ident = t.identity()
    assert ident["statut"] == "actif" and ident["sha256"] == mi.artifact_sha256(p.read_bytes())
    assert str(tmp_path) not in repr(ident)


def test_modele_invalide_refuse_sans_activation(tmp_path):
    p, t = _tracker(tmp_path)
    p.write_bytes(b"pas un modele")
    assert t.refresh()["resultat"] == "illisible"
    assert t.identity()["statut"] == "aucun_modele_actif"
    assert t.current().bundle is None


def test_echec_d_un_nouvel_artefact_garde_l_ancien_actif(tmp_path):
    p, t = _tracker(tmp_path)
    _write(p, _bundle(0), 1000)
    t.refresh()
    old = t.identity()["sha256"]
    _write(p, _bundle(1, names=list(reversed(FEATURE_NAMES))), 2000)   # incompatible
    assert t.refresh()["resultat"] == "incompatible"
    ident = t.identity()
    assert ident["statut"] == "actif" and ident["sha256"] == old
    assert ident["dernier_chargement_refuse"]["resultat"] == "incompatible"
    assert ident["dernier_chargement_refuse"]["sha256"] != old


def test_rechargement_met_a_jour_l_identite(tmp_path):
    p, t = _tracker(tmp_path)
    _write(p, _bundle(0), 1000)
    t.refresh()
    first = t.identity()["sha256"]
    assert t.refresh() is None                       # inchange : pas de rechargement
    _write(p, _bundle(1), 2000)
    assert t.refresh()["resultat"] == "charge"
    assert t.identity()["sha256"] != first and "dernier_chargement_refuse" not in t.identity()


def test_artefact_disparu_ne_desactive_pas_le_modele_charge(tmp_path):
    p, t = _tracker(tmp_path)
    _write(p, _bundle(0), 1000)
    t.refresh()
    p.unlink()
    assert t.refresh()["resultat"] == "absent"
    assert t.identity()["statut"] == "actif"


def test_identite_associee_a_la_detection(tmp_path):
    p, t = _tracker(tmp_path)
    _write(p, _bundle(0), 1000)
    t.refresh()
    active = t.current()
    used = mi.detection_identity(active, ml_used=True)
    assert used["utilise_pour_ce_point"] is True and used["sha256"] == t.identity()["sha256"]
    assert mi.detection_identity(active, ml_used=False)["utilise_pour_ce_point"] is False
    none = mi.detection_identity(mi.ModelTracker(path_fn=lambda: "x").current(), ml_used=True)
    assert none == {"statut": "aucun_modele_actif", "utilise_pour_ce_point": False}


def test_identite_historique_absente_jamais_deduite_du_disque(tmp_path, monkeypatch):
    def interdit(*a, **k):
        raise AssertionError("le disque ne doit pas etre consulte")
    monkeypatch.setattr(mi, "load_artifact", interdit)
    for cf in (None, {}, {"combined": 0.4}, {"model": "x"}):
        assert mi.identity_from_contributing(cf)["statut"] == "inconnue"
    rec = {"model": {"statut": "actif", "sha256": "ab"}}
    assert mi.identity_from_contributing(rec) == rec["model"]


def test_publication_prometheus_etats(tmp_path):
    p, t = _tracker(tmp_path)
    pm.publish_model(t.identity(), None)
    assert pm.model_state_label(t.identity()) == "aucun_modele_actif"
    p.write_bytes(b"x")
    t.refresh()
    assert pm.model_state_label(t.identity()) == "aucun_modele_dernier_chargement_refuse"
    _write(p, _bundle(0), 1000)
    t.refresh()
    pm.publish_model(t.identity(), "charge")
    assert pm.model_state_label(t.identity()) == "actif"


def test_cablage_du_detecteur():
    src = DETECTOR.read_text(encoding="utf-8")
    assert "_MODEL = ModelTracker()" in src and "_ML[" not in src
    assert "active_model = _MODEL.current()" in src
    assert "bundle = active_model.bundle" in src
    assert '"model": detection_identity(active_model, ml_used)' in src
