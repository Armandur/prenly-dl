import pytest
from fastapi.testclient import TestClient

from prenly_dl.db import repository as repo
from prenly_dl.db.models import Publication


# Titlarna som den fejkade sajten svarar med. Bilagan är inte läsbar, så
# listan täcker både det som går att lägga till och det som inte gör det.
FEJKADE_TITLAR = [
    {"id": 3121, "name": "Kundtidning DN", "user_readable": True},
    {"id": 3181, "name": "Bilagan", "user_readable": True},
    {"id": 9999, "name": "Utan läsrätt", "user_readable": False},
]


@pytest.fixture
def client(temp_env, monkeypatch):
    # Schemaläggaren ska inte starta i testerna.
    import prenly_dl.web.app as app_module
    import prenly_dl.web.routes as routes_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
    # Inga nätverksanrop i testerna.
    monkeypatch.setattr(routes_module, "discover_titles", lambda creds: FEJKADE_TITLAR)
    with TestClient(app_module.create_app()) as test_client:
        yield test_client


@pytest.fixture
def client_utan_api(temp_env, monkeypatch):
    """Klient där titellistningen misslyckas, så fallbacken kan testas."""
    import prenly_dl.web.app as app_module
    import prenly_dl.web.routes as routes_module
    from prenly_dl.errors import PrenlyAuthError

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)

    def _spricker(creds):
        raise PrenlyAuthError("Inte autentiserad.")

    monkeypatch.setattr(routes_module, "discover_titles", _spricker)
    with TestClient(app_module.create_app()) as test_client:
        yield test_client


def _skapa_publikation(namn="Tidningen"):
    from prenly_dl.db.session import get_session

    with get_session() as session:
        publication = Publication(
            title_id=4019, name=namn, slug=repo.unique_slug(session, namn),
            site="https://a.se", textalk_auth="k", auth="t",
        )
        session.add(publication)
        session.flush()
        return publication.id, publication.slug


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_oversikten_listar_publikationer(client):
    _skapa_publikation("Tidningen Hemslöjd")
    response = client.get("/")
    assert response.status_code == 200
    assert "Tidningen Hemslöjd" in response.text


def test_publikationssidan_visar_uppgifter(client):
    _, slug = _skapa_publikation()
    response = client.get(f"/publications/{slug}")
    assert response.status_code == 200
    assert "Utgåvor" in response.text


