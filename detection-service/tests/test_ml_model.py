"""Tests du wrapper Isolation Forest : calibration, KS, sanity-gate, attribution."""
import numpy as np
import pytest

import ml_model
from features import FEATURE_NAMES


# --- calibration ------------------------------------------------------------

def test_calibrate_ancre_p50_a_0_et_p99_a_1():
    calib = {"p50": 0.3, "p99": 0.6}
    assert float(ml_model.calibrate(0.3, calib)) == pytest.approx(0.0)
    assert float(ml_model.calibrate(0.6, calib)) == pytest.approx(1.0)
    assert float(ml_model.calibrate(0.45, calib)) == pytest.approx(0.5)


def test_calibrate_clamp_hors_bornes():
    calib = {"p50": 0.3, "p99": 0.6}
    assert float(ml_model.calibrate(2.0, calib)) == 1.0
    assert float(ml_model.calibrate(-1.0, calib)) == 0.0


def test_calibrate_distribution_degeneree():
    calib = {"p50": 0.5, "p99": 0.5}  # p99 == p50
    assert float(ml_model.calibrate(0.9, calib)) == 0.0


def test_calibration_params_percentiles():
    raw = np.linspace(0, 1, 101)  # 0..1
    params = ml_model.calibration_params(raw)
    assert params["p50"] == pytest.approx(0.5, abs=0.02)
    assert params["p99"] == pytest.approx(0.99, abs=0.02)
    assert params["max"] == pytest.approx(1.0)


# --- statistique KS ---------------------------------------------------------

def test_ks_distributions_identiques():
    a = np.array([1.0, 2.0, 3.0, 4.0])
    assert ml_model._ks_statistic(a, a) == 0.0


def test_ks_distributions_disjointes():
    a = np.array([1.0, 2.0, 3.0])
    b = np.array([10.0, 11.0, 12.0])
    assert ml_model._ks_statistic(a, b) == pytest.approx(1.0)


# --- entrainement / scoring -------------------------------------------------

@pytest.fixture(scope="module")
def trained():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(200, len(FEATURE_NAMES)))
    model = ml_model.train(X, contamination=0.05)
    return model, X


def test_raw_anomaly_forme_et_type(trained):
    model, X = trained
    scores = ml_model.raw_anomaly(model, X[:10])
    assert isinstance(scores, np.ndarray)
    assert scores.shape == (10,)


def test_raw_anomaly_accepte_un_seul_vecteur(trained):
    model, X = trained
    scores = ml_model.raw_anomaly(model, X[0])
    assert scores.shape == (1,)


def test_point_extreme_plus_anormal_que_le_centre(trained):
    model, X = trained
    centre = np.zeros(len(FEATURE_NAMES))
    extreme = np.full(len(FEATURE_NAMES), 50.0)
    assert ml_model.raw_anomaly(model, extreme)[0] > ml_model.raw_anomaly(model, centre)[0]


# --- sanity gate ------------------------------------------------------------

def test_sanity_gate_premier_modele(trained):
    model, X = trained
    ok, report = ml_model.sanity_gate(model, None, X[:50])
    assert ok is True
    assert "premier" in report["reason"].lower()


def test_sanity_gate_meme_modele_ks_nul(trained):
    model, X = trained
    ok, report = ml_model.sanity_gate(model, {"model": model}, X[:50])
    assert ok is True
    assert report["ks"] == pytest.approx(0.0, abs=1e-9)


# --- attribution ------------------------------------------------------------

def test_attribute_renvoie_top_k(trained):
    model, X = trained
    medians = np.median(X, axis=0)
    calib = ml_model.calibration_params(ml_model.raw_anomaly(model, X))
    x = np.full(len(FEATURE_NAMES), 10.0)
    top = ml_model.attribute(model, x, medians, calib, top_k=3)
    assert len(top) == 3
    assert all(name in FEATURE_NAMES for name, _ in top)
    # trie par |contribution| decroissante
    vals = [abs(v) for _, v in top]
    assert vals == sorted(vals, reverse=True)


# --- metadonnees ------------------------------------------------------------

def test_build_meta_contient_feature_names(trained):
    model, X = trained
    meta = ml_model.build_meta("2026-01-01T00:00:00+00:00", X, 0.05, 10, 14)
    assert meta["feature_names"] == FEATURE_NAMES
    assert meta["n_samples"] == len(X)
    assert len(meta["feature_medians"]) == len(FEATURE_NAMES)


# --- compatibilite d'un bundle (R5) -----------------------------------------

def _bundle(model, X):
    meta = ml_model.build_meta("2026-01-01T00:00:00+00:00", X, 0.05, 10, 14)
    meta["calibration"] = ml_model.calibration_params(ml_model.raw_anomaly(model, X))
    return {"model": model, "meta": meta}


def test_compat_bundle_conforme(trained):
    assert ml_model.check_bundle_compat(_bundle(*trained)) == []


def test_compat_ordre_des_features_different(trained):
    b = _bundle(*trained)
    b["meta"]["feature_names"] = list(reversed(FEATURE_NAMES))
    assert any("ordre" in p for p in ml_model.check_bundle_compat(b))


def test_compat_noms_et_nombre_differents(trained):
    b = _bundle(*trained)
    b["meta"]["feature_names"] = FEATURE_NAMES[:-1] + ["inconnue"]
    assert ml_model.check_bundle_compat(b)
    b["meta"]["feature_names"] = FEATURE_NAMES[:-1]
    assert ml_model.check_bundle_compat(b)


