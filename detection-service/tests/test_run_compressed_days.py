"""
scripts/run_compressed_days.sh : syntaxe et planning des journees (--dry-run avec
une heure simulee FAKE_NOW). Aucun k6, aucun docker n'est lance : le mode
--dry-run sort avant toute commande docker.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "run_compressed_days.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or not SCRIPT.exists(),
                                reason="bash ou scripts/run_compressed_days.sh indisponible")

T0 = 1792886400            # 2026-10-25T00:00:00+00:00 (heure paire UTC)


def run(*args, fake_now=None, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("K6_", "JOURNAL", "STOP_FLAG", "PID_FILE"))}
    if fake_now is not None:
        env["FAKE_NOW"] = str(fake_now)
    env.update(extra_env or {})
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=30)


def plan(n, fake_now):
    p = run(str(n), "--dry-run", fake_now=fake_now)
    assert p.returncode == 0, p.stderr
    return [line.split("\t")[1] for line in p.stdout.splitlines()]


def test_t0_est_une_heure_paire():
    assert T0 % 7200 == 0


def test_syntaxe_bash():
    p = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_fins_de_ligne_lf():
    assert b"\r" not in SCRIPT.read_bytes()


def test_planning_aligne_sur_les_heures_paires():
    assert plan(3, T0 - 600) == ["2026-10-25T00:00:00+00:00", "2026-10-25T02:00:00+00:00",
                                 "2026-10-25T04:00:00+00:00"]


def test_leger_retard_tolere_meme_heure_paire():
    assert plan(1, T0 + 30) == ["2026-10-25T00:00:00+00:00"]


def test_retard_trop_grand_heure_paire_suivante():
    assert plan(1, T0 + 90) == ["2026-10-25T02:00:00+00:00"]


def test_sept_journees_14_heures():
    p = plan(7, T0 - 1)
    assert len(p) == 7 and p[-1] == "2026-10-25T12:00:00+00:00"


@pytest.mark.parametrize("args", [(), ("0",), ("abc",), ("3", "--autre")])
def test_arguments_invalides(args):
    assert run(*args).returncode == 2


def test_load_js_inchange_et_sans_telechargement():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "/scripts/load.js" in src and ':/scripts:ro' in src and "--pull=never" in src
    assert "load_light" not in src


def test_reseau_par_defaut_du_projet_cassandra_fresh():
    src = SCRIPT.read_text(encoding="utf-8")
    assert 'NET="${K6_NETWORK:-cassandra_fresh_default}"' in src
    assert "cassandra_compresse" not in src


# --- mode hote (k6.exe sous Windows) : uniquement --dry-run et refus avant ecriture ---

DOCKER_LINES = (
    '  MSYS_NO_PATHCONV=1 docker run --rm --pull=never --name "$NAME" --network "$NET" \\\n'
    '    -e BASE_URL="$BASE_URL" -v "$K6_DIR":/scripts:ro \\\n'
    '    "$IMAGE" run --quiet /scripts/load.js\n'
    '  code=$?\n'
)


def dry(extra_env=None):
    return run("2", "--dry-run", fake_now=T0 - 600, extra_env=extra_env)


def test_mode_hote_par_defaut():
    p = dry()
    assert p.returncode == 0, p.stderr
    assert "# mode=hote base_url=http://localhost:8000" in p.stderr
    assert 'K6_RUNNER="${K6_RUNNER:-hote}"' in SCRIPT.read_text(encoding="utf-8")


def test_mode_docker_explicite_et_commande_docker_inchangee():
    p = dry({"K6_RUNNER": "docker"})
    assert p.returncode == 0, p.stderr
    assert "# mode=docker base_url=http://gateway:8000" in p.stderr
    assert DOCKER_LINES in SCRIPT.read_text(encoding="utf-8")


def test_mode_hote_meme_planning_url_locale_sans_statistiques():
    d, h = dry({"K6_RUNNER": "docker"}), dry({"K6_RUNNER": "hote", "K6_BIN": "/c/Users/chana/tools/k6/k6.exe"})
    assert h.returncode == 0, h.stderr
    assert h.stdout == d.stdout                                   # memes heures paires
    assert "# mode=hote base_url=http://localhost:8000" in h.stderr
    assert "/c/Users/chana/tools/k6/k6.exe run --quiet --no-usage-report" in h.stderr
    assert h.stderr.rstrip().endswith("k6/load.js")


def test_mode_hote_url_personnalisee():
    p = dry({"K6_RUNNER": "hote", "K6_BASE_URL": "http://127.0.0.1:8000"})
    assert p.returncode == 0 and "base_url=http://127.0.0.1:8000" in p.stderr


@pytest.mark.parametrize("runner", ["wsl", "Docker", ""])
def test_mode_invalide_refuse(runner):
    env = {"K6_RUNNER": runner} if runner else {"K6_RUNNER": "x"}
    p = dry(env)
    assert p.returncode == 2 and p.stdout == "" and "K6_RUNNER invalide" in p.stderr


def test_mode_hote_k6_introuvable_refuse_avant_toute_ecriture(tmp_path):
    journal = tmp_path / "sous_dossier" / "journal.tsv"
    p = run("1", extra_env={"K6_RUNNER": "hote", "K6_BIN": str(tmp_path / "absent" / "k6.exe"),
                            "JOURNAL": str(journal)})
    assert p.returncode == 2 and "k6 introuvable" in p.stderr
    assert not journal.parent.exists()                            # ni dossier ni journal crees


def test_mode_hote_commande_et_pid():
    src = SCRIPT.read_text(encoding="utf-8")
    hote = src[src.index("    # k6.exe sous Windows"): src.index("    rm -f \"$PID_FILE\"")]
    assert '"$K6_BIN" run --quiet --no-usage-report "$LOAD_JS_HOST" &' in hote
    assert 'WSLENV="BASE_URL${WSLENV:+:$WSLENV}"' in hote
    assert 'wait "$k6pid"' in hote and '> "$PID_FILE"' in hote
    assert src.count("--no-usage-report") == 2                    # commande hote + son affichage
    assert "--no-usage-report" not in DOCKER_LINES
