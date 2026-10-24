"""
Validation avant promotion d'un modele (train_model.validate_promotion) et choix
du jeu d'evaluation par defaut (evaluate_layered.latest_campaign_input).

Statuts : initialisation (1er modele, promu mais NON valide), ok, regression,
impossible. Une validation impossible n'est jamais une reussite.
Aucune base reelle : les acces DB sont remplaces par des fonctions de test.
"""
from datetime import datetime, timedelta, timezone

import pytest

import evaluate_layered as ev
import ml_model
import train_model as tm
from evaluation import InjectionWindow

T0 = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
CURRENT = {"model": "courant", "meta": {}}
CANDIDATE = {"model": "candidat", "meta": {}}


def _mkdirs(root, *rels):
    for rel in rels:
        (root / rel).mkdir(parents=True)


# --- choix de la campagne d'evaluation --------------------------------------

def test_campagne_la_plus_recente_hors_archive(tmp_path, monkeypatch):
    monkeypatch.delenv("EVAL_INPUT", raising=False)
    _mkdirs(tmp_path,
            "campaign_20261101_0900/ground_truth",
            "campaign_20261103_1400/ground_truth",
            "campaign_20261105_0800",                    # sans ground_truth : ignoree
            "archive/campaign_20991231_2359/ground_truth")  # archive : ignoree
    choisi = ev.latest_campaign_input(tmp_path)
    assert choisi == str(tmp_path / "campaign_20261103_1400" / "ground_truth")


def test_eval_input_prioritaire(tmp_path, monkeypatch):
    _mkdirs(tmp_path, "campaign_20261103_1400/ground_truth")
    monkeypatch.setenv("EVAL_INPUT", "/chemin/explicite")
    assert ev.latest_campaign_input(tmp_path) == "/chemin/explicite"


def test_aucune_campagne(tmp_path, monkeypatch):
    monkeypatch.delenv("EVAL_INPUT", raising=False)
    _mkdirs(tmp_path, "archive/campaign_20260706_1109/ground_truth")
    assert ev.latest_campaign_input(tmp_path) is None


# --- statuts de validation --------------------------------------------------

def _window(start=T0, minutes=5):
    return InjectionWindow(scenario_id="s", fault_type="latency_step",
                           target_endpoint="GET /orders/{order_id}",
                           injected_at=start, cleared_at=start + timedelta(minutes=minutes),
                           magnitude={}, source_file="gt.json")


def _patch(monkeypatch, *, current=CURRENT, campaign="/gt/campaign/ground_truth",
           windows=None, features=None, recall_fp=None, state=None):
    if state is None:
        state = "absent" if current is None else "ok"
    monkeypatch.setattr(ml_model, "model_store_state", lambda: (state, current, f"etat de test {state}"))
    # Bundles factices : la compatibilite reelle est testee a part (test_ml_model,
    # test_candidat_incompatible_validation_impossible).
    monkeypatch.setattr(ml_model, "check_bundle_compat", lambda b: [])
    monkeypatch.setattr(ev, "latest_campaign_input", lambda *a, **k: campaign)
    monkeypatch.setattr(ev, "load_windows", lambda p: windows if windows is not None else [_window()])
    monkeypatch.setattr(ev, "load_features_span", lambda conn, s, u: features if features is not None else
                        {"GET /orders/{order_id}": [{"time": T0 + timedelta(minutes=2)}]})
    monkeypatch.setattr(tm, "load_baseline_lookup", lambda conn: ({}, {}))
    if recall_fp is not None:
        monkeypatch.setattr(tm, "_recall_fp",
                            lambda ev_, w, by_ep, ex, fb, bundle: recall_fp[bundle["model"]])


def test_premier_modele_initialisation_non_validee(monkeypatch):
    _patch(monkeypatch, current=None)
    statut, rapport = tm.validate_promotion(None, CANDIDATE)
    assert statut == "initialisation"
    assert "non validee" in rapport.lower()


@pytest.mark.parametrize("state", ["incoherent", "illisible", "incompatible", "dossier_absent", "erreur"])
def test_etat_douteux_du_modele_validation_impossible(monkeypatch, state):
    _patch(monkeypatch, current=None, state=state)
    statut, rapport = tm.validate_promotion(None, CANDIDATE)
    assert statut == "impossible"
    assert state in rapport