def test_compat_n_features_in_different():
    X6 = np.random.default_rng(1).normal(size=(100, len(FEATURE_NAMES) - 1))
    model6 = ml_model.train(X6, contamination=0.05)
    X = np.random.default_rng(1).normal(size=(100, len(FEATURE_NAMES)))
    b = _bundle(ml_model.train(X, contamination=0.05), X)
    b["model"] = model6
    assert any("n_features_in_" in p for p in ml_model.check_bundle_compat(b))


@pytest.mark.parametrize("calib", [None, {}, {"p50": 0.3}, {"p50": 0.3, "p99": float("nan")}])
def test_compat_calibration_invalide(trained, calib):
    b = _bundle(*trained)
    if calib is None:
        del b["meta"]["calibration"]
    else:
        b["meta"]["calibration"] = calib
    assert any("calibration" in p for p in ml_model.check_bundle_compat(b))


def test_compat_medianes_invalides(trained):
    b = _bundle(*trained)
    b["meta"]["feature_medians"] = b["meta"]["feature_medians"][:-1]
    assert any("feature_medians" in p for p in ml_model.check_bundle_compat(b))
    b["meta"]["feature_medians"] = [float("inf")] * len(FEATURE_NAMES)
    assert any("feature_medians" in p for p in ml_model.check_bundle_compat(b))


@pytest.mark.parametrize("bundle", [None, [], {"model": object()}, {"meta": {}}])
def test_compat_structure_invalide(bundle):
    assert ml_model.check_bundle_compat(bundle)


# --- etat du depot de modeles (R1) : diagnostic sans ecriture ---------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "models"
    d.mkdir()
    monkeypatch.setattr(ml_model, "MODEL_DIR", str(d))
    return d


def _listing(d):
    return sorted(p.name for p in d.iterdir()) if d.exists() else None


def test_etat_dossier_absent_et_non_cree(tmp_path, monkeypatch):
    d = tmp_path / "inexistant"
    monkeypatch.setattr(ml_model, "MODEL_DIR", str(d))
    assert ml_model.model_store_state()[0] == "dossier_absent"
    assert not d.exists()


def test_etat_absent_si_dossier_vide(store):
    state, bundle, _ = ml_model.model_store_state()
    assert (state, bundle) == ("absent", None)


def test_etat_incoherent_artefacts_sans_latest(store):
    (store / "iforest_20260101_000000.joblib").write_bytes(b"x")
    assert ml_model.model_store_state()[0] == "incoherent"


def test_etat_incoherent_reference_sans_modele(store):
    np.save(store / ml_model.REFERENCE_NAME, np.zeros((5, len(FEATURE_NAMES))))
    assert ml_model.model_store_state()[0] == "incoherent"


def test_etat_illisible_latest_corrompu(store):
    (store / ml_model.LATEST_NAME).write_bytes(b"ceci n'est pas un joblib")
    assert ml_model.model_store_state()[0] == "illisible"


def test_etat_illisible_latest_pas_un_bundle(store):
    import joblib
    joblib.dump([1, 2, 3], store / ml_model.LATEST_NAME)
    assert ml_model.model_store_state()[0] == "illisible"


def test_etat_incompatible(store, trained):
    import joblib
    b = _bundle(*trained)
    b["meta"]["feature_names"] = list(reversed(FEATURE_NAMES))
    joblib.dump(b, store / ml_model.LATEST_NAME)
    assert ml_model.model_store_state()[0] == "incompatible"


def test_etat_ok_et_diagnostic_sans_ecriture(store, trained):
    import joblib
    joblib.dump(_bundle(*trained), store / ml_model.LATEST_NAME)
    avant = _listing(store)
    state, bundle, _ = ml_model.model_store_state()
    assert state == "ok" and bundle is not None
    assert _listing(store) == avant


def test_etat_erreur_si_lecture_du_dossier_echoue(store, monkeypatch):
    def refus(_):
        raise PermissionError("acces refuse")
    monkeypatch.setattr(ml_model.os, "listdir", refus)
    assert ml_model.model_store_state()[0] == "erreur"


# --- artefact precedent et fenetre de reference -----------------------------

def test_precedent_aucun(store):
    assert ml_model.load_previous_versioned_state()[0] == "aucun"


def test_precedent_illisible(store):
    (store / "iforest_20260101_000000.joblib").write_bytes(b"corrompu")
    assert ml_model.load_previous_versioned_state()[0] == "illisible"


def test_precedent_ok(store, trained):
    import joblib
    joblib.dump(_bundle(*trained), store / "iforest_20260101_000000.joblib")
    state, bundle, _ = ml_model.load_previous_versioned_state()
    assert state == "ok" and "model" in bundle


def test_reference_corrompue_erreur_explicite(store, trained):
    (store / ml_model.REFERENCE_NAME).write_bytes(b"corrompu")
    with pytest.raises(ml_model.ReferenceWindowError):
        ml_model.ensure_reference(trained[1])


def test_reference_mauvaise_forme_erreur_explicite(store, trained):
    np.save(store / ml_model.REFERENCE_NAME, np.zeros((5, len(FEATURE_NAMES) + 1)))
    with pytest.raises(ml_model.ReferenceWindowError):
        ml_model.ensure_reference(trained[1])


# --- sanity gate strict (R9 partiel) ----------------------------------------

def test_sanity_gate_strict_refuse_sans_reference(trained):
    model, X = trained
    ok, report = ml_model.sanity_gate(model, None, X[:50], strict=True)
    assert ok is False


def test_sanity_gate_strict_refuse_fenetre_vide(trained):
    model, X = trained
    ok, _ = ml_model.sanity_gate(model, {"model": model}, X[:0], strict=True)
    assert ok is False


def test_sanity_gate_strict_compare_normalement(trained):
    model, X = trained
    ok, report = ml_model.sanity_gate(model, {"model": model}, X[:50], strict=True)
    assert ok is True and report["ks"] == pytest.approx(0.0, abs=1e-9)
