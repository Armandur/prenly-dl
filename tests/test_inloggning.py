"""Inloggning med användarnamn och lösenord.

Uppgifter lades tidigare till genom att köra en konsolsnutt i en inloggad
läsare och klistra in tokens. De går ut, och då tystnade hämtningarna
tills någon klistrade in nya. Uppmätt mot Prenly 2026-08-22: inloggningen
sker i tre steg, och alla tre går att göra utan webbläsare.
"""

import pytest
import requests

from prenly_dl import api
from prenly_dl.errors import PrenlyAuthError, PrenlyError


class _Svar:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text or ""
        self.ok = status < 400

    def json(self):
        if self._data is None:
            raise ValueError("inte json")
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise requests.RequestException(f"status {self.status_code}")


class _Http:
    """Fejkad session som svarar efter url."""

    def __init__(self, svar: dict):
        self.svar = svar
        self.anrop = []

    def get(self, url, **kwargs):
        self.anrop.append(("GET", url, kwargs))
        return self.svar[url]

    def post(self, url, **kwargs):
        self.anrop.append(("POST", url, kwargs))
        return self.svar[url]


SIDA = 'var x = {"client":{"authToken":"NYCKELN123"},"media_host":"cdn"};'
SESSIONS = "https://content.textalk.se/api/web-reader/v1/sessions"
LOGIN = SESSIONS + "/authentications/username"


def test_klientnyckeln_hamtas_ur_lasarens_sida():
    http = _Http({"https://etidning.dn.se/": _Svar(text=SIDA)})
    assert api.fetch_client_key(http, "https://etidning.dn.se") == "NYCKELN123"


def test_saknad_klientnyckel_ger_begripligt_fel():
    http = _Http({"https://etidning.dn.se/": _Svar(text="<html>inget här</html>")})
    with pytest.raises(PrenlyError, match="Hittade ingen klientnyckel"):
        api.fetch_client_key(http, "https://etidning.dn.se")


def test_sessionen_skapas_med_klientnyckeln():
    http = _Http({SESSIONS: _Svar(201, {"token": "anonym"})})
    assert api.create_session(http, "https://etidning.dn.se", "NYCKELN123") == "anonym"
    _, _, kwargs = http.anrop[0]
    headers = kwargs["headers"]
    assert headers["X-Textalk-Content-Client-Authorize"] == "NYCKELN123"
    assert headers["X-Textalk-Content-Product-Type"] == "webapp"
    # Origin måste vara läsarens adress, annars svarar API:t inte.
    assert headers["Origin"] == "https://etidning.dn.se"
    # Ingen Bearer före inloggning: sessionen finns ju inte än.
    assert "Authorization" not in headers


def test_inloggningen_skickar_bearer_och_formularfalt():
    http = _Http({LOGIN: _Svar(200, {"token": "inloggad"})})
    ny = api.login(http, "https://etidning.dn.se", "NYCKELN123", "anonym", "u", "p")
    assert ny == "inloggad"
    _, _, kwargs = http.anrop[0]
    assert kwargs["headers"]["Authorization"] == "Bearer anonym"
    assert kwargs["data"] == {"username": "u", "password": "p", "ask_polite": "true"}


def test_svar_utan_ny_token_behaller_sessionen():
    """Servern kan logga in den befintliga sessionen i stället för att byta."""
    http = _Http({LOGIN: _Svar(200, {})})
    assert api.login(http, "https://etidning.dn.se", "N", "anonym", "u", "p") == "anonym"


def test_fel_losenord_ger_authfel_med_serverns_besked():
    """Uppmätt svar: 400 med INVALID_LOGIN_CREDENTIALS."""
    http = _Http({LOGIN: _Svar(400, {"message": "Invalid login credentials.",
                                     "code": "INVALID_LOGIN_CREDENTIALS"})})
    with pytest.raises(PrenlyAuthError, match="Invalid login credentials"):
        api.login(http, "https://etidning.dn.se", "N", "anonym", "u", "fel")


def test_losenordet_skickas_aldrig_i_url():
    http = _Http({LOGIN: _Svar(200, {"token": "t"})})
    api.login(http, "https://etidning.dn.se", "N", "anonym", "u", "hemligt")
    _, url, kwargs = http.anrop[0]
    assert "hemligt" not in url
    assert "hemligt" not in str(kwargs.get("headers"))


# --------------------------------------------------------------------------
# Sparade uppgifter och automatisk återinloggning
# --------------------------------------------------------------------------


