"""Sondering efter fler Prenly-läsare hos Bonnier News.

Uppmätt 2026-08-22: 52 av 84 varumärkesdomäner har en läsare på
etidning.<domän>, tillsammans 463 titlar. Prenumerationen är global, men
varje sajt måste läggas till för sig - Dalademokraten upptäcktes av en
slump och gav tillgång till Expressen.
"""

import pytest
import requests

from prenly_dl import discover
from prenly_dl.errors import PrenlyError


class _Svar:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status
        self.ok = status < 400

    def raise_for_status(self):
        if not self.ok:
            raise requests.RequestException(str(self.status_code))


SIDA = """
  <a href="https://www.dn.se">DN</a>
  <a href="https://expressen.se">Expressen</a>
  <a href="https://www.bonniernews.se/om-oss">Om oss</a>
  <a href="https://boldprinting.se">Bold</a>
"""


def test_bonniers_egna_bolagssidor_sorteras_bort():
    class _Http:
        def get(self, url, **kwargs):
            return _Svar(SIDA)

    assert discover.fetch_brand_domains(_Http()) == ["dn.se", "expressen.se"]


def test_otillganglig_varumarkessida_ger_begripligt_fel():
    class _Http:
        def get(self, url, **kwargs):
            raise requests.RequestException("nere")

    with pytest.raises(PrenlyError, match="Kunde inte läsa"):
        discover.fetch_brand_domains(_Http())


def test_doman_utan_lasare_ger_inget_fynd(monkeypatch):
    def _ingen_nyckel(session, url):
        raise PrenlyError("Hittade ingen klientnyckel")

    monkeypatch.setattr(discover, "fetch_client_key", _ingen_nyckel)
    # Det vanliga svaret: 32 av 84 domäner saknade läsare.
    assert discover.probe_site(None, "breakit.se") is None


def test_titlarna_hamtas_utan_inloggning(monkeypatch):
    """En anonym session räcker, så sonderingen kostar inga inloggningar."""
    monkeypatch.setattr(discover, "fetch_client_key", lambda s, url: "NYCKEL")
    monkeypatch.setattr(discover, "create_session", lambda s, url, k: "anonym")
    monkeypatch.setattr(discover, "get_context_token", lambda s, c: "kontext")
    monkeypatch.setattr(
        discover, "get_titles",
        lambda s, c, t: [
            {"id": 259, "name": "Expressen", "user_readable": True},
            {"id": 1, "name": "Bilaga", "user_readable": False},
        ],
    )

    fynd = discover.probe_site(None, "dn.se")

    assert fynd.url == "https://etidning.dn.se"
    assert fynd.antal == 2
    assert fynd.titlar[0] == {"id": 259, "name": "Expressen", "readable": True}
    assert fynd.fel == ""


def test_lasare_som_inte_svarar_tas_med_med_felet(monkeypatch):
    """Den går att lägga till för hand, så den ska inte döljas."""
    monkeypatch.setattr(discover, "fetch_client_key", lambda s, url: "NYCKEL")

    def _spricker(*args, **kwargs):
        raise PrenlyError("svarade 503")

    monkeypatch.setattr(discover, "create_session", _spricker)

    fynd = discover.probe_site(None, "dn.se")
    assert fynd is not None
    assert fynd.antal == 0
    assert "503" in fynd.fel


def test_sonderingen_pausar_mellan_anropen(monkeypatch):
    """Sonderingen rör någon annans servrar."""
    pauser = []
    monkeypatch.setattr(discover.time, "sleep", lambda s: pauser.append(s))
    monkeypatch.setattr(discover, "fetch_brand_domains", lambda s: ["a.se", "b.se"])
    monkeypatch.setattr(discover, "probe_site", lambda s, d: None)

    discover.discover(None)

    assert pauser == [discover.PAUS, discover.PAUS]
    assert discover.PAUS > 0


# --------------------------------------------------------------------------
# Jobbet och vyn
# --------------------------------------------------------------------------


def _klient(temp_env, monkeypatch):
    from fastapi.testclient import TestClient
    import prenly_dl.web.app as app_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
    return TestClient(app_module.create_app())


def _fynd(doman, antal, titlar=None):
    f = discover.Fynd(doman=doman, url=f"https://etidning.{doman}", klientnyckel="k")
    f.titlar = titlar or [
        {"id": i, "name": f"Titel {i}", "readable": True} for i in range(antal)
    ]
    return f


