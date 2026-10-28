"""
Case de baseline de l'explication (explainer.build_context) : meme horloge que le
detecteur (season.slot), convention PostgreSQL (0 = dimanche), a l'heure de la
mesure. Ancien bug : now.weekday() (0 = lundi) lisait la bande du mauvais jour.
Curseur SIMULE : aucune base, aucun appel au LLM.
"""
from datetime import datetime, timezone

import explainer
import season

UTC = timezone.utc


class RecordingCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


def _exact_lookup(cur):
    """Parametres (endpoint, metrique, dow, heure) de la requete de case exacte."""
    for sql, params in cur.calls:
        if "endpoint_baseline" in sql and "hour_bucket = %s" in sql:
            return params
    raise AssertionError("aucune lecture de case exacte")


def test_lundi_lit_la_case_du_lundi_convention_postgresql():
    cur = RecordingCursor()
    lundi = datetime(2024, 1, 8, 10, 15, tzinfo=UTC)
    explainer.build_context(cur, "GET /orders", "p99_ms", lundi)
    assert _exact_lookup(cur) == ("GET /orders", "p99_ms", 1, 10)    # ancien code : dow 0


def test_toute_la_semaine():
    for i, expected in enumerate([0, 1, 2, 3, 4, 5, 6]):      # dimanche 07/01/2024 ...
        cur = RecordingCursor()
        explainer.build_context(cur, "GET /orders", "p99_ms", datetime(2024, 1, 7 + i, 9, tzinfo=UTC))
        assert _exact_lookup(cur)[2:] == (expected, 9)


def test_heure_de_la_mesure_prioritaire_sur_l_heure_de_decision():
    cur = RecordingCursor()
    explainer.build_context(cur, "GET /orders", "p99_ms",
                            datetime(2024, 1, 8, 11, 0, 30, tzinfo=UTC),
                            measured_at=datetime(2024, 1, 8, 10, 59, 40, tzinfo=UTC))
    assert _exact_lookup(cur)[2:] == (1, 10)


def test_mode_compresse(monkeypatch):
    monkeypatch.setattr(season, "MODE", season.COMPRESSE)
    cur = RecordingCursor()
    explainer.build_context(cur, "GET /orders", "p99_ms", datetime(2026, 10, 25, 13, 37, tzinfo=UTC))
    assert _exact_lookup(cur)[2:] == (0, 19)
