"""Tests des utilitaires de baseline (convention dow + deviation unilaterale)."""
from datetime import datetime

import pytest

from baseline_utils import pg_dow, compute_deviation


def test_pg_dow_convention_postgres():
    # Postgres EXTRACT(DOW) : dimanche=0 .. samedi=6.
    # 2024-01-01 = lundi (weekday 0) -> pg_dow 1 ; 2024-01-07 = dimanche -> pg_dow 0.
    assert pg_dow(datetime(2024, 1, 1)) == 1   # lundi
    assert pg_dow(datetime(2024, 1, 7)) == 0   # dimanche
    assert pg_dow(datetime(2024, 1, 6)) == 6   # samedi


def test_pg_dow_toujours_dans_0_6():
    for day in range(1, 8):
        assert 0 <= pg_dow(datetime(2024, 1, day)) <= 6


def test_compute_deviation_au_dessus_p90():
    # (observed - p90) / (p90 - p10)
    assert compute_deviation(400, 90, 180) == pytest.approx((400 - 180) / (180 - 90 + 1e-6))


def test_compute_deviation_sous_ou_egal_p90_est_zero():
    assert compute_deviation(180, 90, 180) == 0.0
    assert compute_deviation(120, 90, 180) == 0.0


def test_compute_deviation_baseline_absente():
    assert compute_deviation(400, None, 180) == 0.0
    assert compute_deviation(400, 90, None) == 0.0


def test_compute_deviation_unilaterale():
    # Contrairement a signed_deviation, cette version ignore le cote bas.
    assert compute_deviation(10, 90, 180) == 0.0
