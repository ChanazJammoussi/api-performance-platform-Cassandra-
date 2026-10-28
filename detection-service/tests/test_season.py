"""
Horloge saisonniere unique (season.py) : le mode reel reproduit exactement l'ancien
calcul ; le mode compresse range chaque mesure dans l'heure simulee de load.js.
Fonctions pures uniquement : aucune base, aucun service, aucun trafic.
"""
import random
from datetime import datetime, timedelta, timezone

import pytest

import baseline_job
import season
import train_model
import validation_checks as vc
from baseline_utils import pg_dow

UTC = timezone.utc
PLUS1 = timezone(timedelta(hours=1))


def _old_slot(dt):
    """Ancien calcul de detector.py / train_model.baseline_for : (pg_dow(dt), dt.hour)."""
    return pg_dow(dt), dt.hour


def _old_hour_slots(start, end):
    """Ancien validation_checks._hour_slots (copie fidele, pour la non-regression)."""
    slots = []
    t = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    end = end.astimezone(UTC)
    while t <= end:
        s = ((t.weekday() + 1) % 7, t.hour)
        if s not in slots:
            slots.append(s)
        t += timedelta(hours=1)
    return slots


def _random_dates(n, seed=0):
    rng = random.Random(seed)
    base = datetime(2024, 1, 1, tzinfo=UTC)
    return [base + timedelta(seconds=rng.randrange(0, 3 * 365 * 86400),
                             microseconds=rng.randrange(0, 10 ** 6)) for _ in range(n)]


# --- mode reel : aucune regression ------------------------------------------------

def test_mode_par_defaut_reel():
    assert season.MODE == season.REEL
    assert season.describe() == {"mode": "reel"}


def test_mode_reel_identique_a_l_ancien_calcul_sur_10000_dates():
    for dt in _random_dates(10000):
        assert season.slot(dt, season.REEL) == _old_slot(dt)
        naive = dt.replace(tzinfo=None)
        assert season.slot(naive, season.REEL) == _old_slot(naive)


def test_mode_reel_jours_convention_postgresql():
    # dimanche 07/01/2024 ... samedi 13/01/2024 -> 0, 1, ..., 6
    days = [season.slot(datetime(2024, 1, 7 + i, 12, tzinfo=UTC), season.REEL)[0] for i in range(7)]
    assert days == [0, 1, 2, 3, 4, 5, 6]


def test_mode_reel_fuseau_converti_en_utc():
    # dimanche 07/01 00:30 a UTC+1 = samedi 06/01 23:30 UTC
    assert season.slot(datetime(2024, 1, 7, 0, 30, tzinfo=PLUS1), season.REEL) == (6, 23)


def test_slots_between_reel_identique_a_l_ancien():
    rng = random.Random(1)
    for start in _random_dates(500, seed=2):
        end = start + timedelta(seconds=rng.randrange(0, 6 * 3600))
        assert season.slots_between(start, end, season.REEL) == _old_hour_slots(start, end)


def test_sql_mode_reel_texte_historique():
    assert season.sql_slot("time", season.REEL) == (
        "EXTRACT(DOW FROM time)::smallint", "EXTRACT(HOUR FROM time)::smallint")


def test_baseline_job_sql_mode_reel_inchange():
    sql = baseline_job.build_upsert_sql("p99_ms", season.REEL)
    assert "    EXTRACT(DOW FROM time)::smallint                    AS dow,\n" in sql
    assert "    EXTRACT(HOUR FROM time)::smallint                   AS hour_bucket,\n" in sql
    assert "GROUP BY endpoint_id, dow, hour_bucket" in sql and "HAVING COUNT(*) >= 10" in sql


# --- mode compresse ------------------------------------------------------------

@pytest.mark.parametrize("hms,heure", [
    ((0, 0, 0), 0), ((0, 4, 59), 0), ((0, 5, 0), 1), ((1, 55, 0), 23), ((1, 59, 59), 23),
    ((2, 0, 0), 0), ((13, 37, 0), 19), ((23, 59, 59), 23),
])
def test_mode_compresse_valeurs_connues(hms, heure):
    assert season.slot(datetime(2026, 10, 25, *hms, tzinfo=UTC), season.COMPRESSE) == (0, heure)


