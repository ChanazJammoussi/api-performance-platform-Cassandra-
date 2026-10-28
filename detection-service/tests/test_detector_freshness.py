"""
Watermark du detecteur en direct (detector.run_detection) :
  - une mesure fraiche est traitee UNE seule fois ;
  - une mesure perimee n'est pas traitee et l'automate d'alertes n'avance pas ;
  - la case de baseline vient de l'heure de la MESURE, les dates d'alerte de
    l'heure de DECISION.
Base SIMULEE en memoire : aucune connexion reelle, aucun service.
"""
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import detector as det

UTC = timezone.utc
NOW = datetime(2026, 10, 25, 14, 0, 30, tzinfo=UTC)
EP = "GET /orders"                     # SLO p99 = 300 ms : 100 ms = aucune anomalie
DETECTOR = Path(__file__).resolve().parents[1] / "detector.py"


class FakeCursor:
    rowcount = 0

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)

    def fetchone(self):
        return None

    def fetchall(self):
        return list(self.conn.fetchall_rows)

    def close(self):
        pass


class FakeConn:
    def __init__(self, fail_commit=False):
        self.sql, self.commits, self.fail_commit, self.fetchall_rows = [], 0, fail_commit, []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("commit en echec (simule)")
        self.commits += 1

    def rollback(self):
        pass


class Gauge:
    def __init__(self):
        self.value = None

    def set(self, v):
        self.value = v


@pytest.fixture
def env(monkeypatch):
    """Isole run_detection : tout ce qui ecrirait ou appellerait l'exterieur est remplace."""
    state = {"now": NOW, "rows": [], "signals": [], "anomalies": [], "slots": []}

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["now"]

    def fake_baselines(cur, endpoint_id, metrics, dow, hour):
        state["slots"].append((dow, hour))
        return {m: None for m in metrics}

    def fake_signal(cur, endpoint_id, signal_type, anomaly, severity, score, raw_value, layer,
                    now, expected_states=None, **kw):
        state["signals"].append({"endpoint": endpoint_id, "signal": signal_type, "now": now,
                                 "measured_at": kw.get("measured_at")})

    stale = Gauge()
    monkeypatch.setattr(det, "datetime", FixedDatetime)
    monkeypatch.setattr(det, "_LAST_SCORED", {})
    monkeypatch.setattr(det, "_STALE_LOGGED", set())
    monkeypatch.setattr(det, "maybe_load_model", lambda: None)
    monkeypatch.setattr(det, "get_latest_features", lambda cur: list(state["rows"]))
    monkeypatch.setattr(det, "get_baselines", fake_baselines)
    monkeypatch.setattr(det, "analyze_service_graph", lambda *a, **k: {})
    monkeypatch.setattr(det, "write_anomaly",
                        lambda cur, ep, sig, ws, *a: state["anomalies"].append((ep, ws)))
    monkeypatch.setattr(det, "process_signal", fake_signal)
    monkeypatch.setattr(det, "audit_state_consistency", lambda *a: None)
    monkeypatch.setattr(det, "_update_metrics_gauges", lambda cur: None)
    monkeypatch.setattr(det.pm, "ENDPOINTS_STALE", stale)
    state["stale_gauge"] = stale
    return state


def _row(ts, p99=100.0, ep=EP):
    return (ep, p99, 0.0, ts)


def _cycle(env, rows, now=None, conn=None):
    env["rows"] = rows
    if now is not None:
        env["now"] = now
    det.run_detection(conn or FakeConn())


# --- fonction pure -----------------------------------------------------------

def test_select_fresh_classe_les_mesures():
    rows = [_row(NOW - timedelta(seconds=30), ep="A"), _row(NOW - timedelta(seconds=200), ep="B"),
            _row(NOW - timedelta(seconds=40), ep="C")]
    fresh, stale, done = det.select_fresh(rows, {"C": NOW - timedelta(seconds=40)}, NOW)
    assert [r[0] for r in fresh] == ["A"]
    assert [r[0] for r in stale] == ["B"]
    assert [r[0] for r in done] == ["C"]


def test_select_fresh_limite_d_age_incluse():
    at_limit = _row(NOW - timedelta(seconds=det.MAX_FEATURE_AGE_S))
    assert det.select_fresh([at_limit], {}, NOW)[0] == [at_limit]


# --- une mesure fraiche, une seule fois ----------------------------------------

