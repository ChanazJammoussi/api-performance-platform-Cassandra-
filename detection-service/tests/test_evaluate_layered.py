"""Tests de la logique d'evaluation offline (hysteresis, matching, magnitude)."""
from datetime import datetime, timedelta, timezone

import pytest

import evaluate_layered as ev
from evaluation import InjectionWindow

UTC = timezone.utc


# --- replay (hysteresis M cycles consecutifs) -------------------------------

def test_replay_fire_apres_m_cycles_consecutifs():
    pts = [(1, False), (2, True), (3, True), (4, True), (5, False), (6, True), (7, True)]
    onsets = ev.replay(pts)
    # M_WINDOWS=2 -> fire au 2e True consecutif ; nouvel episode apres un False.
    assert onsets == [3, 7]


def test_replay_un_seul_true_ne_fire_pas():
    pts = [(1, True), (2, False), (3, True), (4, False)]
    assert ev.replay(pts) == []


def test_replay_serie_vide():
    assert ev.replay([]) == []


# --- match_window -----------------------------------------------------------

def _window(injected, cleared, endpoint="GET /orders/{order_id}", src="latency_step_core.json"):
    return InjectionWindow(
        scenario_id="s", fault_type="latency_step", target_endpoint=endpoint,
        injected_at=injected, cleared_at=cleared, magnitude={}, source_file=src,
    )


def test_match_window_onset_dans_la_fenetre():
    injected = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    w = _window(injected, injected + timedelta(minutes=5))
    onset = injected + timedelta(seconds=90)
    delay = ev.match_window([onset], w)
    assert delay == pytest.approx(90.0)


def test_match_window_dans_la_grace():
    injected = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    cleared = injected + timedelta(minutes=5)
    w = _window(injected, cleared)
    onset = cleared + timedelta(seconds=60)  # < GRACE (120s)
    assert ev.match_window([onset], w) is not None


def test_match_window_hors_fenetre():
    injected = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    w = _window(injected, injected + timedelta(minutes=5))
    onset = injected + timedelta(minutes=30)  # bien apres cleared + grace
    assert ev.match_window([onset], w) is None


def test_match_window_prend_le_premier_onset():
    injected = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    w = _window(injected, injected + timedelta(minutes=5))
    o1 = injected + timedelta(seconds=60)
    o2 = injected + timedelta(seconds=200)
    assert ev.match_window([o2, o1], w) == pytest.approx(60.0)


# --- magnitude_of -----------------------------------------------------------

def test_magnitude_stress():
    w = _window(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC),
                src="latency_step_large_stress_20260706.json")
    assert ev.magnitude_of(w) == "stress"


def test_magnitude_core():
    w = _window(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC),
                src="latency_step_large_core_20260706.json")
    assert ev.magnitude_of(w) == "core"


def test_magnitude_par_defaut_all():
    w = _window(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC),
                src="bad_deploy.json")
    assert ev.magnitude_of(w) == "all"