def test_jobbet_sparar_fynden_och_markerar_de_kanda(db, temp_env, monkeypatch):
    from prenly_dl import services
    from prenly_dl.db import repository as repo
    from prenly_dl.db.models import Job, JobStatus

    with db() as session:
        repo.upsert_site(session, "https://etidning.dn.se", {"textalk-auth": "k", "auth": "t"})
        job = repo.enqueue_discover(session)
        job_id = job.id

    monkeypatch.setattr(
        services, "discover",
        lambda http, progress=None: [_fynd("dn.se", 3), _fynd("sydsvenskan.se", 17)],
    )

    with db() as session:
        services.execute_job(session, repo.claim_next_job(session))

    with db() as session:
        assert session.get(Job, job_id).status == JobStatus.DONE.value
        assert "2 sajter med läsare, 1 ej tillagda" in session.get(Job, job_id).message
        import json
        poster = json.loads(repo.get_setting(session, services.DISCOVER_KEY, "[]"))

    # Störst först, och den vi redan har är märkt.
    assert [p["doman"] for p in poster] == ["sydsvenskan.se", "dn.se"]
    assert [p["kand"] for p in poster] == [False, True]


def test_vyn_visar_bara_sajter_som_inte_ar_tillagda(db, temp_env, monkeypatch):
    import json
    from prenly_dl import services
    from prenly_dl.db import repository as repo

    with db() as session:
        repo.upsert_site(session, "https://etidning.dn.se", {"textalk-auth": "k", "auth": "t"})
        repo.set_setting(session, services.DISCOVER_KEY, json.dumps([
            {"doman": "dn.se", "url": "https://etidning.dn.se", "antal": 3,
             "titlar": ["A"], "fel": "", "kand": True, "lasbara": 3},
            {"doman": "sydsvenskan.se", "url": "https://etidning.sydsvenskan.se",
             "antal": 17, "titlar": ["Sydsvenskan"], "fel": "", "kand": False, "lasbara": 0},
        ]))
        session.commit()

    with _klient(temp_env, monkeypatch) as client:
        text = client.get("/credentials").text

    assert "sydsvenskan.se" in text
    assert "1 sajter som inte är tillagda" in text
    # Den tillagda ska inte erbjudas igen.
    assert text.count('value="https://etidning.dn.se"') == 0


def test_lagg_till_kraver_att_nagon_sajt_har_uppgifter(db, temp_env, monkeypatch):
    """Uppgifterna lånas från en inloggad sajt: samma konto gäller överallt."""
    from prenly_dl.db import repository as repo

    with db() as session:
        repo.upsert_site(session, "https://a.se", {"textalk-auth": "k", "auth": "t"})
        session.commit()

    with _klient(temp_env, monkeypatch) as client:
        svar = client.post(
            "/credentials/discover/add",
            data={"url": "https://etidning.sydsvenskan.se"},
            follow_redirects=True,
        )
    assert "Logga in på en sajt först" in svar.text


def test_lagg_till_anvander_sparade_uppgifter(db, temp_env, monkeypatch):
    from prenly_dl.db import repository as repo
    import prenly_dl.web.routes as routes_module

    with db() as session:
        site = repo.upsert_site(session, "https://etidning.dn.se",
                                {"textalk-auth": "k", "auth": "t"})
        site.username, site.password = "hej@example.com", "hemligt"
        session.commit()

    sedda = {}

    def _login_site(session, url, username, password):
        sedda["url"] = url
        sedda["username"] = username
        return repo.upsert_site(session, url, {"textalk-auth": "k", "auth": "ny"})

    monkeypatch.setattr(routes_module, "login_site", _login_site)
    monkeypatch.setattr(routes_module, "verify_site", lambda s, site: __import__(
        "prenly_dl.db.models", fromlist=["AuthStatus"]).AuthStatus.OK)

    with _klient(temp_env, monkeypatch) as client:
        svar = client.post(
            "/credentials/discover/add",
            data={"url": "https://etidning.sydsvenskan.se"},
            follow_redirects=True,
        )

    assert sedda["url"] == "https://etidning.sydsvenskan.se"
    assert sedda["username"] == "hej@example.com"
    assert "La till" in svar.text
