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


# ===========================================================================
# Lot 2b-2 : evaluation officielle / diagnostique d'une campagne.
# Campagne SYNTHETIQUE (tmp_path) + base SIMULEE qui echoue si l'on lit
# endpoint_baseline ou si l'on tente une ecriture.
# ===========================================================================
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

import joblib  # noqa: E402
import numpy as np  # noqa: E402

import ml_model  # noqa: E402
import validation_checks as vc  # noqa: E402
from features import FEATURE_NAMES  # noqa: E402

T0E = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)          # jeudi -> dow PostgreSQL 4
EP = "GET /orders"                                       # SLO p99 = 300 ms
W1 = T0E + timedelta(minutes=30)
W2 = T0E + timedelta(minutes=60)
FP_SPIKE = (T0E + timedelta(minutes=90), T0E + timedelta(minutes=92))
BANDS = {"p50_ms": (40.0, 50.0, 70.0), "p95_ms": (60.0, 80.0, 120.0),
         "p99_ms": (80.0, 100.0, 150.0), "rps": (5.0, 10.0, 15.0)}


def band_rows(p99=None, drop=(), p99_by_hour=None):
    rows = []
    for h in (8, 9, 10):
        for m, band in BANDS.items():
            if (m, h) in drop:
                continue
            if m == "p99_ms":
                band = (p99_by_hour or {}).get(h, p99 or band)
            rows.append({"endpoint_id": EP, "metric": m, "dow": 4, "hour_bucket": h,
                         "p10": band[0], "p50": band[1], "p90": band[2],
                         "sample_count": 30, "updated_at": "2026-10-01T07:00:00+00:00"})
    return rows


def make_eval_campaign(root, rows=None, starts=(W1, W2), name="campaign_20261001_0800"):
    d = root / name
    (d / "ground_truth").mkdir(parents=True)
    rows = band_rows() if rows is None else rows
    fp = vc.write_baseline_snapshot(d, rows, T0E)
    manifest = {"campaign_id": name, "started_at": T0E.isoformat(), "commit_sha": "abc",
                "scenarios": ["s1"], "scrape_interval": 60,
                "feature_names": FEATURE_NAMES, "feature_fingerprint": vc.feature_fingerprint(FEATURE_NAMES),
                "baseline_snapshot": vc.BASELINE_SNAPSHOT_NAME, "baseline_fingerprint": fp,
                "baseline_row_count": len(rows),
                "completed_at": (T0E + timedelta(hours=3)).isoformat()}
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    entries = []
    for tier, start in zip(("core", "stress"), starts):
        gt = f"s1_{tier}_20261001_0800.json"
        fault = {"scenario_id": "s1", "fault_type": "latency_step", "target_service": "orders",
                 "target_endpoint": EP, "injected_at": start.isoformat(),
                 "cleared_at": (start + timedelta(minutes=5)).isoformat(),
                 "magnitude": {"latency_ms": 900}}
        (d / "ground_truth" / gt).write_text(json.dumps({"faults": [fault]}), encoding="utf-8")
        entries.append({"scenario": "s1", "tier": tier, "status": "ok", "ground_truth_file": gt})
    (d / "summary.json").write_text(json.dumps(entries), encoding="utf-8")
    return d


def feature_rows(spikes=None, drop=(), end=T0E + timedelta(hours=2)):
    spikes = [(W1, W1 + timedelta(minutes=5)), (W2, W2 + timedelta(minutes=5)), FP_SPIKE] \
        if spikes is None else spikes
    rows, t = [], T0E
    while t <= end:
        if not any(a <= t <= b for a, b in drop):
            p99 = 1000.0 if any(a <= t <= b for a, b in spikes) else 100.0
            rows.append((EP, t, 10.0, 50.0, 80.0, p99, 0.0))
        t += timedelta(minutes=1)
    return rows


class GuardCursor:
    WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE)\b", re.I)

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        if "endpoint_baseline" in sql.lower():
            raise AssertionError("la baseline en base ne doit jamais etre lue")
        if self.WRITE.search(sql):
            raise AssertionError(f"ecriture interdite : {sql.strip()[:40]}")
        self.conn.sql.append(sql)

    def fetchall(self):
        return list(self.conn.rows)

    def close(self):
        pass


