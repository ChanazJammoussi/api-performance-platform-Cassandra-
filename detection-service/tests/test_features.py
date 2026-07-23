"""Tests de la logique de features derivees (math pure, endpoint-relatives)."""
import math

import numpy as np
import pytest

from features import (
    FEATURE_NAMES, WINDOW_SIZE, MIN_WINDOW,
    theil_sen_slope, signed_deviation, direction_of, compute_features, vectorize,
)


# --- theil_sen_slope --------------------------------------------------------

def test_theil_sen_pente_lineaire_constante():
    assert theil_sen_slope([1, 2, 3, 4, 5]) == pytest.approx(1.0)


def test_theil_sen_serie_plate():
    assert theil_sen_slope([5, 5, 5, 5]) == 0.0


def test_theil_sen_pente_negative():
    assert theil_sen_slope([10, 8, 6, 4]) == pytest.approx(-2.0)


def test_theil_sen_robuste_a_un_outlier():
    # Un seul point aberrant ne doit pas dominer la mediane des pentes.
    lin = [1, 2, 3, 4, 5, 6, 7]
    with_outlier = lin.copy()
    with_outlier[3] = 100
    assert theil_sen_slope(with_outlier) == pytest.approx(1.0, abs=0.5)


def test_theil_sen_trop_peu_de_points():
    assert theil_sen_slope([1, 2]) == 0.0  # < MIN_WINDOW
    assert theil_sen_slope([]) == 0.0


def test_theil_sen_ignore_nan_none():
    assert theil_sen_slope([1, None, 2, float("nan"), 3]) == pytest.approx(1.0)


# --- signed_deviation -------------------------------------------------------

def test_signed_deviation_au_dessus_bande_positif():
    # obs=400, bande [90,180] -> (400-180)/(180-90) = 2.444
    assert signed_deviation(400, 90, 130, 180) == pytest.approx(220 / 90)


def test_signed_deviation_sous_bande_negatif():
    assert signed_deviation(50, 90, 130, 180) == pytest.approx((50 - 90) / 90)


def test_signed_deviation_dans_bande_zero():
    assert signed_deviation(130, 90, 130, 180) == 0.0


def test_signed_deviation_valeurs_invalides():
    assert signed_deviation(None, 90, 130, 180) == 0.0
    assert signed_deviation(400, None, None, None) == 0.0
    assert signed_deviation(float("nan"), 90, 130, 180) == 0.0


def test_signed_deviation_bande_degeneree():
    # p10 == p90 -> largeur nulle -> 0 (pas de division par ~0)
    assert signed_deviation(400, 100, 100, 100) == 0.0


# --- direction_of -----------------------------------------------------------

def test_direction_degradation():
    assert direction_of(400, 90, 180) == "degradation"


def test_direction_improvement():
    assert direction_of(50, 90, 180) == "improvement"


def test_direction_normal_dans_bande():
    assert direction_of(130, 90, 180) == "normal"


def test_direction_normal_si_baseline_absente():
    assert direction_of(400, None, None) == "normal"
    assert direction_of(float("nan"), 90, 180) == "normal"


# --- compute_features / vectorize -------------------------------------------

def _window(p50, p95, p99, rps=10.0, err=0.0, n=5):
    """Fenetre plate de n cycles aux valeurs donnees."""
    return [{"p50_ms": p50, "p95_ms": p95, "p99_ms": p99, "rps": rps,
             "error_rate_5xx": err} for _ in range(n)]


def test_compute_features_fenetre_vide():
    feat = compute_features([])
    assert set(feat) == set(FEATURE_NAMES)
    assert all(v == 0.0 for v in feat.values())


def test_compute_features_ratio_p99_p50():
    feat = compute_features(_window(100, 150, 200))
    assert feat["p99_over_p50"] == pytest.approx(2.0)


def test_compute_features_error_rate_passe_tel_quel():
    feat = compute_features(_window(100, 150, 200, err=12.5))
    assert feat["error_rate_5xx"] == pytest.approx(12.5)


def test_compute_features_couvre_tous_les_noms():
    feat = compute_features(_window(100, 150, 200))
    assert set(feat) == set(FEATURE_NAMES)


def test_vectorize_ordre_canonique():
    feat = {n: float(i) for i, n in enumerate(FEATURE_NAMES)}
    vec = vectorize(feat)
    assert isinstance(vec, np.ndarray)
    assert list(vec) == [float(i) for i in range(len(FEATURE_NAMES))]


def test_vectorize_feature_manquante_defaut_zero():
    vec = vectorize({})  # aucune cle -> tout a 0
    assert vec.shape == (len(FEATURE_NAMES),)
    assert np.all(vec == 0.0)


def test_baseline_dev_utilise_dans_features():
    # p99 tres au-dessus de sa bande -> baseline_dev_p99 > 0
    baselines = {"p99_ms": (90, 130, 180), "p50_ms": (40, 50, 60),
                 "p95_ms": (80, 100, 140), "rps": (5, 10, 15)}
    feat = compute_features(_window(50, 120, 400), baselines)
    assert feat["baseline_dev_p99"] > 0
