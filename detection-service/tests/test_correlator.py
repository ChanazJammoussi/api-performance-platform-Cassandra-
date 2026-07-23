"""Tests du scoring de correlation (proximite temporelle + match service)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import correlator
from correlator import (
    _compute_imputation_score, _service_matches, _normalize_endpoint, correlate,
    CAUSAL_WINDOW_MINUTES,
)

UTC = timezone.utc


def _inj(injected, cleared, endpoint="GET /orders/{order_id}", service="orders"):
    return {"injected_at": injected, "cleared_at": cleared,
            "target_endpoint": endpoint, "target_service": service}


def test_score_maximal_dans_la_fenetre_injection():
    inj = _inj(datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
               datetime(2024, 1, 1, 12, 10, tzinfo=UTC))
    onset = datetime(2024, 1, 1, 12, 5, tzinfo=UTC)
    assert _compute_imputation_score(inj, onset) == 1.0


def test_score_decroit_avant_injection():
    injected = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    inj = _inj(injected, injected + timedelta(minutes=10))
    onset = injected - timedelta(minutes=CAUSAL_WINDOW_MINUTES / 2)  # moitie de fenetre
    assert _compute_imputation_score(inj, onset) == pytest.approx(0.5, abs=1e-6)


def test_score_nul_au_dela_de_la_fenetre():
    injected = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    inj = _inj(injected, injected + timedelta(minutes=10))
    onset = injected - timedelta(minutes=CAUSAL_WINDOW_MINUTES + 5)
    assert _compute_imputation_score(inj, onset) == 0.0


def test_service_match_exact():
    inj = _inj(None, None, endpoint="GET /orders/{order_id}", service="orders")
    assert _service_matches(inj, "GET /orders/{order_id}") is True


def test_service_match_par_nom_de_service():
    inj = _inj(None, None, endpoint="GET /orders/{order_id}", service="orders")
    assert _service_matches(inj, "GET /api/orders/1") is True  # 'orders' contenu


def test_service_no_match():
    inj = _inj(None, None, endpoint="GET /orders/{order_id}", service="orders")
    assert _service_matches(inj, "POST /payments") is False


def test_normalize_endpoint():
    assert _normalize_endpoint("  GET /Orders ") == "get /orders"


def test_correlate_integration_tmp(tmp_path, monkeypatch):
    """correlate() lit les ground-truth et retourne le meilleur match."""
    injected = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    cleared = injected + timedelta(minutes=10)
    payload = {"faults": [{
        "scenario_id": "bad_deploy", "fault_type": "latency_step",
        "target_service": "orders", "target_endpoint": "GET /orders/{order_id}",
        "injected_at": injected.isoformat(), "cleared_at": cleared.isoformat(),
        "magnitude": {"latency_ms": 500},
    }]}
    (tmp_path / "gt.json").write_text(json.dumps(payload))
    monkeypatch.setattr(correlator, "RESULTS_DIR", str(tmp_path))

    onset = injected + timedelta(minutes=3)
    result = correlate("GET /orders/{order_id}", onset)
    assert result is not None
    assert result["scenario_id"] == "bad_deploy"
    assert result["fault_type"] == "latency_step"
    assert result["service_match"] is True
    assert result["imputation_score"] == pytest.approx(1.0)


def test_correlate_aucun_match(tmp_path, monkeypatch):
    monkeypatch.setattr(correlator, "RESULTS_DIR", str(tmp_path))  # dossier vide
    onset = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    assert correlate("GET /orders/{order_id}", onset) is None