def test_mode_compresse_jour_toujours_0_et_independant_du_fuseau():
    for dt in _random_dates(5000, seed=3):
        d, h = season.slot(dt, season.COMPRESSE)
        assert d == 0 and 0 <= h < season.SLOTS_PER_DAY
        assert season.slot(dt.astimezone(PLUS1), season.COMPRESSE) == (d, h)


def test_mode_compresse_chaque_heure_simulee_dure_5_minutes():
    t0 = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)          # heure paire : debut de journee
    seen = [season.slot(t0 + timedelta(seconds=s), season.COMPRESSE)[1] for s in range(0, 7200, 60)]
    assert seen == [m // 5 for m in range(120)]


def test_mode_compresse_date_sans_fuseau_refusee():
    with pytest.raises(ValueError, match="fuseau"):
        season.slot(datetime(2026, 10, 25, 1, 0), season.COMPRESSE)


def test_slots_between_compresse_pas_de_5_minutes_et_changement_de_journee():
    start = datetime(2026, 10, 25, 1, 52, tzinfo=UTC)
    end = start + timedelta(minutes=14)                   # 01:52 -> 02:06
    assert season.slots_between(start, end, season.COMPRESSE) == [(0, 22), (0, 23), (0, 0), (0, 1)]


def test_sql_mode_compresse():
    dow, hour = season.sql_slot("time", season.COMPRESSE)
    assert dow == "0::smallint"
    assert "EXTRACT(EPOCH FROM time)" in hour and "7200" in hour and "/ 300" in hour


def test_baseline_job_sql_mode_compresse():
    sql = baseline_job.build_upsert_sql("rps", season.COMPRESSE)
    assert "0::smallint                    AS dow," in sql and "FLOOR(MOD(" in sql
    assert "EXTRACT(DOW" not in sql and "EXTRACT(HOUR" not in sql


# --- reglage, melange de modes ---------------------------------------------------

def test_mode_depuis_l_environnement():
    assert season.mode_from_env({}) == season.REEL
    assert season.mode_from_env({"SEASON_MODE": ""}) == season.REEL
    assert season.mode_from_env({"SEASON_MODE": " Compresse "}) == season.COMPRESSE
    with pytest.raises(ValueError, match="SEASON_MODE invalide"):
        season.mode_from_env({"SEASON_MODE": "accelere"})


def test_mode_inconnu_refuse():
    with pytest.raises(ValueError):
        season.slot(datetime(2026, 1, 1, tzinfo=UTC), "hebdo")


def test_lignes_d_un_autre_mode_detectees():
    reel = [{"dow": 3, "hour_bucket": 14}]
    compresse = [{"dow": 0, "hour_bucket": 23}]
    assert season.rows_mode_reasons(reel, season.COMPRESSE)
    assert season.rows_mode_reasons(compresse, season.COMPRESSE) == []
    assert season.rows_mode_reasons(reel, season.REEL) == []
    assert season.rows_mode_reasons([{"dow": 0, "hour_bucket": 30}], season.REEL)


def test_meme_mode():
    assert season.same_mode(None, {"mode": "reel"})
    assert not season.same_mode(None, season.describe(season.COMPRESSE))
    assert season.same_mode(season.describe(season.COMPRESSE), season.describe(season.COMPRESSE))


# --- une seule horloge pour tous les composants ------------------------------------

@pytest.mark.parametrize("mode", [season.REEL, season.COMPRESSE])
def test_meme_case_pour_entrainement_controles_et_detection(monkeypatch, mode):
    monkeypatch.setattr(season, "MODE", mode)
    for dt in _random_dates(2000, seed=4):
        key = season.slot(dt)
        exact = {("E", m, *key): (1.0, 2.0, 3.0) for m in train_model.BASELINE_METRICS}
        got = train_model.baseline_for(exact, {}, "E", dt)
        assert all(v == (1.0, 2.0, 3.0) for v in got.values())       # case exacte trouvee
        assert vc._hour_slots(dt, dt) == [key]
