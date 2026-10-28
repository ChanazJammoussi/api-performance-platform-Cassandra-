"""
scraper.py : taux d'erreurs 4xx/5xx (audit du 27/10). Aucune requete reseau
(requests.post remplace), aucune base (connexion factice). La semantique PromQL est testee
par promtool : tests/promql/scraper_error_rate.test.yml (memes requetes, verifie ci-dessous).
"""
import re
import types
from pathlib import Path

import pytest

import scraper

PROMTOOL = Path(__file__).parent / "promql" / "scraper_error_rate.test.yml"

S5 = 'calls_total{span_kind="SPAN_KIND_SERVER", http_status_code=~"5.."}'
S4 = 'calls_total{span_kind="SPAN_KIND_SERVER", http_status_code=~"4.."}'
SA = 'calls_total{span_kind="SPAN_KIND_SERVER"}'


def _appels(s):
    return (f"sum without(http_status_code, status_code) "
            f"((({s} - last_over_time({s}[6h] offset 5m)) >= 0) or {s})")


ERR5 = f"100 * {_appels(S5)} / {_appels(SA)}"
ERR4 = f"100 * {_appels(S4)} / {_appels(SA)}"


def _post(monkeypatch, effet):
    monkeypatch.setattr(scraper.requests, "post", effet)


def test_query_succes_sans_resultat_renvoie_dict_vide(monkeypatch):
    _post(monkeypatch, lambda *a, **k: types.SimpleNamespace(
        json=lambda: {"status": "success", "data": {"result": []}}))
    assert scraper.query_prometheus("x") == {}


def test_query_refusee_renvoie_none(monkeypatch):
    _post(monkeypatch, lambda *a, **k: types.SimpleNamespace(
        json=lambda: {"status": "error", "error": "parse error"}))
    assert scraper.query_prometheus("x") is None


def test_query_exception_renvoie_none(monkeypatch):
    def boom(*a, **k):
        raise scraper.requests.ConnectionError("refus")
    _post(monkeypatch, boom)
    assert scraper.query_prometheus("x") is None


def test_requetes_d_erreurs_exactes(monkeypatch):
    monkeypatch.setattr(scraper, "query_prometheus", lambda promql: {"requete": promql})
    *_, e5, e4 = scraper.fetch_features()
    assert e5 == {"requete": ERR5} and e4 == {"requete": ERR4}


def test_echec_des_requetes_latences_vides_erreurs_none(monkeypatch):
    monkeypatch.setattr(scraper, "query_prometheus", lambda promql: None)
    assert scraper.fetch_features() == ({}, {}, {}, {}, None, None)


def test_promtool_utilise_les_requetes_du_scraper():
    exprs = re.findall(r"^\s+- expr: '(.*)'$", PROMTOOL.read_text(encoding="utf-8"), re.M)
    assert set(exprs) == {ERR5, _appels(S5), _appels(SA)}


class _Conn:
    def __init__(self):
        self.lignes, self.commits = [], 0

    def cursor(self):
        conn = self

        class _Cur:
            def execute(self, sql, row):
                conn.lignes.append(row)

            def close(self):
                pass
        return _Cur()

    def commit(self):
        self.commits += 1


def _tous(valeur):
    return {(ep["service"], ep["route"]): valeur for ep in scraper.ENDPOINTS}


def _ecrire(e5, e4):
    conn = _Conn()
    scraper.write_features(conn, "t", _tous(10.0), _tous(20.0), _tous(30.0), _tous(1.0), e5, e4)
    return conn


@pytest.mark.parametrize("e5,e4", [(None, {}), ({}, None)])
def test_echec_requete_erreurs_aucune_ligne_jamais_zero(e5, e4, caplog):
    conn = _ecrire(e5, e4)
    assert conn.lignes == [] and conn.commits == 0
    assert "Error-rate query failed" in caplog.text


def test_requete_reussie_valeur_reportee_sinon_zero():
    conn = _ecrire({("orders", "/orders"): 1.57}, {})
    assert len(conn.lignes) == len(scraper.ENDPOINTS) and conn.commits == 1
    assert {l[1]: (l[7], l[8]) for l in conn.lignes if l[7] or l[8]} == {"GET /orders": (1.57, 0.0)}
