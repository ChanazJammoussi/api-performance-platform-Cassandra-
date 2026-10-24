"""
Ground truth des pannes de demonstration (scripts/lib_ground_truth.sh).

On appelle les fonctions bash dans un dossier temporaire, SANS injecter de panne
ni lancer de trafic, puis on verifie que le fichier produit :
  - a exactement les champs des entrees de scenario-runner/runner.py ;
  - est lu par tous les chargeurs reels (exclusions baseline / training,
    evaluation, correlator).
"""
import ast
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import baseline_job
import correlator
import evaluation
import train_model

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "lib_ground_truth.sh"
DEMO = REPO / "scripts" / "demo.sh"
GUIDE = REPO / "scripts" / "demo-guide.md"
RUNNER = REPO / "scenario-runner" / "runner.py"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not LIB.exists(),
    reason="bash ou scripts/lib_ground_truth.sh indisponible",
)


def _runner_fault_keys():
    """Cles des dicts d'entree de faute construits par runner.py (source, sans import)."""
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    keysets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if "injected_at" in keys:
                keysets.append(keys)
    assert keysets, "entree de faute introuvable dans runner.py"
    return keysets[0]


def _bash(script, cwd):
    return subprocess.run(["bash", "-c", f'. "{LIB.as_posix()}"; {script}'],
                          cwd=cwd, capture_output=True, text=True)


def _start(gt_file, provisoire=360):
    return (f'gt_start "{gt_file}" demo latency_step orders "GET /orders/{{order_id}}" '
            f'\'{{"latency_ms": 700}}\' {provisoire}')


def _windows_all_loaders(results_dir, gt_file, monkeypatch):
    monkeypatch.setattr(baseline_job, "RESULTS_DIR", str(results_dir))
    monkeypatch.setattr(train_model, "RESULTS_DIR", str(results_dir))
    monkeypatch.setattr(correlator, "RESULTS_DIR", str(results_dir))
    starts, _ = baseline_job.load_injection_windows()
    return {
        "baseline_job": len(starts),
        "train_model": len(train_model.load_injection_windows()),
        "correlator": len(correlator._load_all_injections()),
        "evaluation": len(evaluation.load_injection_windows(Path(gt_file))),
    }


def test_entree_provisoire_lue_par_tous_les_chargeurs(tmp_path, monkeypatch):
    gt = tmp_path / "demo" / "demo_test.json"
    r = _bash(_start(gt.as_posix()), tmp_path)
    assert r.returncode == 0, r.stderr

    data = json.loads(gt.read_text(encoding="utf-8"))
    entry = data["faults"][0]
    assert set(entry) == _runner_fault_keys()
    assert entry["magnitude"] == {"latency_ms": 700}
    ia = datetime.fromisoformat(entry["injected_at"])
    ca = datetime.fromisoformat(entry["cleared_at"])
    assert ia.tzinfo is not None
    assert 355 <= (ca - ia).total_seconds() <= 365   # fin provisoire = +360 s

    counts = _windows_all_loaders(tmp_path, gt, monkeypatch)
    assert counts == {"baseline_job": 1, "train_model": 1, "correlator": 1, "evaluation": 1}


def test_gt_end_remplace_la_fin_provisoire(tmp_path, monkeypatch):
    gt = tmp_path / "demo" / "demo_test.json"
    r = _bash(f'{_start(gt.as_posix())}; gt_end', tmp_path)
    assert r.returncode == 0, r.stderr

    entry = json.loads(gt.read_text(encoding="utf-8"))["faults"][0]
    ia = datetime.fromisoformat(entry["injected_at"])
    ca = datetime.fromisoformat(entry["cleared_at"])
    assert 0 <= (ca - ia).total_seconds() < 60       # heure reelle, pas la provisoire

    counts = _windows_all_loaders(tmp_path, gt, monkeypatch)
    assert set(counts.values()) == {1}


def test_gt_start_n_ecrase_pas_un_fichier_existant(tmp_path):
    gt = tmp_path / "demo_test.json"
    gt.write_text("contenu existant", encoding="utf-8")
    r = _bash(_start(gt.as_posix()), tmp_path)
    assert r.returncode != 0
    assert gt.read_text(encoding="utf-8") == "contenu existant"


def test_gt_end_sans_gt_start_ne_cree_rien(tmp_path):
    r = _bash("gt_end", tmp_path)
    assert r.returncode == 0
    assert list(tmp_path.iterdir()) == []


def test_demo_sh_enregistre_avant_injection_et_cloture_au_reset():
    src = DEMO.read_text(encoding="utf-8")
    assert "lib_ground_truth.sh" in src
    # gt_start avant l'appel d'injection, gt_end dans cleanup apres le reset.
    assert src.index("gt_start ") < src.index("/faults/latency_step")
    cleanup = src[src.index("cleanup() {"): src.index("trap cleanup EXIT")]
    assert cleanup.index("/faults/reset") < cleanup.index("gt_end")


def test_guide_blocs_ground_truth_conformes_a_runner():
    # Verification statique (PowerShell absent en CI) : les blocs du guide existent,
    # utilisent les champs de runner.py, et encadrent l'injection / le reset.
    guide = GUIDE.read_text(encoding="utf-8")
    debut = guide.index("# --- ground truth : debut de panne")
    fin = guide.index("# --- ground truth : fin de panne")
    bloc_debut = guide[debut: guide.index("```", debut)]
    bloc_fin = guide[fin: guide.index("```", fin)]
    for bloc in (bloc_debut, bloc_fin):
        assert "curl" not in bloc and "/faults/" not in bloc
        assert "WriteAllText" in bloc            # UTF-8 sans BOM (lisible par json.load)
    champs = {c for c in _runner_fault_keys() if f"{c} " in bloc_debut or f"{c}=" in bloc_debut}
    assert champs == _runner_fault_keys()
    assert debut < guide.index("/faults/latency_step", debut)   # avant l'injection
    assert guide.rindex("/faults/reset", 0, fin) < fin          # apres le reset
