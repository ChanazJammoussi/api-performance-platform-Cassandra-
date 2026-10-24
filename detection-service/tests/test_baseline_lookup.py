"""
Reconstruction de la baseline (exact + fallback) a partir de lignes : base de
donnees OU instantane de campagne (train_model.baseline_lookup_from_rows).
Aucune connexion reelle : la base est remplacee par un faux curseur.
"""
from datetime import datetime, timezone

import pytest

import train_model as tm
import validation_checks as vc

COLS = ("endpoint_id", "metric", "dow", "hour_bucket", "p10", "p50", "p90")
TUPLES = [
    ("GET /orders", "p99_ms", 0, 8, 90.0, 120.0, 180.0),
    ("GET /orders", "p99_ms", 0, 9, 70.0, 100.0, 250.0),
    ("GET /orders", "p99_ms", 1, 2, None, 300.0, None),     # bande incomplete
    ("GET /orders", "p50_ms", 0, 8, 40.0, 50.0, 60.0),
    ("POST /payments", "p99_ms", 3, 14, 200.0, 400.0, 900.0),
]
ROWS = [dict(zip(COLS, t)) for t in TUPLES]


class FakeCursor:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class FakeConn:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self):
        return self.cur


def test_cles_exactes():
    exact, _ = tm.baseline_lookup_from_rows(ROWS)
    assert exact[("GET /orders", "p99_ms", 0, 8)] == (90.0, 120.0, 180.0)
    assert exact[("GET /orders", "p99_ms", 1, 2)] == (None, 300.0, None)
    assert len(exact) == len(ROWS)


def test_bande_de_secours_min_moyenne_max_sans_valeurs_vides():
    _, fallback = tm.baseline_lookup_from_rows(ROWS)
    # p10 : min(90, 70) ; p50 : moyenne(120, 100, 300) ; p90 : max(180, 250). None ignores.
    assert fallback[("GET /orders", "p99_ms")] == (70.0, pytest.approx((120 + 100 + 300) / 3), 250.0)
    assert fallback[("POST /payments", "p99_ms")] == (200.0, 400.0, 900.0)


def test_bande_de_secours_entierement_vide():
    rows = [dict(zip(COLS, ("GET /x", "rps", 0, 0, None, None, None)))]
    _, fallback = tm.baseline_lookup_from_rows(rows)
    assert fallback[("GET /x", "rps")] == (None, None, None)


def test_jour_et_heure_convertis_en_entiers():
    rows = [dict(r, dow=float(r["dow"]), hour_bucket=str(r["hour_bucket"])) for r in ROWS]
    exact, _ = tm.baseline_lookup_from_rows(rows)
    assert ("GET /orders", "p99_ms", 0, 8) in exact


def test_liste_vide():
    assert tm.baseline_lookup_from_rows([]) == ({}, {})


def test_equivalence_avec_la_lecture_en_base():
    conn = FakeConn(TUPLES)
    assert tm.load_baseline_lookup(conn) == tm.baseline_lookup_from_rows(ROWS)
    sql = " ".join(conn.cur.sql).upper()
    assert sql.strip().startswith("SELECT") and "FROM ENDPOINT_BASELINE" in sql
    for mot in ("INSERT", "UPDATE ", "DELETE", "TRUNCATE"):
        assert mot not in sql


def test_ordre_des_lignes_sans_effet():
    assert tm.baseline_lookup_from_rows(ROWS) == tm.baseline_lookup_from_rows(list(reversed(ROWS)))


def test_instantane_de_campagne_donne_la_meme_baseline(tmp_path):
    snapshot_rows = [dict(r, sample_count=30, updated_at="2026-11-01T08:00:00+00:00") for r in ROWS]
    fp = vc.write_baseline_snapshot(tmp_path, snapshot_rows, datetime(2026, 11, 1, tzinfo=timezone.utc))
    relues, reasons = vc.check_baseline_snapshot(tmp_path, fp)
    assert reasons == []
    assert tm.baseline_lookup_from_rows(relues) == tm.baseline_lookup_from_rows(ROWS)


def test_baseline_for_identique_depuis_instantane_et_depuis_la_base(tmp_path):
    snapshot_rows = [dict(r, sample_count=30, updated_at=None) for r in ROWS]
    fp = vc.write_baseline_snapshot(tmp_path, snapshot_rows, datetime(2026, 11, 1, tzinfo=timezone.utc))
    relues, _ = vc.check_baseline_snapshot(tmp_path, fp)
    via_snapshot = tm.baseline_lookup_from_rows(relues)
    via_base = tm.load_baseline_lookup(FakeConn(TUPLES))
    for dt in (datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc),     # dimanche 8h : exact
               datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc)):    # creneau absent : secours
        assert tm.baseline_for(*via_snapshot, "GET /orders", dt) == tm.baseline_for(*via_base, "GET /orders", dt)
