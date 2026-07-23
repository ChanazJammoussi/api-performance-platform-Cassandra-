"""Tests de l'heuristique d'alerte precoce TTD (extrapolation Theil-Sen)."""
import pytest

from ttd import estimate_ttd, MIN_SLOPE, MAX_HORIZON_MIN


def test_tendance_haussiere_estime_le_ttd():
    # 100 -> 200 par pas de ~11ms/cycle, SLO 300 -> il reste ~100ms a parcourir.
    rising = [100, 110, 120, 130, 140, 150, 160, 170, 190, 200]
    out = estimate_ttd(rising, 300)
    assert out is not None
    assert not out["already_breaching"]
    assert out["ttd_minutes"] > 0
    assert out["slope_ms_per_min"] > 0


def test_intervalle_coherent():
    out = estimate_ttd([100, 120, 140, 160, 180, 200], 300)
    assert out is not None
    if out["ttd_low"] is not None and out["ttd_high"] is not None:
        # borne basse (pente rapide) <= borne haute (pente lente)
        assert out["ttd_low"] <= out["ttd_high"] + 1e-6


def test_serie_plate_pas_de_ttd():
    assert estimate_ttd([150] * 10, 300) is None


def test_tendance_decroissante_pas_de_ttd():
    assert estimate_ttd([200, 180, 160, 140, 120, 100], 300) is None


def test_deja_au_dessus_du_slo():
    out = estimate_ttd([310, 320, 330], 300)
    assert out is not None
    assert out["already_breaching"] is True
    assert out["ttd_minutes"] == 0.0


def test_trop_peu_de_points():
    assert estimate_ttd([100, 200], 300) is None


def test_slo_absent():
    assert estimate_ttd([100, 150, 200], None) is None


def test_horizon_trop_lointain_rejete():
    # pente minuscule mais > MIN_SLOPE, tres loin du SLO -> au-dela de l'horizon
    slow = [100 + i * (MIN_SLOPE + 0.01) for i in range(10)]
    out = estimate_ttd(slow, 100000)
    assert out is None  # depasse MAX_HORIZON_MIN
