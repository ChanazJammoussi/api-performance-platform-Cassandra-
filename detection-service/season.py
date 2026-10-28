"""
season.py -- horloge saisonniere unique de la baseline (spec 5.3).

Toutes les cases (dow, hour_bucket) d'endpoint_baseline sont calculees ici, pour
la baseline (SQL), le detecteur, l'entrainement, l'explication, les controles et
l'evaluation : une seule definition, jamais deux conventions.

Deux modes (variable d'environnement SEASON_MODE) :
  reel      (defaut) : jour de semaine PostgreSQL (0 = dimanche) x heure UTC.
                       Comportement historique, inchange.
  compresse          : "journee compressee" de load.js. Une journee simulee dure
                       CYCLE_MINUTES (120) minutes reelles et commence a chaque
                       heure paire UTC ; une heure simulee = SLOT_MINUTES (5)
                       minutes reelles. hour_bucket = heure simulee (0..23) ;
                       dow vaut toujours 0 (load.js rejoue la meme journee : pas
                       de saisonnalite hebdomadaire). Spec 5.3 : "In the demo the
                       week is the compressed seasonality cycle of the load
                       generator".

Bibliotheque standard uniquement (importable depuis l'hote, comme validation_checks).
Un SEASON_MODE invalide leve ValueError a l'import : le service s'arrete au
demarrage au lieu de calculer avec une horloge inconnue.
"""

import math
import os
from datetime import datetime, timedelta, timezone

from baseline_utils import pg_dow

REEL, COMPRESSE = "reel", "compresse"
MODES = (REEL, COMPRESSE)

CYCLE_MINUTES = 120       # duree d'une journee simulee (= stages de k6/load.js)
SLOT_MINUTES = 5          # duree d'une heure simulee
SLOTS_PER_DAY = CYCLE_MINUTES // SLOT_MINUTES   # 24
_CYCLE_S = CYCLE_MINUTES * 60
_SLOT_S = SLOT_MINUTES * 60


def mode_from_env(env=None):
    """Mode lu dans SEASON_MODE (defaut reel). Valeur inconnue -> ValueError."""
    env = os.environ if env is None else env
    raw = env.get("SEASON_MODE", "")
    value = raw.strip().lower() or REEL
    if value not in MODES:
        raise ValueError(f"SEASON_MODE invalide : {raw!r} (attendu : {', '.join(MODES)})")
    return value


MODE = mode_from_env()


def _mode(mode):
    m = MODE if mode is None else mode
    if m not in MODES:
        raise ValueError(f"mode saisonnier inconnu : {m!r}")
    return m


def describe(mode=None):
    """Description du mode, inscrite dans les manifestes et les modeles."""
    m = _mode(mode)
    if m == REEL:
        return {"mode": REEL}
    return {"mode": COMPRESSE, "cycle_minutes": CYCLE_MINUTES, "slot_minutes": SLOT_MINUTES}


def _require_aware(dt):
    if not isinstance(dt, datetime):
        raise ValueError(f"datetime attendu, recu {type(dt).__name__}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"date sans fuseau horaire ({dt.isoformat()}) : mode compresse impossible")
    return dt


def _cycle_offset_s(dt):
    """Secondes ecoulees depuis le debut de la journee simulee (heure paire UTC)."""
    return math.floor(_require_aware(dt).timestamp()) % _CYCLE_S


def slot(dt, mode=None):
    """
    Case (dow, hour_bucket) d'une mesure prise a l'instant dt.
      reel      : (jour PostgreSQL, heure) en UTC si dt a un fuseau ; une date sans
                  fuseau est prise telle quelle (comportement historique).
      compresse : (0, heure simulee 0..23) ; date sans fuseau refusee (ValueError).
    """
    m = _mode(mode)
    if m == REEL:
        if dt.tzinfo is not None and dt.utcoffset() is not None:
            dt = dt.astimezone(timezone.utc)
        return pg_dow(dt), dt.hour
    return 0, _cycle_offset_s(dt) // _SLOT_S


def slots_between(start, end, mode=None):
    """
    Cases (dow, hour_bucket) touchees par [start, end], dans l'ordre, sans doublon.
      reel      : pas d'une heure UTC (ancien validation_checks._hour_slots).
      compresse : pas d'une heure simulee (5 minutes reelles).
    """
    m = _mode(mode)
    _require_aware(start)
    _require_aware(end)
    step = timedelta(hours=1) if m == REEL else timedelta(seconds=_SLOT_S)
    end = end.astimezone(timezone.utc)
    t = start.astimezone(timezone.utc)
    if m == REEL:
        t = t.replace(minute=0, second=0, microsecond=0)
    else:
        t = t.replace(microsecond=0) - timedelta(seconds=_cycle_offset_s(t) % _SLOT_S)
    slots = []
    while t <= end:
        s = slot(t, m)
        if s not in slots:
            slots.append(s)
        t += step
    return slots


def sql_slot(column="time", mode=None):
    """
    Expressions SQL (dow, hour_bucket) equivalentes a slot() pour une colonne
    timestamptz (baseline_job.py). En mode reel : texte historique inchange.
    """
    m = _mode(mode)
    if m == REEL:
        return (f"EXTRACT(DOW FROM {column})::smallint",
                f"EXTRACT(HOUR FROM {column})::smallint")
    return ("0::smallint",
            f"FLOOR(MOD(FLOOR(EXTRACT(EPOCH FROM {column}))::numeric, {_CYCLE_S}) "
            f"/ {_SLOT_S})::smallint")


def rows_mode_reasons(rows, mode=None):
    """
    Raisons pour lesquelles des lignes endpoint_baseline ne peuvent pas venir du
    mode donne (melange de modes). Mode compresse : dow doit valoir 0 et
    hour_bucket etre dans 0..23. Mode reel : dow 0..6 et heure 0..23.
    """
    m = _mode(mode)
    bad = []
    for r in rows:
        try:
            d, h = int(r["dow"]), int(r["hour_bucket"])
        except (KeyError, TypeError, ValueError):
            bad.append(r)
            continue
        if m == COMPRESSE and (d != 0 or not 0 <= h < SLOTS_PER_DAY):
            bad.append(r)
        elif m == REEL and (not 0 <= d <= 6 or not 0 <= h <= 23):
            bad.append(r)
    if not bad:
        return []
    return [f"baseline : {len(bad)} ligne(s) incompatible(s) avec le mode saisonnier "
            f"{m!r} (melange de modes ?)"]


def same_mode(a, b):
    """Deux descriptions de mode sont-elles identiques ? (absence = reel)."""
    norm = lambda d: describe(REEL) if d is None else d
    return norm(a) == norm(b)