def test_okand_publikation_leder_till_oversikten(client):
    response = client.get("/publications/finns-inte", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_skapa_publikation_fran_json(client):
    conf = """{
      "credentials": {"site": "https://b.se", "textalk-auth": "k", "auth": "t"},
      "publication": {"title": 123, "limit": 2},  # kommentar
      "prefix": "Bladet - ", "folder": "Bladet"
    }"""
    response = client.post("/publications", data={"conf_json": conf}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/publications/bladet"

    from prenly_dl.db.session import get_session

    with get_session() as session:
        publication = repo.get_publication_by_slug(session, "bladet")
        assert publication.title_id == 123
        assert publication.watch_limit == 2


def test_trasig_json_ger_felmeddelande(client):
    response = client.post("/publications", data={"conf_json": "{trasig"}, follow_redirects=True)
    assert "Kunde inte tolka JSON" in response.text


def test_koa_nedladdning_skapar_jobb(client):
    publication_id, slug = _skapa_publikation()
    from prenly_dl.db.session import get_session

    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        issue, _ = repo.upsert_issue(session, publication, {"uid": "9", "name": "2026-01-01"})
        issue_id = issue.id

    response = client.post(f"/issues/{issue_id}/download", data={"force": "on"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/publications/{slug}"

    jobs = client.get("/api/jobs").json()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"


def test_installningar_avvisar_trasigt_cron(client):
    response = client.post("/settings", data={"poll_cron": "varje dag", "history_days": 30},
                           follow_redirects=True)
    assert "Ogiltigt cron-uttryck" in response.text


def test_installningar_sparar_giltigt_cron(client):
    response = client.post("/settings", data={"poll_cron": "30 5 * * *", "history_days": 14},
                           follow_redirects=True)
    assert "Inställningarna sparade" in response.text
    assert 'value="30 5 * * *"' in response.text


def test_losenordsskydd_omdirigerar_till_inloggning(temp_env, monkeypatch):
    monkeypatch.setenv("PRENLY_PASSWORD", "hemligt")
    import prenly_dl.web.app as app_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
    with TestClient(app_module.create_app()) as test_client:
        response = test_client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

        assert test_client.post("/login", data={"password": "fel"},
                                follow_redirects=False).headers["location"] == "/login"
        test_client.post("/login", data={"password": "hemligt"}, follow_redirects=False)
        assert test_client.get("/").status_code == 200


def test_htmx_polling_far_204_i_stallet_for_inloggningssidan(temp_env, monkeypatch):
    """En utloggad htmx-poll ska inte swappa in inloggningsformuläret."""
    monkeypatch.setenv("PRENLY_PASSWORD", "hemligt")
    import prenly_dl.web.app as app_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
    with TestClient(app_module.create_app()) as test_client:
        response = test_client.get(
            "/stats/cards", headers={"HX-Request": "true"}, follow_redirects=False
        )
        assert response.status_code == 204
        assert "password" not in response.text.lower()


def test_uppgiftssidan_visar_konsolsnutten(client):
    response = client.get("/uppgifter")
    assert response.status_code == 200
    # Sidan ska visa den riktiga snutten, inte en beskrivning av den.
    assert "PrenlyData" in response.text
    assert "prenlyreadersessiontoken" in response.text
    assert "prenlyKonfig" in response.text


def valbara(html: str) -> list[str]:
    """Värdena på de kryssrutor en webbläsare faktiskt skickar.

    Titlar utan läsrätt renderas som disabled och följer inte med posten.
    """
    import html as htmlmod
    import re

    return [
        htmlmod.unescape(m.group(1))
        for m in re.finditer(r'name="valda" value="([^"]+)"([^>]*)>', html)
        if "disabled" not in m.group(2)
    ]


KONSOLUTSKRIFT = """
prenly-dl 13 publikationer på https://etidning.dn.se, 2 läsbara med det här kontot.

--- Kundtidning DN (titel 3121) ---
{
  "credentials": {"site": "https://etidning.dn.se", "cdn": "", "textalk-auth": "abc", "auth": "def"},
  "publication": {"title": 3121, "limit": 1},
  "prefix": "Kundtidning DN - ",
  "folder": "Kundtidning DN"
}

--- Bilagan (titel 3181) ---
{
  "credentials": {"site": "https://etidning.dn.se", "cdn": "", "textalk-auth": "abc", "auth": "def"},
  "publication": {"title": 3181, "limit": 1},
  "prefix": "Bilagan - ",
  "folder": "Bilagan"
}
Kör prenlyKonfig() för att kopiera den första.
"""


def test_listan_byggs_fran_titellistan_inte_fran_blocken(client):
    """Ett block ska räcka: resten hämtas från Prenly."""
    svar = client.post("/uppgifter/granska", data={"klistrat": ETT_BLOCK})
    assert svar.status_code == 200
    # Bilagan fanns inte i det inklistrade blocket men i titellistan.
    assert "Bilagan" in svar.text
    assert "2 publikationer" in svar.text
    assert "Utan läsrätt" in svar.text
    assert "Ingen läsrätt" in svar.text


def test_granskning_hittar_alla_konfigurationer_i_utskriften(client):
    svar = client.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    assert svar.status_code == 200
    assert "2 publikationer" in svar.text
    assert "Kundtidning DN" in svar.text
    assert "Bilagan" in svar.text
    # Titellistan har tre poster, varav en utan läsrätt som inte går att välja.
    assert svar.text.count('name="valda"') == 3
    assert svar.text.count("disabled") == 1


def test_utan_titellista_anvands_det_inklistrade(client_utan_api):
    svar = client_utan_api.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    assert "Kunde inte hämta publikationslistan" in svar.text
    assert "Inte autentiserad" in svar.text
    # Blocken som klistrades in går ändå att spara.
    assert svar.text.count('name="valda"') == 2


def test_granskning_markerar_befintliga(client):
    client.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    client.post("/publications", data={"conf_json": KONSOLUTSKRIFT.split("---")[2]
                                       .split("Bilagan")[0].strip()})
    svar = client.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    assert "Finns redan" in svar.text
    assert "Ny" in svar.text


def test_text_utan_konfiguration_ger_besked(client):
    svar = client.post(
        "/uppgifter/granska", data={"klistrat": "bara brus"}, follow_redirects=True
    )
    assert "Hittade ingen konfiguration" in svar.text


def test_sparar_bara_de_valda(client):
    granskning = client.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    varden = valbara(granskning.text)
    assert len(varden) == 2

    svar = client.post(
        "/uppgifter/spara", data={"valda": varden[0]}, follow_redirects=True
    )
    assert "1 tillagda" in svar.text

    from prenly_dl.db import repository as repo_module
    from prenly_dl.db.session import get_session

    with get_session() as session:
        assert len(repo_module.list_publications(session)) == 1


def test_sparar_utan_val_ger_besked(client):
    svar = client.post("/uppgifter/spara", data={}, follow_redirects=True)
    assert "Inga publikationer valda" in svar.text


def test_omsparning_uppdaterar_uppgifterna(client):
    granskning = client.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    client.post(
        "/uppgifter/spara",
        data={"valda": valbara(granskning.text)},
        follow_redirects=True,
    )

    ny_token = KONSOLUTSKRIFT.replace('"auth": "def"', '"auth": "helt-ny-token"')
    granskning2 = client.post("/uppgifter/granska", data={"klistrat": ny_token})
    svar = client.post(
        "/uppgifter/spara",
        data={"valda": valbara(granskning2.text)},
        follow_redirects=True,
    )
    assert "2 uppdaterade med nya uppgifter" in svar.text

    from prenly_dl.db import repository as repo_module
    from prenly_dl.db.session import get_session

    with get_session() as session:
        for publication in repo_module.list_publications(session):
            assert publication.auth == "helt-ny-token"


ETT_BLOCK = """{
  "credentials": {"site": "https://etidning.dn.se", "cdn": "", "textalk-auth": "ny", "auth": "ny"},
  "publication": {"title": 3121, "limit": 1},
  "prefix": "Kundtidning DN - ",
  "folder": "Kundtidning DN"
}"""


def test_ett_enskilt_block_fungerar(client_utan_api):
    svar = client_utan_api.post("/uppgifter/granska", data={"klistrat": ETT_BLOCK})
    assert "1 publikation" in svar.text
    assert svar.text.count('name="valda"') == 1


def test_ett_block_fornyar_befintliga_uppgifter(client_utan_api):
    granskning = client_utan_api.post(
        "/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT}
    )
    client_utan_api.post(
        "/uppgifter/spara",
        data={"valda": valbara(granskning.text)},
        follow_redirects=True,
    )

    # Bara ett block: den publikationen ska kännas igen och uppdateras.
    granskning2 = client_utan_api.post("/uppgifter/granska", data={"klistrat": ETT_BLOCK})
    assert "Finns redan" in granskning2.text
    svar = client_utan_api.post(
        "/uppgifter/spara",
        data={"valda": valbara(granskning2.text)},
        follow_redirects=True,
    )
    assert "1 uppdaterade med nya uppgifter" in svar.text

    from prenly_dl.db import repository as repo_module
    from prenly_dl.db.session import get_session

    with get_session() as session:
        publikationer = {p.title_id: p for p in repo_module.list_publications(session)}
        # Bara den i blocket ska ha rörts.
        assert publikationer[3121].auth == "ny"
        assert publikationer[3181].auth == "def"
        assert len(publikationer) == 2


def test_granskning_via_htmx_ger_bara_fragmentet(client):
    svar = client.post(
        "/uppgifter/granska",
        data={"klistrat": KONSOLUTSKRIFT},
        headers={"HX-Request": "true"},
    )
    assert svar.status_code == 200
    assert 'id="fynd"' in svar.text
    assert "2 publikationer" in svar.text
    # Fragmentet ska inte dra med sig hela sidan igen.
    assert "<!doctype html>" not in svar.text.lower()
    assert "Klistra in resultatet" not in svar.text


def test_htmx_fel_visas_i_fragmentet_utan_omdirigering(client):
    svar = client.post(
        "/uppgifter/granska",
        data={"klistrat": "bara brus"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert svar.status_code == 200
    assert "Hittade ingen konfiguration" in svar.text
    assert 'id="fynd"' in svar.text


def test_granskning_utan_javascript_ger_hela_sidan(client):
    svar = client.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    assert "<!doctype html>" in svar.text.lower()
    assert "Klistra in resultatet" in svar.text
    assert "2 publikationer" in svar.text


def test_inklistringsytan_finns_pa_lagg_till_sidan(client):
    svar = client.get("/publications/new")
    assert svar.status_code == 200
    assert 'id="klistra"' in svar.text
    assert 'hx-post="/uppgifter/granska"' in svar.text


def test_flera_publikationer_gar_lika_bra_fran_lagg_till_sidan(client):
    svar = client.post(
        "/uppgifter/granska",
        data={"klistrat": KONSOLUTSKRIFT},
        headers={"HX-Request": "true", "Referer": "http://test/publications/new"},
    )
    assert "2 publikationer" in svar.text


def test_utan_javascript_renderas_sidan_man_kom_ifran(client):
    fran_ny = client.post(
        "/uppgifter/granska",
        data={"klistrat": KONSOLUTSKRIFT},
        headers={"Referer": "http://test/publications/new"},
    )
    assert "Fyll i för hand" in fran_ny.text
    assert "2 publikationer" in fran_ny.text

    fran_uppgifter = client.post(
        "/uppgifter/granska",
        data={"klistrat": KONSOLUTSKRIFT},
        headers={"Referer": "http://test/uppgifter"},
    )
    assert "PrenlyData" in fran_uppgifter.text
    assert "2 publikationer" in fran_uppgifter.text
