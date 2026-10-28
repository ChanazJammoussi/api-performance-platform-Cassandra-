"""
Garde-fous de lancement des campagnes (validation_checks.campaign_target et
season_mode_reasons, cables dans scenario-runner/run_evaluation_suite.py) :
  G1 mode explicite et coherent, G2 dossier conforme au mode, G3 reprise du meme mode.
Fonctions pures + lecture du source de la suite (yaml absent de l'image de test).
Aucun dossier n'est cree, aucune base, aucun reseau.
"""
import ast
import json
import shlex
import subprocess as _sp
import types
from pathlib import Path, PureWindowsPath

import pytest

import season
import validation_checks as vc

REPO = Path(__file__).resolve().parents[2]
SUITE = REPO / "scenario-runner" / "run_evaluation_suite.py"
SEASON_C = season.describe(season.COMPRESSE)


@pytest.fixture
def root(tmp_path):
    """Racine results/ fictive ; rien n'est cree dessous par les fonctions testees."""
    return tmp_path / "scenario-runner" / "results"


def target(root, mode_env, results_dir=None, process_mode=None):
    env = {} if mode_env is None else {"SEASON_MODE": mode_env}
    pm = process_mode if process_mode is not None else (
        season.mode_from_env(env) if mode_env and mode_env.strip().lower() in season.MODES else season.REEL)
    return vc.campaign_target(env, results_dir, root, pm)


# --- G1 : mode explicite ----------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "   "])
def test_mode_absent_ou_vide_refuse(root, value):
    with pytest.raises(vc.CampaignTargetError, match="SEASON_MODE non defini"):
        target(root, value)


def test_mode_invalide_refuse(root):
    with pytest.raises(vc.CampaignTargetError, match="SEASON_MODE invalide"):
        target(root, "accelere", process_mode=season.REEL)


def test_mode_incoherent_apres_chargement_du_env_refuse(root):
    # SEASON_MODE=compresse ajoute par .env alors que le processus a demarre en reel
    with pytest.raises(vc.CampaignTargetError, match="incoherent"):
        target(root, "compresse", process_mode=season.REEL)
    with pytest.raises(vc.CampaignTargetError, match="incoherent"):
        target(root, "reel", process_mode=season.COMPRESSE)


# --- G2 : dossier de sortie --------------------------------------------------------

def test_reel_defaut_inchange(root):
    assert target(root, "reel") == (season.REEL, root.resolve())


def test_reel_dossier_explicite_accepte(root, tmp_path):
    other = tmp_path / "ailleurs"
    assert target(root, "reel", other) == (season.REEL, other.resolve())


@pytest.mark.parametrize("sub", ["compresse", "compresse/lot2", "compresse/../compresse"])
def test_reel_interdit_dans_compresse(root, sub):
    with pytest.raises(vc.CampaignTargetError, match="reserve au mode compresse"):
        target(root, "reel", root / sub)


def test_compresse_defaut_results_compresse(root):
    assert target(root, " Compresse ") == (season.COMPRESSE, (root / "compresse").resolve())


@pytest.mark.parametrize("sub", ["compresse", "compresse/lot2"])
def test_compresse_dans_compresse_accepte(root, sub):
    assert target(root, "compresse", root / sub)[1] == (root / sub).resolve()


@pytest.mark.parametrize("path", ["", "compresse/../autre", "compresse_bis", "../compresse"])
def test_compresse_hors_compresse_refuse(root, path):
    with pytest.raises(vc.CampaignTargetError, match="doivent etre ecrites sous"):
        target(root, "compresse", root / path if path else root)


def test_chemin_relatif_resolu_depuis_le_dossier_courant(root, monkeypatch):
    root.mkdir(parents=True)
    monkeypatch.chdir(root.parent)                 # scenario-runner/
    assert target(root, "compresse", "results/compresse")[1] == (root / "compresse").resolve()
    with pytest.raises(vc.CampaignTargetError):
        target(root, "compresse", "results")


def test_chemins_style_wsl(tmp_path):
    root = tmp_path / "mnt" / "c" / "Users" / "x" / "cassandra" / "scenario-runner" / "results"
    assert target(root, "compresse")[1] == (root / "compresse").resolve()
    with pytest.raises(vc.CampaignTargetError):
        target(root, "compresse", root)


