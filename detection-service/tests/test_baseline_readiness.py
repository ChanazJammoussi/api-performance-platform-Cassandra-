"""
Verdict "baseline PRETE / PAS PRETE" (baseline_readiness.assess) sur des journees
SYNTHETIQUES : chaque critere a un cas pret et un cas pas pret. Le vrai
k6/load.js est lu (lecture seule) pour le profil attendu ; aucune base.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import baseline_readiness as br
import season

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[2]
LOAD_JS = REPO / "k6" / "load.js"
B0 = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)               # heure paire UTC
ENDPOINTS = ["GET /orders", "POST /payments"]
METRICS = ["p99_ms", "rps", "error_rate_5xx"]


@pytest.fixture(autouse=True)
def compresse(monkeypatch):
    monkeypatch.setattr(season, "MODE", season.COMPRESSE)


def profile():
    return br.expected_slot_profile(*br.parse_k6_stages(LOAD_JS.read_text(encoding="utf-8")))


def cycles(n=7, offset_s=20, code=0):
    return [{"cycle": i + 1, "boundary": B0 + timedelta(hours=2 * i),
             "start": B0 + timedelta(hours=2 * i, seconds=offset_s),
             "end": B0 + timedelta(hours=2 * i + 2, seconds=30), "exit_code": code} for i in range(n)]


def times(n_cycles=7, gap=None):
    end = B0 + timedelta(hours=2 * n_cycles, seconds=30)
    out, t = {}, B0 + timedelta(seconds=br.WARMUP_S)
    pts = []
    while t <= end:
        if not (gap and gap[0] <= t <= gap[1]):
            pts.append(t)
        t += timedelta(seconds=62)
    for ep in ENDPOINTS:
        out[ep] = list(pts)
    return out


def rows(samples=33, prof=None, skip=(), extra=()):
    prof = profile() if prof is None else prof
    out = []
    for ep in ENDPOINTS:
        for m in METRICS:
            for h in range(24):
                if (ep, m, h) in skip:
                    continue
                if m == "rps":
                    p10, p50, p90 = prof[h] * 0.5, prof[h] * 0.6, prof[h] * 0.7
                elif m == "p99_ms":
                    p10, p50, p90 = 80.0, 100.0, 150.0
                else:
                    p10 = p50 = p90 = 0.0                       # aucune erreur : bande degeneree
                out.append({"endpoint_id": ep, "metric": m, "dow": 0, "hour_bucket": h,
                            "p10": p10, "p50": p50, "p90": p90, "sample_count": samples})
    return out + list(extra)


def verdict(**over):
    kw = dict(cycles=cycles(), times_by_endpoint=times(), injections=[], rows=rows(),
              endpoints=ENDPOINTS, metrics=METRICS, profile=profile())
    kw.update(over)
    return br.assess(**kw)


def _has(res, fragment):
    return any(fragment in r for r in res["raisons"]), res["raisons"]


# --- profil de load.js -----------------------------------------------------------

def test_load_js_une_journee_de_2_heures():
    start, stages = br.parse_k6_stages(LOAD_JS.read_text(encoding="utf-8"))
    assert start == 5 and sum(d for d, _ in stages) == season.CYCLE_MINUTES * 60
    assert stages[-1][1] == start                     # fin de journee = debut : courbe continue


def test_profil_creux_la_nuit_pic_a_midi():
    p = profile()
    assert len(p) == 24
    assert max(p) == pytest.approx(50, abs=1) and min(p) < 10
    assert p.index(max(p)) in range(12, 17)
    # creux au debut de journee (lissage 5 min sur la fin de la journee precedente) ;
    # la descente 15 -> 5 utilisateurs des minutes 110-120 donne ~10 a l'heure 23.
    assert p.index(min(p)) == 0 and p[0] < 10 and p[23] < 15


def test_pearson():
    assert br.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert br.pearson([1, 1, 1], [1, 2, 3]) is None


# --- verdict ---------------------------------------------------------------------

def test_baseline_complete_prete():
    res = verdict()
    assert res["pret"], res["raisons"]
    assert all(c >= 0.99 for c in res["details"]["correlations"].values())
    assert any("degeneree" in i for i in res["infos"])           # taux d'erreur : signale seulement


def test_mode_reel_refuse(monkeypatch):
    monkeypatch.setattr(season, "MODE", season.REEL)
    assert _has(verdict(), "ce verdict concerne le mode compresse")[0]


@pytest.mark.parametrize("over,fragment", [
    ({"cycles": cycles(6)}, "6 terminee(s)"),
    ({"cycles": cycles(code=99)}, "code de sortie 99"),
    ({"cycles": cycles(offset_s=90)}, "demarrage a 90 s"),
    ({"cycles": cycles()[:3] + cycles(8)[4:]}, "non consecutif"),
])
def test_journees_k6(over, fragment):
    assert _has(verdict(**over), fragment)[0]


def test_journee_pas_sur_une_heure_paire():
    c = cycles()
    c[0] = dict(c[0], boundary=B0 + timedelta(minutes=30), start=B0 + timedelta(minutes=30, seconds=5))
    assert _has(verdict(cycles=c), "n'est pas une heure paire")[0]


def test_trou_dans_les_mesures():
    gap = (B0 + timedelta(hours=5), B0 + timedelta(hours=5, minutes=7))
    assert _has(verdict(times_by_endpoint=times(gap=gap)), "trou de")[0]


def test_endpoint_sans_mesure():
    t = times()
    del t["POST /payments"]
    assert _has(verdict(times_by_endpoint=t), "POST /payments : aucune mesure")[0]


def test_panne_injectee_pendant_la_collecte():
    inj = [(B0 + timedelta(hours=3), B0 + timedelta(hours=3, minutes=5))]
    assert _has(verdict(injections=inj), "panne(s) injectee(s)")[0]


def test_panne_hors_periode_acceptee():
    inj = [(B0 - timedelta(days=30), B0 - timedelta(days=30) + timedelta(minutes=5))]
    assert verdict(injections=inj)["pret"]


def test_case_manquante():
    res = verdict(rows=rows(skip={("GET /orders", "p99_ms", 7)}))
    assert _has(res, "143/144 cases presentes")[0]


def test_mesures_insuffisantes_par_case():
    assert _has(verdict(rows=rows(samples=10)), "moins de 25 mesures")[0]


def test_melange_de_modes():
    intrus = {"endpoint_id": "GET /orders", "metric": "p99_ms", "dow": 3, "hour_bucket": 14,
              "p10": 1.0, "p50": 2.0, "p90": 3.0, "sample_count": 40}
    assert _has(verdict(rows=rows(extra=[intrus])), "incompatible(s) avec le mode")[0]


def test_bande_de_latence_invalide():
    r = rows()
    r[0] = dict(r[0], p10=100.0, p90=100.0)
    assert _has(verdict(rows=r), "bande(s) invalide(s)")[0]


def test_forme_du_trafic_differente_de_load_js():
    plat = [10.0] * 12 + [11.0] * 12                       # aucun creux ni pic
    res = verdict(rows=rows(prof=plat))
    assert _has(res, "forme du trafic")[0]


# --- journal -----------------------------------------------------------------

def test_journal_lu():
    text = ("# cycle\theure_paire\tdebut\tfin\tcode\n"
            "1\t2026-10-25T00:00:00+00:00\t2026-10-25T00:00:20+00:00\t2026-10-25T02:00:30+00:00\t0\n")
    c = br.parse_journal(text)
    assert c[0]["cycle"] == 1 and c[0]["exit_code"] == 0 and c[0]["start"].tzinfo is not None


@pytest.mark.parametrize("line", [
    "1\t2026-10-25T00:00:00+00:00\t0\n",
    "1\t2026-10-25T00:00:00\t2026-10-25T00:00:20\t2026-10-25T02:00:30\t0\n",
    "x\t2026-10-25T00:00:00+00:00\t2026-10-25T00:00:20+00:00\t2026-10-25T02:00:30+00:00\t0\n",
])
def test_journal_mal_forme_refuse(line):
    with pytest.raises(ValueError):
        br.parse_journal(line)