def test_mesure_fraiche_traitee_une_seule_fois(env):
    ts = NOW - timedelta(seconds=30)
    _cycle(env, [_row(ts)])
    assert [s["signal"] for s in env["signals"]] == ["p99_ms", "error_rate_5xx"]
    assert env["anomalies"] == [(EP, ts)]
    _cycle(env, [_row(ts)], now=NOW + timedelta(seconds=60))     # meme mesure, cycle suivant
    assert len(env["signals"]) == 2 and len(env["anomalies"]) == 1


def test_nouvelle_mesure_traitee(env):
    ts = NOW - timedelta(seconds=30)
    _cycle(env, [_row(ts)])
    _cycle(env, [_row(ts + timedelta(seconds=62))], now=NOW + timedelta(seconds=61))
    assert len(env["signals"]) == 4 and len(env["anomalies"]) == 2


# --- mesure perimee : rien n'avance ---------------------------------------------

def test_mesure_perimee_non_traitee_automate_fige(env):
    old = NOW - timedelta(seconds=det.MAX_FEATURE_AGE_S + 1)
    for k in range(3):
        _cycle(env, [_row(old)], now=NOW + timedelta(seconds=60 * k))
    assert env["signals"] == [] and env["anomalies"] == []
    assert env["stale_gauge"].value == 1


def test_scenario_du_04_09_vieille_mesure_jamais_recalculee(env):
    # Diagnostic de l'audit : une mesure de plusieurs semaines etait recalculee
    # a chaque cycle (anomalies en double, automate qui avance).
    ancienne = datetime(2026, 9, 4, 10, 14, 49, tzinfo=UTC)
    for k in range(5):
        _cycle(env, [_row(ancienne, p99=900.0)], now=NOW + timedelta(seconds=60 * k))
    assert env["signals"] == [] and env["anomalies"] == []


def test_mesure_nan_marquee_traitee_sans_automate(env):
    ts = NOW - timedelta(seconds=30)
    _cycle(env, [_row(ts, p99=math.nan)])
    assert env["signals"] == [] and det._LAST_SCORED[EP] == ts


# --- reference temporelle ----------------------------------------------------------

def test_case_de_baseline_a_l_heure_de_la_mesure(env):
    ts = datetime(2026, 10, 25, 13, 59, 50, tzinfo=UTC)          # mesure prise a 13 h
    _cycle(env, [_row(ts)], now=datetime(2026, 10, 25, 14, 0, 40, tzinfo=UTC))
    assert env["slots"] == [(0, 13)]                              # dimanche, 13 h (et non 14 h)


def test_case_de_baseline_mode_compresse(env, monkeypatch):
    monkeypatch.setattr(det.season, "MODE", det.season.COMPRESSE)
    ts = datetime(2026, 10, 25, 13, 37, 0, tzinfo=UTC)
    _cycle(env, [_row(ts)], now=ts + timedelta(seconds=40))
    assert env["slots"] == [(0, 19)]


def test_dates_d_alerte_heure_de_decision_et_mesure_transmise(env):
    ts = NOW - timedelta(seconds=45)
    _cycle(env, [_row(ts)])
    assert all(s["now"] == NOW and s["measured_at"] == ts for s in env["signals"])


# --- robustesse ------------------------------------------------------------------

def test_cycle_en_erreur_ne_marque_aucune_mesure(env):
    ts = NOW - timedelta(seconds=30)
    with pytest.raises(RuntimeError):
        _cycle(env, [_row(ts)], conn=FakeConn(fail_commit=True))
    assert det._LAST_SCORED == {}
    _cycle(env, [_row(ts)], now=NOW + timedelta(seconds=60))     # retentee au cycle suivant
    assert len(env["anomalies"]) == 2


def test_reprise_apres_redemarrage():
    conn = FakeConn()
    ts = NOW - timedelta(seconds=20)
    conn.fetchall_rows = [(EP, ts), ("POST /payments", None)]
    last = det.load_last_scored(conn.cursor())
    assert last == {EP: ts}
    assert "FROM anomalies" in conn.sql[0] and "max(window_start)" in conn.sql[0]
    assert det.select_fresh([_row(ts)], last, NOW)[2] == [_row(ts)]   # deja traitee


def test_cablage_watermark_dans_le_detecteur():
    src = DETECTOR.read_text(encoding="utf-8")
    body = src.split("def run_detection(conn):", 1)[1].split("\ndef ", 1)[0]
    assert "fresh, stale, _done = select_fresh(rows, _LAST_SCORED, now)" in body
    assert "for row in fresh:" in body and "for row in rows:" not in body
    assert "dow, hour = season.slot(ts)" in body and "pg_dow" not in src
    assert "conn.commit()\n    cur.close()\n    # Apres le commit" in body
    assert body.rstrip().endswith("_LAST_SCORED.update(scored)")
    assert "measured_at=measured_at" in src