def test_inclusion_style_windows_insensible_a_la_casse():
    reserved = PureWindowsPath(r"C:\Users\chana\cassandra\scenario-runner\results\compresse")
    assert vc._is_within(PureWindowsPath(r"c:\users\CHANA\cassandra\scenario-runner\results\Compresse\c1"),
                         reserved)
    assert not vc._is_within(PureWindowsPath(r"C:\Users\chana\cassandra\scenario-runner\results"), reserved)
    assert not vc._is_within(PureWindowsPath(r"C:\Users\chana\cassandra\scenario-runner\results\compresse_bis"),
                             reserved)


def test_aucun_dossier_cree(root):
    target(root, "compresse")
    target(root, "reel")
    assert not root.exists()


# --- G3 : reprise ----------------------------------------------------------------

def test_reprise_meme_mode_acceptee():
    assert vc.season_mode_reasons({"season": SEASON_C}, SEASON_C) == []
    assert vc.season_mode_reasons({"season": {"mode": "reel"}}, season.describe(season.REEL)) == []


def test_reprise_campagne_historique_sans_mode_en_reel():
    assert vc.season_mode_reasons({"campaign_id": "campaign_20260802_1457"},
                                  season.describe(season.REEL)) == []


def test_reprise_autre_mode_refusee():
    assert vc.season_mode_reasons({}, SEASON_C)                       # historique (reel) en compresse
    assert vc.season_mode_reasons({"season": SEASON_C}, season.describe(season.REEL))
    assert vc.season_mode_reasons({"season": "compresse"}, SEASON_C)  # mal forme
    assert vc.season_mode_reasons([], SEASON_C)                       # pas un objet


def test_reprise_mode_par_defaut_du_processus(monkeypatch):
    monkeypatch.setattr(season, "MODE", season.COMPRESSE)
    assert vc.season_mode_reasons({"season": SEASON_C}) == []
    assert vc.season_mode_reasons({"season": {"mode": "reel"}})


# --- cablage dans la suite (lecture du source) -----------------------------------

def _src():
    return SUITE.read_text(encoding="utf-8")


def _method(src, name):
    start = src.index(f"def {name}(")
    nxt = src.find("\n    def ", start + 1)
    return src[start: nxt if nxt != -1 else len(src)]


def test_suite_g1_g2_dans_init_avant_tout_chemin():
    init = _method(_src(), "__init__")
    call = init.index("vc.campaign_target(")
    assert init.index("self.config = load_config()") < call       # apres le .env
    assert call < init.index("self.results_dir = results_dir") < init.index("self.campaign_dir")
    assert "os.environ, args.results_dir, SUITE_DIR / \"results\", season.MODE" in init
    assert "except vc.CampaignTargetError" in init and "sys.exit(2)" in init
    assert "Path(args.results_dir).resolve()" not in init


def test_suite_g3_avant_dossiers_manifeste_et_injections():
    run = _method(_src(), "run")
    g3 = run.index("vc.season_mode_reasons(manifest)")
    assert run.index("self._load_resume()") < g3 < run.index("self._setup_dirs()")
    assert g3 < run.index("self._write_manifest(scenarios)") < run.index("self._run_scenario(")
    assert "sys.exit(2)" in run[g3: run.index("self._setup_dirs()")]


def test_suite_defaut_results_dir_selon_le_mode():
    src = _src()
    opt = src[src.index('"--results-dir"'): src.index('"--min-core-duration"')]
    assert "default=None" in opt and 'SUITE_DIR / "results"' not in opt


# --- preconditions : services en marche (check_processes via docker ps) -----------

STACK = ["cassandra-timescaledb", "cassandra-scraper", "cassandra-detector", "cassandra-baseline"]


def test_services_tous_en_marche():
    assert vc.running_services(STACK + ["cassandra-k6-compresse"]) == [
        ("cassandra-scraper", True), ("cassandra-detector", True), ("k6", True)]


@pytest.mark.parametrize("absent", ["cassandra-scraper", "cassandra-detector"])
def test_service_absent_refuse(absent):
    res = dict(vc.running_services([n for n in STACK if n != absent] + ["cassandra-k6-compresse"]))
    assert res[absent] is False and sum(not ok for ok in res.values()) == 1