class GuardConn:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def cursor(self, *a, **k):
        return GuardCursor(self)

    def set_session(self, **k):
        self.readonly = k.get("readonly")

    def commit(self):
        raise AssertionError("commit interdit")

    def close(self):
        pass


def _evaluate(tmp_path, rows=None, features=None, **kw):
    d = make_eval_campaign(tmp_path, rows=rows)
    conn = GuardConn(feature_rows() if features is None else features)
    return ev.evaluate_campaign(conn, d, **kw), conn


def _hours_expected():
    # 121 lignes, derniere exclue -> 120 ; 2 x 8 lignes dans [injected, cleared + 120 s].
    return (120 - 16) / 60


# --- resultats officiels --------------------------------------------------------

def test_campagne_complete_resultats_officiels_S_et_B(tmp_path):
    res, conn = _evaluate(tmp_path)
    assert res["statut"] == "officiel", res["raisons"]
    o = res["officiel"]
    for k in ("S", "B"):
        assert (o[k]["detectees"], o[k]["fenetres"], o[k]["recall"]) == (2, 2, 1.0)
        assert o[k]["fp"] == 1
        assert o[k]["fp_par_heure"] == pytest.approx(1 / _hours_expected())
        assert o[k]["delai_median_s"] == 60.0
    assert o["identite"]["heures_observables_hors_panne"] == pytest.approx(_hours_expected())
    assert o["identite"]["empreinte_fenetres_comparees"] == o["identite"]["empreinte_fenetres_campagne"]


def test_C_jamais_chiffre_et_N_non_fourni(tmp_path):
    res, _ = _evaluate(tmp_path)
    assert res["officiel"]["C"]["statut"] == "non_verifiable"
    assert "recall" not in res["officiel"]["C"]
    assert res["officiel"]["N"] == {"statut": "non_fourni"}


def test_baseline_en_base_jamais_lue_ni_ecriture(tmp_path):
    res, conn = _evaluate(tmp_path)
    assert res["statut"] == "officiel"
    assert len(conn.sql) == 1 and "FROM endpoint_features" in conn.sql[0]


def test_resultats_reproductibles(tmp_path):
    a, _ = _evaluate(tmp_path / "a")
    b, _ = _evaluate(tmp_path / "b")
    assert a["officiel"] == b["officiel"]


# --- campagne / donnees invalides -----------------------------------------------