def test_inloggning_sparar_uppgifterna_pa_sajten(db, temp_env, monkeypatch):
    from prenly_dl import services
    from prenly_dl.db import repository as repo

    monkeypatch.setattr(services, "fetch_client_key", lambda http, site: "NYCKEL")
    monkeypatch.setattr(services, "create_session", lambda http, site, key: "anonym")
    monkeypatch.setattr(
        services, "login",
        lambda http, site, key, token, u, p: "inloggad",
    )

    with db() as session:
        site = services.login_site(session, "etidning.dn.se", "u", "hemligt")

        # Adressen normaliseras: https läggs på och avslutande slash tas bort.
        assert site.url == "https://etidning.dn.se"
        assert site.textalk_auth == "NYCKEL"
        assert site.auth == "inloggad"
        # Uppgifterna sparas för att kunna logga in igen.
        assert site.username == "u" and site.password == "hemligt"
        assert len(repo.list_sites(session)) == 1


def test_utgangen_session_loggar_in_igen_automatiskt(db, temp_env, monkeypatch):
    """Utan det tystnar hämtningarna tills någon klistrar in en ny token."""
    from prenly_dl import services
    from prenly_dl.db import repository as repo
    from prenly_dl.db.models import AuthStatus

    with db() as session:
        site = repo.upsert_site(
            session, "https://etidning.dn.se",
            {"textalk-auth": "gammal", "auth": "utgangen"},
        )
        site.username, site.password = "u", "hemligt"
        session.flush()

        forsok = {"n": 0}

        def _kontexttoken(http, creds):
            forsok["n"] += 1
            if creds.auth == "utgangen":
                raise PrenlyAuthError("Inte autentiserad.")
            return "kontext"

        monkeypatch.setattr(services, "get_context_token", _kontexttoken)
        monkeypatch.setattr(services, "get_titles", lambda *a: [])
        monkeypatch.setattr(services, "fetch_client_key", lambda http, s: "ny-nyckel")
        monkeypatch.setattr(services, "create_session", lambda http, s, k: "anonym")
        monkeypatch.setattr(services, "login", lambda http, s, k, t, u, p: "fars-token")

        status = services.verify_site(session, site)

    assert forsok["n"] == 2, "första försöket ska följas av ett efter inloggning"
    assert site.auth == "fars-token"
    assert site.textalk_auth == "ny-nyckel"
    assert status is not AuthStatus.AUTH_ERROR


def test_utan_sparat_losenord_blir_det_authfel_som_forr(db, temp_env, monkeypatch):
    from prenly_dl import services
    from prenly_dl.db import repository as repo
    from prenly_dl.db.models import AuthStatus

    with db() as session:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "utgangen"}
        )
        session.flush()

        def _spricker(http, creds):
            raise PrenlyAuthError("Inte autentiserad.")

        monkeypatch.setattr(services, "get_context_token", _spricker)

        def _far_inte_koras(*args, **kwargs):
            raise AssertionError("inloggning försöktes utan sparade uppgifter")

        monkeypatch.setattr(services, "fetch_client_key", _far_inte_koras)

        assert services.verify_site(session, site) is AuthStatus.AUTH_ERROR


def test_rutten_kraver_alla_tre_falten(temp_env, monkeypatch):
    from fastapi.testclient import TestClient
    import prenly_dl.web.app as app_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)

    with TestClient(app_module.create_app()) as client:
        for data in ({"url": "", "username": "u", "password": "p"},
                     {"url": "https://a.se", "username": "", "password": "p"},
                     {"url": "https://a.se", "username": "u", "password": ""}):
            svar = client.post("/credentials/login", data=data, follow_redirects=True)
            assert "Fyll i adress, användarnamn och lösenord" in svar.text


def test_rutten_visar_serverns_besked_vid_fel_losenord(temp_env, monkeypatch):
    from fastapi.testclient import TestClient
    import prenly_dl.web.app as app_module
    import prenly_dl.web.routes as routes_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)

    def _nekas(session, url, username, password):
        raise PrenlyAuthError("Invalid login credentials.")

    monkeypatch.setattr(routes_module, "login_site", _nekas)

    with TestClient(app_module.create_app()) as client:
        svar = client.post(
            "/credentials/login",
            data={"url": "https://a.se", "username": "u", "password": "fel"},
            follow_redirects=True,
        )
    assert "Inloggningen nekades" in svar.text
    assert "Invalid login credentials" in svar.text


def test_inloggning_nollstaller_inte_sajtens_cdn(db, temp_env, monkeypatch):
    """Två av sajterna har en egen cdn, och utan den går media inte att hämta."""
    from prenly_dl import services
    from prenly_dl.db import repository as repo

    monkeypatch.setattr(services, "fetch_client_key", lambda http, s: "NY")
    monkeypatch.setattr(services, "create_session", lambda http, s, k: "anonym")
    monkeypatch.setattr(services, "login", lambda http, s, k, t, u, p: "inloggad")

    with db() as session:
        repo.upsert_site(
            session, "https://etidning.dalademokraten.se",
            {"textalk-auth": "gammal", "auth": "gammal",
             "cdn": "https://1123185085.rsc.cdn77.org"},
        )
        session.flush()

        site = services.login_site(
            session, "https://etidning.dalademokraten.se", "u", "p"
        )

    assert site.cdn == "https://1123185085.rsc.cdn77.org"
    assert site.auth == "inloggad"