@pytest.mark.parametrize("k6", ["cassandra-k6-compresse", "cassandra-demo-k6"])
def test_conteneur_k6_reconnu(k6):
    assert dict(vc.running_services(STACK + [k6]))["k6"] is True


def test_sans_k6_refuse():
    assert dict(vc.running_services(STACK))["k6"] is False


def test_noms_exacts_pas_de_prefixe():
    res = dict(vc.running_services(["cassandra-scraper-old", "x-cassandra-detector", "cassandra-k6-compresse"]))
    assert res["cassandra-scraper"] is False and res["cassandra-detector"] is False


def test_aucun_conteneur():
    assert all(not ok for _, ok in vc.running_services([]))


def test_suite_check_processes_via_docker_ps():
    src = _src()
    body = src[src.index("def check_processes("): src.index("# ── Garde DB")]
    assert '["docker", "ps", "--no-trunc", "--format", "{{.Names}}|{{.Image}}|{{.Command}}"]' in body
    assert 'names = [l.split("|", 1)[0] for l in lignes]' in body
    assert "conteneurs_k6, raison = k6_docker_containers(lignes)" in body
    assert "k6_source(names, host_k6_state(), season.MODE, conteneurs_k6)" in body
    assert "vc.running_services(names)" in body
    assert '"ps", "aux"' not in body and "scraper.py" not in body
    assert "except (OSError, subprocess.TimeoutExpired)" in body and "result.returncode != 0" in body
    assert body.count("return False") == 2


# --- k6 : une seule source de trafic (k6.exe du mode hote OU conteneur k6) ---------
# La suite n'est pas importable dans l'image de test (yaml absent) : ses fonctions sont
# extraites du source (ast) et executees avec de faux os/subprocess/docker ps. Aucun
# processus n'est lance.

LOAD_JS = PureWindowsPath(r"C:\Users\chana\cassandra\k6\load.js")
K6_EXE = r"C:\Users\chana\tools\k6\k6.exe"
K6_CMD = K6_EXE + " run --quiet --no-usage-report C:/Users/chana/cassandra/k6/load.js"
K6_DOCKER = "cassandra-k6-compresse"


def _fonctions_suite(*names, **ns):
    """Fonctions de la suite extraites du source et compilees seules dans ns."""
    nodes = [n for n in ast.parse(_src()).body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    assert sorted(n.name for n in nodes) == sorted(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SUITE), "exec"), ns)
    return ns


def _k6(processes, load_js=LOAD_JS):
    fn = _fonctions_suite("campaign_k6_running", shlex=shlex)["campaign_k6_running"]
    return fn(processes, load_js)


def _p(cmd=K6_CMD, name="k6.exe"):
    return {"Name": name, "CommandLine": cmd}


@pytest.mark.parametrize("cmd", [
    K6_CMD,                                                         # forme attendue
    f'"{K6_EXE}" run --quiet --no-usage-report C:/Users/chana/cassandra/k6/load.js',
    K6_CMD.upper(),                                                 # casse
    K6_EXE + r" run --quiet --no-usage-report C:\Users\chana\cassandra\k6\load.js",
    K6_EXE + ' run --quiet --no-usage-report "C:/Users/chana/cassandra/k6/load.js"',
    "C:/Users/chana/tools/k6/k6.exe run --quiet --no-usage-report "
    "C:/Users/chana/cassandra/k6/load.js",                          # argv[0] avec /
    "k6.exe  run   --quiet --no-usage-report C:/Users/chana/cassandra/k6/load.js",
])
def test_k6_hote_commande_attendue_acceptee(cmd):
    assert _k6([_p(cmd)]) is True


def test_k6_hote_depot_avec_espaces_entre_guillemets():
    load_js = PureWindowsPath(r"C:\Mes projets\cassandra\k6\load.js")
    cmd = (r'"C:\Program Files\k6\k6.exe" run --quiet --no-usage-report '
           r'"C:\Mes projets\cassandra\k6\load.js"')
    assert _k6([_p(cmd)], load_js) is True