def test_campagne_format_historique_refusee_sans_requete(tmp_path):
    d = make_eval_campaign(tmp_path)
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    for k in ("feature_fingerprint", "baseline_fingerprint"):
        m.pop(k)
    (d / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    conn = GuardConn(feature_rows())
    res = ev.evaluate_campaign(conn, d)
    assert res["statut"] == "campagne_invalide" and res["officiel"] is None
    assert conn.sql == []


def test_instantane_modifie_refuse(tmp_path):
    d = make_eval_campaign(tmp_path)
    p = d / vc.BASELINE_SNAPSHOT_NAME
    snap = json.loads(p.read_text(encoding="utf-8"))
    snap["rows"][0]["p90"] = 999.0
    p.write_text(json.dumps(snap), encoding="utf-8")
    res = ev.evaluate_campaign(GuardConn(feature_rows()), d)
    assert res["statut"] == "campagne_invalide"
    assert any("empreinte" in r for r in res["raisons"])


def test_scrape_interval_absent_refuse(tmp_path):
    d = make_eval_campaign(tmp_path)
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    m.pop("scrape_interval")
    (d / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    res = ev.evaluate_campaign(GuardConn(feature_rows()), d)
    assert res["statut"] == "campagne_invalide"


def test_aucune_mesure_n_est_pas_zero_faux_positif(tmp_path):
    res, _ = _evaluate(tmp_path, features=[])
    assert res["statut"] == "aucune_mesure" and res["officiel"] is None
    assert "resultats_partiels" not in res["diagnostic"]


# --- couverture -------------------------------------------------------------------

def test_fenetre_sans_mesures_pas_de_resultat_officiel_mais_diagnostic(tmp_path):
    feats = feature_rows(drop=[(W2, W2 + timedelta(minutes=7))])
    res, _ = _evaluate(tmp_path, features=feats)
    assert res["statut"] == "couverture_insuffisante" and res["officiel"] is None
    w2 = [f for f in res["diagnostic"]["fenetres"] if f["source"].startswith("s1_stress")][0]
    assert not w2["evaluable_SB"]
    assert any("mesures insuffisantes" in r for r in w2["raisons_SB"])
    assert any("trous dans les mesures" in r for r in w2["raisons_SB"])
    partiels = res["diagnostic"]["resultats_partiels"]
    assert "PAS une evaluation officielle" in partiels["avertissement"]
    assert partiels["S"]["fenetres"] == 1


def test_alerte_pendant_panne_non_evaluable_n_est_pas_un_faux_positif(tmp_path):
    # W2 non evaluable (aucun contexte avant la panne) mais des alertes pendant W2.
    feats = feature_rows(drop=[(W2 - timedelta(minutes=10), W2 - timedelta(seconds=1))])
    res, _ = _evaluate(tmp_path, features=feats)
    assert res["statut"] == "couverture_insuffisante"
    s = res["diagnostic"]["resultats_partiels"]["S"]
    assert s["alertes_pendant_panne_non_comparee"] == 1
    assert s["fp"] == 1                         # seul le pic hors panne


def test_derniere_mesure_ignoree_comme_le_rejeu(tmp_path):
    # Mesures qui s'arretent juste apres W2 : la derniere ligne n'est pas comptee.
    res, _ = _evaluate(tmp_path, features=feature_rows(end=W2 + timedelta(minutes=7)))
    w2 = [f for f in res["diagnostic"]["fenetres"] if f["source"].startswith("s1_stress")][0]
    assert w2["pendant"] == 7 and w2["attendus"] == 8


def test_trous_mesures_et_affiches(tmp_path):
    feats = feature_rows(drop=[(W1 + timedelta(minutes=2), W1 + timedelta(minutes=4))])
    res, _ = _evaluate(tmp_path, features=feats)
    w1 = [f for f in res["diagnostic"]["fenetres"] if f["source"].startswith("s1_core")][0]
    assert w1["plus_grand_ecart_s"] == 240.0
    assert w1["evaluable_SB"]                     # aucune limite d'ecart imposee


# --- baseline ----------------------------------------------------------------------

def test_baseline_degeneree_refuse_le_resultat_officiel(tmp_path):
    res, _ = _evaluate(tmp_path, rows=band_rows(p99=(100.0, 100.0, 100.0)))
    assert res["statut"] == "couverture_insuffisante"
    assert all(any("degenere" in r for r in f["raisons_SB"]) for f in res["diagnostic"]["fenetres"])


def test_bande_de_secours_valide_acceptee_et_comptee(tmp_path):
    res, _ = _evaluate(tmp_path, rows=band_rows(drop=[("p99_ms", 8)]))
    assert res["statut"] == "officiel", res["raisons"]
    w1 = [f for f in res["diagnostic"]["fenetres"] if f["source"].startswith("s1_core")][0]
    assert w1["creneaux_secours"]["p99_ms"] == 1
    assert w1["baseline"]["p99_ms"] == "fallback"


def test_bande_de_secours_inutilisable_refusee(tmp_path):
    rows = band_rows(drop=[("p99_ms", 8)], p99_by_hour={9: (None, 100.0, 150.0), 10: (None, 100.0, 150.0)})
    res, _ = _evaluate(tmp_path, rows=rows)
    assert res["statut"] == "couverture_insuffisante"
    w1 = [f for f in res["diagnostic"]["fenetres"] if f["source"].startswith("s1_core")][0]
    assert any("bande de secours inutilisable" in r for r in w1["raisons_SB"])


# --- faux positifs -------------------------------------------------------------------

def test_denominateur_nul_taux_impossible():
    w = {"injected": W1, "cleared": W1 + timedelta(minutes=5)}
    rows = {EP: [{"time": W1 + timedelta(minutes=m)} for m in range(5)]}
    assert ev.observable_hours_outside_faults(rows, [w], 60) == (0.0, 0)
    stats = ev.configuration_stats({}, "static", [], [], 0.0)
    assert stats["fp"] == 0 and stats["fp_par_heure"] is None and stats["recall"] is None


def test_denominateur_exclut_pannes_et_periodes_sans_mesure():
    w = {"injected": W1, "cleared": W1 + timedelta(minutes=5)}
    times = [W1 - timedelta(minutes=3), W1 - timedelta(minutes=2),           # observables
             W1 + timedelta(minutes=1), W1 + timedelta(minutes=6),           # dans la panne (+120 s)
             W1 + timedelta(minutes=30)]                                     # derniere : ignoree
    hours, n = ev.observable_hours_outside_faults({EP: [{"time": t} for t in times]}, [w], 60)
    assert n == 2 and hours == pytest.approx(2 / 60)


# --- persistance et selection explicite -------------------------------------------------

def test_persist_refuse_sans_ecriture(tmp_path, monkeypatch, capsys):
    d = make_eval_campaign(tmp_path)
    conn = GuardConn(feature_rows())
    monkeypatch.setattr(ev.psycopg2, "connect", lambda *a, **k: conn)
    assert ev.main(["--input", str(d), "--persist"]) == 3
    assert "aucune ecriture" in capsys.readouterr().err
    assert conn.readonly is True
    assert all("INSERT" not in s.upper() for s in conn.sql)


def test_campagne_explicite_obligatoire(monkeypatch):
    monkeypatch.delenv("EVAL_INPUT", raising=False)

    def no_connect(*a, **k):
        raise AssertionError("aucune connexion sans campagne explicite")
    monkeypatch.setattr(ev.psycopg2, "connect", no_connect)
    assert ev.main([]) == 1


def test_eval_input_accepte_et_codes_de_sortie(tmp_path, monkeypatch):
    d = make_eval_campaign(tmp_path)
    monkeypatch.setattr(ev.psycopg2, "connect", lambda *a, **k: GuardConn(feature_rows()))
    monkeypatch.setenv("EVAL_INPUT", str(d))
    assert ev.main([]) == 0
    monkeypatch.setattr(ev.psycopg2, "connect", lambda *a, **k: GuardConn([]))
    assert ev.main([]) == 2


# --- candidat N ------------------------------------------------------------------------

def _candidate(tmp_path, names=None):
    X = np.random.default_rng(7).normal(size=(300, len(FEATURE_NAMES)))
    model = ml_model.train(X, contamination=0.02)
    meta = ml_model.build_meta("2026-10-01T07:00:00+00:00", X, 0.02, 10, 14)
    meta["calibration"] = ml_model.calibration_params(ml_model.raw_anomaly(model, X))
    if names is not None:
        meta["feature_names"] = names
    path = tmp_path / "candidat.joblib"
    joblib.dump({"model": model, "meta": meta}, path)
    return path


def test_candidat_compatible_evalue_sur_les_memes_fenetres(tmp_path):
    path = _candidate(tmp_path)
    res, _ = _evaluate(tmp_path / "c", candidate_path=path)
    assert res["statut"] == "officiel", res["raisons"]
    n, s = res["officiel"]["N"], res["officiel"]["S"]
    assert n["statut"] == "evalue" and n["fenetres"] == s["fenetres"] == 2
    assert n["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert res["officiel"]["C"]["statut"] == "non_verifiable"


def test_candidat_incompatible_sans_chiffres(tmp_path):
    path = _candidate(tmp_path, names=list(reversed(FEATURE_NAMES)))
    res, _ = _evaluate(tmp_path / "c", candidate_path=path)
    n = res["officiel"]["N"]
    assert n["statut"] == "incompatible" and "recall" not in n and n["raisons"]


def test_candidat_illisible(tmp_path):
    bad = tmp_path / "x.joblib"
    bad.write_bytes(b"pas un modele")
    res, _ = _evaluate(tmp_path / "c", candidate_path=bad)
    assert res["officiel"]["N"]["statut"] == "incompatible"
