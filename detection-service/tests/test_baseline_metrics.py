"""
Coherence entre les metriques calculees par baseline_job.py et celles dont le
modele a besoin (train_model.py, detector.py).

Une metrique demandee par le modele mais absente de baseline_job n'a jamais de
bande saisonniere : sa deviation vaut toujours 0 (features.signed_deviation) et
la feature correspondante est morte sans erreur visible.
"""
import ast
import os

import baseline_job
import train_model

DETECTOR_PATH = os.path.join(os.path.dirname(__file__), "..", "detector.py")


def _detector_ml_metrics():
    """Lit ML_METRICS dans le source de detector.py, sans importer le module."""
    with open(DETECTOR_PATH, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "ML_METRICS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("ML_METRICS introuvable dans detector.py")


def test_baseline_couvre_les_metriques_du_training():
    manquantes = set(train_model.BASELINE_METRICS) - set(baseline_job.METRICS)
    assert not manquantes, f"metriques sans baseline (training) : {sorted(manquantes)}"


def test_baseline_couvre_les_metriques_du_detecteur():
    manquantes = set(_detector_ml_metrics()) - set(baseline_job.METRICS)
    assert not manquantes, f"metriques sans baseline (detecteur) : {sorted(manquantes)}"


def test_metriques_baseline_mappees_sur_colonne_homonyme():
    # Chaque metrique de baseline lit la colonne endpoint_features du meme nom.
    for metric, col in baseline_job.METRICS.items():
        assert metric == col, f"{metric} -> {col}"