def test_k6_hote_depot_avec_espaces_sans_guillemets_refuse():
    load_js = PureWindowsPath(r"C:\Mes projets\cassandra\k6\load.js")
    cmd = K6_EXE + r" run --quiet --no-usage-report C:\Mes projets\cassandra\k6\load.js"
    assert _k6([_p(cmd)], load_js) is False


@pytest.mark.parametrize("script", [
    "C:/autre-projet/k6/load.js",                   # autre depot
    "C:/Users/chana/cassandra/mk6/load.js",         # dossier ressemblant
    "C:/Users/chana/cassandra/k6/load.js.bak",      # suffixe
    "C:/Users/chana/cassandra/k6/load_light.js",    # autre script du depot
    "C:/Users/chana/cassandra/k6/load.js/",         # separateur final
    "k6/load.js",                                   # chemin relatif
    "C:/Users/chana/cassandra/k6/./load.js",        # chemin non normalise
])
def test_k6_hote_autre_script_refuse(script):
    assert _k6([_p(f"{K6_EXE} run --quiet --no-usage-report {script}")]) is False


@pytest.mark.parametrize("args", [
    "run --quiet C:/Users/chana/cassandra/k6/load.js",                         # sans --no-usage-report
    "run --no-usage-report C:/Users/chana/cassandra/k6/load.js",               # sans --quiet
    "run --quiet --no-usage-report-x C:/Users/chana/cassandra/k6/load.js",     # option modifiee
    "run --quiet --no-usage C:/Users/chana/cassandra/k6/load.js",
    "inspect --quiet --no-usage-report C:/Users/chana/cassandra/k6/load.js",   # pas de run
    "--quiet --no-usage-report C:/Users/chana/cassandra/k6/load.js",
    "--no-usage-report run --quiet C:/Users/chana/cassandra/k6/load.js",       # ordre change
    "run --quiet --no-usage-report C:/Users/chana/cassandra/k6/load.js --vus 200",
    "run --quiet --no-usage-report C:/Users/chana/cassandra/k6/load.js C:/x/autre.js",
    "run --quiet --no-usage-report -e BASE_URL=http://x C:/Users/chana/cassandra/k6/load.js",
    "run --quiet --no-usage-report",                                           # script absent
])
def test_k6_hote_arguments_hors_contexte_refuses(args):
    assert _k6([_p(f"{K6_EXE} {args}")]) is False


@pytest.mark.parametrize("exe", [r"C:\x\k6.exe.bak", r"C:\x\mk6.exe", r"C:\x\k6", "bash.exe"])
def test_k6_hote_autre_executable_refuse(exe):
    cmd = f"{exe} run --quiet --no-usage-report C:/Users/chana/cassandra/k6/load.js"
    assert _k6([_p(cmd)]) is False


@pytest.mark.parametrize("processes", [
    [],                                                   # zero processus
    [_p(), _p()],                                         # deux fois le bon k6
    [_p(), _p(f"{K6_EXE} run autre.js")],                 # bon k6 + k6 manuel
    [_p(), {"CommandLine": K6_CMD}],                      # bon k6 + entree sans nom
])
def test_k6_hote_nombre_de_processus_refuse(processes):
    assert _k6(processes) is False


@pytest.mark.parametrize("processes", [
    [{"CommandLine": K6_CMD}], [_p(name=None)],           # sans nom
    [_p(name="bash.exe")], [_p(name="k6.exe.bak")],       # autre nom
    [_p(cmd=None)], [_p(cmd="")], [{"Name": "k6.exe"}],   # sans ligne de commande
    ["k6.exe"], [None], [42],                             # entrees mal formees
    _p(), None, "k6.exe",                                 # pas une liste
])
def test_k6_hote_entree_incomplete_refusee(processes):
    assert _k6(processes) is False


def test_k6_hote_guillemet_non_ferme_refuse_sans_exception():
    cmd = f'"{K6_EXE} run --quiet --no-usage-report C:/Users/chana/cassandra/k6/load.js'
    assert _k6([_p(cmd)]) is False


def _etat(fake_run, os_name="nt"):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return fake_run()

    ns = _fonctions_suite(
        "campaign_k6_running", "host_k6_state",
        os=types.SimpleNamespace(name=os_name), json=json, shlex=shlex,
        CASSANDRA_DIR=PureWindowsPath(r"C:\Users\chana\cassandra"),
        subprocess=types.SimpleNamespace(run=run, TimeoutExpired=_sp.TimeoutExpired))
    return ns["host_k6_state"](), calls