def test_candidat_incompatible_validation_impossible(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(ml_model, "check_bundle_compat", lambda b: ["ordre different"])
    statut, rapport = tm.validate_promotion(None, CANDIDATE)
    assert statut == "impossible"
    assert "incompatible" in rapport


def test_diagnostic_transmis_n_est_pas_recalcule(monkeypatch):
    # Le diagnostic pris avant les ecritures du cycle fait foi.
    _patch(monkeypatch, state="incoherent", current=None)
    statut, _ = tm.validate_promotion(None, CANDIDATE, store=("absent", None, "avant ecriture"))
    assert statut == "initialisation"


def test_aucune_campagne_validation_impossible(monkeypatch):
    _patch(monkeypatch, campaign=None)
    assert tm.validate_promotion(None, CANDIDATE)[0] == "impossible"


def test_campagne_sans_fenetre_validation_impossible(monkeypatch):
    _patch(monkeypatch, windows=[])
    assert tm.validate_promotion(None, CANDIDATE)[0] == "impossible"


def test_campagne_sans_mesure_validation_impossible(monkeypatch):
    _patch(monkeypatch, features={})
    assert tm.validate_promotion(None, CANDIDATE)[0] == "impossible"


def test_mesures_hors_des_fenetres_validation_impossible(monkeypatch):
    # Des mesures existent sur la periode chargee, mais aucune dans une fenetre.
    _patch(monkeypatch, features={"GET /orders/{order_id}": [{"time": T0 - timedelta(minutes=8)}]})
    assert tm.validate_promotion(None, CANDIDATE)[0] == "impossible"


def test_validation_ok(monkeypatch):
    _patch(monkeypatch, recall_fp={"candidat": (0.80, 3), "courant": (0.80, 3)})
    assert tm.validate_promotion(None, CANDIDATE)[0] == "ok"


def test_validation_regression_recall(monkeypatch):
    _patch(monkeypatch, recall_fp={"candidat": (0.60, 3), "courant": (0.80, 3)})
    assert tm.validate_promotion(None, CANDIDATE)[0] == "regression"


def test_validation_regression_faux_positifs(monkeypatch):
    _patch(monkeypatch, recall_fp={"candidat": (0.80, 20), "courant": (0.80, 3)})
    assert tm.validate_promotion(None, CANDIDATE)[0] == "regression"


def test_erreur_de_validation_est_impossible(monkeypatch):
    def boom(conn, candidate, store=None):
        raise FileNotFoundError("campagne introuvable")
    monkeypatch.setattr(tm, "validate_promotion", boom)
    statut, rapport = tm.run_validation(None, CANDIDATE)
    assert statut == "impossible"
    assert "campagne introuvable" in rapport


# --- decision de promotion --------------------------------------------------

@pytest.mark.parametrize("statut,attendu", [
    ("initialisation", True),
    ("ok", True),
    ("regression", False),
    ("impossible", False),
])
def test_decision_selon_statut(statut, attendu):
    promote, raisons = tm.decide_promotion(ok_gate=True, val_status=statut, fresh=True, force=False)
    assert promote is attendu
    assert (raisons == []) is attendu


def test_decision_refuse_si_gate_ou_fraicheur():
    assert tm.decide_promotion(False, "ok", True, False)[0] is False
    assert tm.decide_promotion(True, "ok", False, False)[0] is False


def test_force_explicite_seulement():
    # --force (manuel) passe outre ; sans lui, une validation impossible bloque.
    assert tm.decide_promotion(True, "impossible", True, force=True)[0] is True
    assert tm.decide_promotion(True, "impossible", True, force=False)[0] is False


def test_force_desactive_par_defaut():
    # --force n'est actif que s'il est passe explicitement (store_true, sans default=True).
    import ast
    import inspect
    calls = [n for n in ast.walk(ast.parse(inspect.getsource(tm.main)))
             if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_argument"
             and n.args and getattr(n.args[0], "value", None) == "--force"]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert kw["action"].value == "store_true"
    assert "default" not in kw


# --- cycle complet train_and_maybe_promote (base simulee, dossier temporaire) --

import argparse  # noqa: E402
import joblib    # noqa: E402
import numpy as np  # noqa: E402
from features import FEATURE_NAMES  # noqa: E402


@pytest.fixture
def cycle(tmp_path, monkeypatch):
    """Remplace tous les acces DB ; MODEL_DIR = dossier temporaire."""
    store = tmp_path / "models"
    store.mkdir()
    monkeypatch.setattr(ml_model, "MODEL_DIR", str(store))
    X = np.random.default_rng(3).normal(size=(300, len(FEATURE_NAMES)))
    monkeypatch.setattr(tm, "load_feature_rows", lambda conn, days: {"GET /orders": [{}]})
    monkeypatch.setattr(tm, "load_baseline_lookup", lambda conn: ({}, {}))
    monkeypatch.setattr(tm, "load_injection_windows", lambda: [])
    monkeypatch.setattr(tm, "build_matrix", lambda *a: (X, 0))
    monkeypatch.setattr(tm, "data_freshness_hours", lambda conn: 1.0)
    return store, X


def _args(force=False):
    return argparse.Namespace(lookback_days=14, contamination=0.02, dry_run=False, force=force)


def _valid_bundle(X):
    model = ml_model.train(X, contamination=0.02)
    meta = ml_model.build_meta("2026-01-01T00:00:00+00:00", X, 0.02, 10, 14)
    meta["calibration"] = ml_model.calibration_params(ml_model.raw_anomaly(model, X))
    return {"model": model, "meta": meta}


def _snapshot(d):
    return {p.name: p.read_bytes() for p in d.iterdir()} if d.exists() else None


def test_cycle_premier_modele_initialisation(cycle):
    store, _ = cycle
    assert tm.train_and_maybe_promote(None, _args()) is True
    latest = joblib.load(store / ml_model.LATEST_NAME)
    assert latest["meta"]["validation"]["status"] == "initialisation"
    assert (store / ml_model.REFERENCE_NAME).exists()


@pytest.mark.parametrize("force", [False, True])
def test_cycle_latest_corrompu_aucune_ecriture(cycle, force):
    store, _ = cycle
    (store / ml_model.LATEST_NAME).write_bytes(b"corrompu")
    avant = _snapshot(store)
    assert tm.train_and_maybe_promote(None, _args(force)) is False
    assert _snapshot(store) == avant


@pytest.mark.parametrize("force", [False, True])
def test_cycle_dossier_absent_rien_cree(cycle, monkeypatch, force):
    store, _ = cycle
    absent = store.parent / "inexistant"
    monkeypatch.setattr(ml_model, "MODEL_DIR", str(absent))
    assert tm.train_and_maybe_promote(None, _args(force)) is False
    assert not absent.exists()


@pytest.mark.parametrize("force", [False, True])
def test_cycle_etat_incoherent_aucune_ecriture(cycle, force):
    store, _ = cycle
    np.save(store / ml_model.REFERENCE_NAME, np.zeros((5, len(FEATURE_NAMES))))
    avant = _snapshot(store)
    assert tm.train_and_maybe_promote(None, _args(force)) is False
    assert _snapshot(store) == avant


@pytest.mark.parametrize("force", [False, True])
def test_cycle_candidat_incompatible_aucune_ecriture(cycle, monkeypatch, force):
    store, _ = cycle
    real_build_meta = ml_model.build_meta

    def meta_reordonnee(*a, **k):
        meta = real_build_meta(*a, **k)
        meta["feature_names"] = list(reversed(FEATURE_NAMES))
        return meta
    monkeypatch.setattr(ml_model, "build_meta", meta_reordonnee)
    assert tm.train_and_maybe_promote(None, _args(force)) is False
    assert _snapshot(store) == {}


def _modele_en_service(store, X):
    b = _valid_bundle(X)
    joblib.dump(b, store / ml_model.LATEST_NAME)
    joblib.dump(b, store / "iforest_20260101_000000.joblib")
    np.save(store / ml_model.REFERENCE_NAME, X[:50])


@pytest.mark.parametrize("defaut", ["reference_absente", "reference_corrompue", "precedent_illisible"])
def test_cycle_reference_douteuse_aucune_ecriture(cycle, defaut):
    store, X = cycle
    _modele_en_service(store, X)
    if defaut == "reference_absente":
        (store / ml_model.REFERENCE_NAME).unlink()
    elif defaut == "reference_corrompue":
        (store / ml_model.REFERENCE_NAME).write_bytes(b"corrompu")
    else:
        (store / "iforest_20260101_000000.joblib").write_bytes(b"corrompu")
    avant = _snapshot(store)
    assert tm.train_and_maybe_promote(None, _args(force=True)) is False
    assert _snapshot(store) == avant


def test_cycle_validation_impossible_latest_inchange(cycle, monkeypatch):
    store, X = cycle
    _modele_en_service(store, X)
    latest_avant = (store / ml_model.LATEST_NAME).read_bytes()
    monkeypatch.setattr(tm, "run_validation", lambda conn, c, store=None: ("impossible", "pas de campagne (test)"))
    assert tm.train_and_maybe_promote(None, _args()) is True
    assert (store / ml_model.LATEST_NAME).read_bytes() == latest_avant   # pas de promotion
    nouveaux = set(_snapshot(store)) - {ml_model.LATEST_NAME, ml_model.REFERENCE_NAME,
                                         "iforest_20260101_000000.joblib"}
    assert len(nouveaux) == 1   # candidat conserve, NON promu (comportement existant)
