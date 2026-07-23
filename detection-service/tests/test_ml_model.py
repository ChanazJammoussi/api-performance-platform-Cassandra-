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