def _sortie(stdout, code=0, stderr=""):
    return lambda: types.SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def _leve(exc):
    def f():
        raise exc
    return f


def test_host_k6_etat_conforme_et_commande_powershell():
    etat, calls = _etat(_sortie(json.dumps([_p()])))
    assert etat == "conforme" and len(calls) == 1
    cmd, kw = calls[0]
    assert cmd[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert cmd[4].startswith("$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; ")
    assert "@(Get-CimInstance Win32_Process" in cmd[4] and "Name='k6.exe'" in cmd[4]
    assert cmd[4].endswith("ConvertTo-Json -InputObject $p -Compress")
    assert kw["timeout"] == 30 and kw["capture_output"] is True


@pytest.mark.parametrize("stdout", ["[]", "[]\r\n"])
def test_host_k6_etat_absent_prouve_par_liste_vide(stdout):
    assert _etat(_sortie(stdout))[0] == "absent"


@pytest.mark.parametrize("stdout", [
    json.dumps([_p(), _p()]),                             # deux k6.exe
    json.dumps([_p(f"{K6_EXE} run autre.js")]),           # k6.exe non conforme
    json.dumps([None]), json.dumps(["k6.exe"]),           # liste au contenu inattendu
])
def test_host_k6_etat_non_conforme(stdout):
    assert _etat(_sortie(stdout))[0] == "non_conforme"


@pytest.mark.parametrize("stdout,code,stderr", [
    ("", 0, ""),                                          # sortie vide : jamais "absent"
    ("  \r\n", 0, ""),
    ("{pas du json", 0, ""),                              # JSON invalide
    ("WARNING: x\n" + json.dumps([_p()]), 0, ""),         # texte avant le JSON
    ("\ufeff" + json.dumps([_p()]), 0, ""),               # BOM en tete -> json.loads refuse
    (json.dumps([_p()]), 1, ""),                          # code de retour non nul
    ("", 1, "Get-CimInstance : acces refuse"),            # erreur PowerShell
    ("[]", 0, "Get-CimInstance : avertissement"),         # texte sur stderr malgre le code 0
    ("null", 0, ""), ("42", 0, ""), ('"k6.exe"', 0, ""),  # JSON valide mais pas une liste
    (json.dumps(_p()), 0, ""),                            # objet seul au lieu d'une liste
])
def test_host_k6_etat_inverifiable(stdout, code, stderr):
    assert _etat(_sortie(stdout, code, stderr))[0] == "inverifiable"


@pytest.mark.parametrize("exc", [OSError("powershell introuvable"),
                                 _sp.TimeoutExpired("powershell", 30)])
def test_host_k6_etat_erreur_systeme_inverifiable(exc):
    assert _etat(_leve(exc))[0] == "inverifiable"


def test_host_k6_etat_hors_windows_aucun_appel():
    etat, calls = _etat(_sortie(json.dumps([_p()])), os_name="posix")
    assert etat == "hors_windows" and calls == []


MODE_C, MODE_R = season.COMPRESSE, season.REEL          # valeurs reelles de season.MODE


def _source(names, etat, mode=MODE_C, conteneurs_k6=()):
    return _fonctions_suite("k6_source", season=season)["k6_source"](names, etat, mode, conteneurs_k6)


@pytest.mark.parametrize("extra,etat,mode,ok", [
    # mode compresse : uniquement le k6.exe du mode hote (docs/mode_compresse.md)
    ([], "conforme", MODE_C, True),                       # mode hote seul
    ([K6_DOCKER], "absent", MODE_C, False),               # conteneur du mode docker refuse
    ([K6_DOCKER], "hors_windows", MODE_C, False),         # idem hors Windows
    ([K6_DOCKER], "conforme", MODE_C, False),             # k6 Docker ET k6 Windows
    ([K6_DOCKER, K6_DOCKER], "absent", MODE_C, False),    # nom en double : fusionne en un seul, refuse (non autorise en compresse)
    ([], "absent", MODE_C, False),                        # aucune source
    ([], "hors_windows", MODE_C, False),
    ([K6_DOCKER], "non_conforme", MODE_C, False),         # conteneur + k6.exe douteux
    ([], "non_conforme", MODE_C, False),
    ([K6_DOCKER], "inverifiable", MODE_C, False),         # Windows non verifiable
    ([], "inverifiable", MODE_C, False),
    ([K6_DOCKER], "etat_inconnu", MODE_C, False),         # etat inattendu
    ([K6_DOCKER], None, MODE_C, False),
    (["cassandra-demo-k6"], "absent", MODE_C, False),         # nom de demo.sh/benchmark
    (["cassandra-k6"], "absent", MODE_C, False),              # nom de demo-guide.md
    (["x-cassandra-k6-compresse"], "absent", MODE_C, False),  # nom approchant
    ([K6_DOCKER, "cassandra-k6"], "absent", MODE_C, False),   # autorise + inconnu
    (["cassandra-demo-k6"], "conforme", MODE_C, False),
    # mode reel : seul cassandra-demo-k6 (demo.sh, benchmark/keep_k6_*.sh), pas de mode hote
    (["cassandra-demo-k6"], "absent", MODE_R, True),
    (["cassandra-demo-k6"], "hors_windows", MODE_R, True),
    ([], "conforme", MODE_R, False),                      # k6.exe hote non prevu en reel
    ([K6_DOCKER], "absent", MODE_R, False),
    (["cassandra-demo-k6"], "conforme", MODE_R, False),   # deux sources
    # mode saisonnier inconnu ou non normalise (season.MODE est toujours en minuscules)
    ([K6_DOCKER], "absent", "autre", False),
    ([K6_DOCKER], "absent", "Compresse", False),
    ([K6_DOCKER], "absent", None, False),
])
def test_k6_source_unique(extra, etat, mode, ok):
    assert _source(STACK + extra, etat, mode)[0] is ok


@pytest.mark.parametrize("mode", season.MODES)
def test_k6_source_connait_chaque_mode_reel(mode):
    ok, detail = _source(STACK, "absent", mode)
    assert ok is False and detail == "aucune source k6"     # mode connu : pas "mode inconnu"


def test_k6_source_double_nomme_les_deux_sources():
    ok, detail = _source(STACK + [K6_DOCKER], "conforme")
    assert ok is False and "k6.exe" in detail and K6_DOCKER in detail


def test_k6_source_conteneur_non_autorise_nomme():
    ok, detail = _source(STACK + ["cassandra-demo-k6"], "absent")
    assert ok is False and "non autorise en mode compresse" in detail and "cassandra-demo-k6" in detail


def test_k6_source_hote_refuse_en_mode_reel():
    ok, detail = _source(STACK, "conforme", MODE_R)
    assert ok is False and detail == "k6.exe du mode hote non prevu en mode reel"


def _check(docker, etat="conforme", vc_=vc, mode=MODE_C):
    """check_processes extraite du source, avec un faux docker ps et un faux etat Windows."""
    appels = []

    def run(cmd, **kw):
        return docker()

    def etat_k6():
        appels.append(1)
        return etat

    ns = _fonctions_suite("check_processes", "k6_source", "k6_docker_containers", "_image_k6",
                          "_executable", vc=vc_, host_k6_state=etat_k6,
                          season=types.SimpleNamespace(MODE=mode, COMPRESSE=season.COMPRESSE,
                                                       REEL=season.REEL),
                          subprocess=types.SimpleNamespace(run=run, TimeoutExpired=_sp.TimeoutExpired))
    return ns["check_processes"](), appels


DOCKER_PS_REEL = [  # docker ps --no-trunc --format {{.Names}}|{{.Image}}|{{.Command}} (27/10/2026)
    # les 8 lignes lues en entier lors de l'audit ; gateway, orders, payments et prometheus
    # (commandes coupees a l'affichage) ne sont pas reproduites
    'cassandra-baseline|cassandra_fresh-baseline|"python -u baseline_job.py --loop --interval 3600"',
    'cassandra-deploy-api|cassandra_fresh-deploy-api|"uvicorn deploy_api:app --host 0.0.0.0 --port 8090"',
    'cassandra-detector|cassandra_fresh-detector|"python -u detector.py"',
    'cassandra-grafana|grafana/grafana:10.4.0|"/run.sh"',
    'cassandra-model-monitor|cassandra_fresh-model-monitor|"python -u model_monitor.py --loop --interval 1800"',
    'cassandra-otel-collector|otel/opentelemetry-collector-contrib:0.102.0|"/otelcol-contrib --config=/etc/otel/config.yaml"',
    'cassandra-scraper|cassandra_fresh-scraper|"python -u scraper.py"',
    'cassandra-timescaledb|timescale/timescaledb:latest-pg16|"docker-entrypoint.sh postgres"',
]
LIGNES_REELLES = {l.split("|", 1)[0]: l for l in DOCKER_PS_REEL}


def _ligne(nom):
    """Ligne "nom|image|commande" de docker ps ; une ligne deja au format est gardee."""
    if "|" in nom:
        return nom
    if "k6" in nom:
        return f'{nom}|grafana/k6:latest|"k6 run --quiet /scripts/load.js"'
    return LIGNES_REELLES[nom]


def _docker_ps(names, code=0):
    return lambda: types.SimpleNamespace(returncode=code,
                                         stdout="\n".join(_ligne(n) for n in names) + "\n",
                                         stderr="erreur docker")


@pytest.mark.parametrize("names,etat,mode,ok", [
    (STACK, "conforme", MODE_C, True),                                    # mode hote
    (STACK + [K6_DOCKER], "absent", MODE_C, False),                       # mode docker refuse
    (STACK + [K6_DOCKER], "conforme", MODE_C, False),                     # double source
    (STACK, "absent", MODE_C, False),                                     # aucune source
    ([n for n in STACK if n != "cassandra-scraper"], "conforme", MODE_C, False),
    (STACK + ["cassandra-demo-k6"], "absent", MODE_C, False),             # nom non autorise
    (STACK + ["cassandra-demo-k6"], "hors_windows", MODE_R, True),        # benchmark reel (WSL)
    (STACK, "conforme", MODE_R, False),                                   # hote non prevu en reel
    (STACK + ['eager_turing|grafana/k6|"k6 run x.js"'], "conforme", MODE_C, False),  # k6 par l'image
])
def test_check_processes_integration(names, etat, mode, ok):
    assert _check(_docker_ps(names), etat, mode=mode)[0] is ok


@pytest.mark.parametrize("etat,ok", [("absent", False), ("conforme", True)])
def test_check_processes_k6_ne_depend_pas_de_la_ligne_k6(etat, ok):
    sans_ligne_k6 = types.SimpleNamespace(running_services=lambda names: [
        ("cassandra-scraper", True), ("cassandra-detector", True)])
    assert _check(_docker_ps(STACK), etat, vc_=sans_ligne_k6)[0] is ok


@pytest.mark.parametrize("docker", [_docker_ps(STACK, code=1), _leve(OSError("docker absent"))])
def test_check_processes_docker_ps_en_echec_refuse_sans_interroger_windows(docker):
    ok, appels = _check(docker)
    assert ok is False and appels == []


def test_check_processes_affiche_la_source_k6(capsys):
    assert _check(_docker_ps(STACK + [K6_DOCKER]), "conforme")[0] is False
    out = capsys.readouterr().out
    assert "[OK  ] cassandra-scraper" in out and "[OK  ] cassandra-detector" in out
    assert "[FAIL] k6 (deux sources : k6.exe et cassandra-k6-compresse)" in out
    assert out.count("] k6") == 1                         # une seule ligne k6


def test_suite_k6_import_et_ordre_des_fonctions():
    src = _src()
    assert "\nimport shlex\n" in src
    pos = [src.index(f"def {n}(") for n in
           ("campaign_k6_running", "host_k6_state", "_image_k6", "_executable",
            "k6_docker_containers", "k6_source", "check_processes")]
    assert pos == sorted(pos)


def test_check_processes_sortie_illisible_refuse_sans_interroger_windows(capsys):
    ok, appels = _check(_docker_ps(STACK + ["nom|image"]))
    assert ok is False and appels == []
    assert "[FAIL] k6 (sortie docker ps illisible)" in capsys.readouterr().out


# --- conteneurs k6 : nom, image ou executable (docker ps --no-trunc, audit du 27/10) -------

@pytest.mark.parametrize("image,attendu", [
    ("grafana/k6", True), ("GRAFANA/K6:LATEST", True), ("docker.io/grafana/k6:0.49.0", True),
    ("localhost/grafana/k6", True), ("localhost:5000/grafana/k6:1.2@sha256:" + "a" * 64, True),
    ("k6", True),
    ("grafana/k6-operator", False), ("grafana/k6x", False), ("grafana/grafana:10.4.0", False),
    ("registre:5000/k6/outil", False),                       # "k6" dans le chemin, pas en dernier
    ("e7eeddf1ce23", None), ("b" * 64, None), ("sha256:" + "c" * 64, None),  # identifiant seul
])
def test_image_k6(image, attendu):
    assert _fonctions_suite("_image_k6")["_image_k6"](image) is attendu


@pytest.mark.parametrize("command,exe", [
    ('"k6 run --quiet /scripts/load.js"', "k6"),
    ('"/usr/bin/k6 run x.js"', "k6"),
    ('"python -u detector.py"', "python"),
    ('"run --quiet x.js"', "run"),                       # executable absent de la commande
    ('"sh -c \'k6 run x.js\'"', "sh"),                   # commande indirecte
    ('""', ""), ("", ""),
])
def test_executable(command, exe):
    assert _fonctions_suite("_executable")["_executable"](command) == exe


def _conteneurs(lignes):
    fn = _fonctions_suite("k6_docker_containers", "_image_k6", "_executable")["k6_docker_containers"]
    return fn(lignes)


def test_conteneurs_sortie_reelle_aucun_k6():
    assert _conteneurs(DOCKER_PS_REEL) == ([], None)


@pytest.mark.parametrize("ligne,noms", [
    ('eager_turing|grafana/k6|"k6 run x.js"', ["eager_turing"]),   # nom sans "k6", image k6
    ('y|grafana/k6|"run --quiet x.js"', ["y"]),                    # image k6, commande sans executable
    ('x|e7eeddf1ce23|"k6 run x.js"', ["x"]),                       # identifiant + executable k6
    ('x|e7eeddf1ce23|"python -u scraper.py"', []),                 # identifiant + autre executable
    ('outil|monimage:1|"/usr/local/bin/k6 run x.js"', ["outil"]),  # image perso, executable k6
    ('outil|alpine|"sh -c \'a | b\'"', []),                        # "|" dans la commande
    # LIMITES CONNUES, non detectees : identifiant + commande sans executable ; k6 indirect
    ('x|e7eeddf1ce23|"run --quiet x.js"', []),
    ('outil|alpine|"sh -c \'k6 run x.js\'"', []),
])
def test_conteneurs_image_ou_executable(ligne, noms):
    assert _conteneurs(DOCKER_PS_REEL + [ligne]) == (noms, None)


@pytest.mark.parametrize("ligne", ["nom|image", '|grafana/k6|"k6"', 'nom||"k6"'])
def test_conteneurs_ligne_illisible_refusee(ligne):
    assert _conteneurs(DOCKER_PS_REEL + [ligne]) == (None, "sortie docker ps illisible")


def test_conteneurs_identifiant_sans_commande_refuse():
    assert _conteneurs(DOCKER_PS_REEL + ['x|e7eeddf1ce23|""']) == (
        None, "conteneur x : image non identifiable (e7eeddf1ce23) et commande vide")


@pytest.mark.parametrize("names,etat,mode,conteneurs,ok", [
    (STACK, "absent", MODE_C, ["eager_turing"], False),                         # k6 par l'image
    (STACK + ["cassandra-demo-k6"], "absent", MODE_R, ["cassandra-demo-k6"], True),  # compte une fois
    (STACK + ["cassandra-demo-k6"], "absent", MODE_R, ["eager_turing"], False),
])
def test_k6_source_avec_conteneurs_k6(names, etat, mode, conteneurs, ok):
    assert _source(names, etat, mode, conteneurs)[0] is ok


def test_k6_source_double_source_par_image():
    ok, detail = _source(STACK, "conforme", MODE_C, ["eager_turing"])
    assert ok is False and detail == "deux sources : k6.exe et eager_turing"
