"""
docker-compose.compresse.yml (fichier complementaire du mode compresse), lu comme
texte (PyYAML absent de l'image) : mode compresse pour les processus concernes,
Slack et Gemini vides, trainer manuel, ground truth isolee. Aucun Docker lance.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OVERRIDE = REPO / "docker-compose.compresse.yml"
BASE = REPO / "docker-compose.yml"


def blocks():
    """{service: texte du bloc} sous la cle services: (indentation de 2 espaces)."""
    text = OVERRIDE.read_text(encoding="utf-8").split("\nservices:\n", 1)[1]
    out, name = {}, None
    for line in text.splitlines():
        m = re.match(r"^  ([a-z][a-z-]*):\s*$", line)
        if m:
            name = m.group(1)
            out[name] = ""
        elif name:
            out[name] += line + "\n"
    return out


def test_services_existants_dans_la_base():
    base = BASE.read_text(encoding="utf-8")
    for name in blocks():
        assert f"\n  {name}:\n" in base


def test_mode_compresse_pour_tous_les_processus_saisonniers():
    b = blocks()
    assert set(b) == {"baseline", "detector", "model-monitor", "trainer"}
    for name, body in b.items():
        assert "- SEASON_MODE=compresse" in body, name


def test_slack_et_gemini_desactives_sans_secret():
    body = blocks()["detector"]
    assert re.search(r"- SLACK_WEBHOOK_URL=\s*$", body, re.M)
    assert re.search(r"- GEMINI_API_KEY=\s*$", body, re.M)
    assert "${" not in OVERRIDE.read_text(encoding="utf-8")       # aucune lecture du .env


def test_trainer_manuel_uniquement():
    assert 'profiles: ["manuel"]' in blocks()["trainer"]


def test_ground_truth_isolee():
    b = blocks()
    for name in ("baseline", "detector", "trainer"):
        assert "./scenario-runner/results/compresse:/data/results:ro" in b[name], name


def test_fichier_de_base_non_modifie_par_le_mode():
    base = BASE.read_text(encoding="utf-8")
    assert "SEASON_MODE" not in base and "compresse" not in base
