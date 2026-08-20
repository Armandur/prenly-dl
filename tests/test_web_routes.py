import pytest
from fastapi.testclient import TestClient

from prenly_dl.db import repository as repo


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


def _skapa_publikation(namn="Tidningen", enabled=True):
    from prenly_dl.db.session import get_session

    with get_session() as session:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
        site.auth_status = "ok"
        publication, _ = repo.upsert_publication(session, site, 4019, namn)
        publication.enabled = enabled
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


def test_forhandsgranska_knappen_serverar_pdf_utan_nedladdningsjobb(
    client, temp_env, monkeypatch
):
    import prenly_dl.web.routes as routes_module
    from prenly_dl.db.session import get_session

    publication_id, slug = _skapa_publikation()
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        issue, _ = repo.upsert_issue(
            session, publication, {"uid": "preview-9", "name": "2026-01-01"}
        )
        issue_id = issue.id

    preview = temp_env / "preview.pdf"

    def _bygg(creds, title_id, uid, preview_issue_id, prefix):
        assert (title_id, uid, preview_issue_id) == (4019, "preview-9", issue_id)
        preview.write_bytes(b"%PDF-1.4\n%%EOF")
        return preview

    monkeypatch.setattr(routes_module, "build_issue_preview", _bygg)
    monkeypatch.setattr(routes_module, "ensure_catalogue_depth", lambda *args: 0)

    page = client.get(f"/publications/{slug}")
    assert f'data-preview="/issues/{issue_id}/preview"' in page.text

    # Bygget är skilt från öppnandet: POST bygger och svarar med adressen,
    # så att knappen kan visa en spinner och öppna fliken först när filen
    # finns.
    byggd = client.post(f"/issues/{issue_id}/preview")
    assert byggd.status_code == 200
    assert byggd.json() == {"url": f"/issues/{issue_id}/preview"}

    response = client.get(f"/issues/{issue_id}/preview")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.content == b"%PDF-1.4\n%%EOF"
    assert client.get("/api/jobs").json() == []


def test_forhandsgranskning_som_misslyckas_ger_besked(client, monkeypatch):
    import prenly_dl.web.routes as routes_module
    from prenly_dl.db.session import get_session
    from prenly_dl.errors import PrenlyAuthError

    publication_id, slug = _skapa_publikation()
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        issue, _ = repo.upsert_issue(session, publication, {"uid": "1", "name": "x"})
        issue_id = issue.id

    def _spricker(*args):
        raise PrenlyAuthError("Inte autentiserad.")

    monkeypatch.setattr(routes_module, "build_issue_preview", _spricker)

    svar = client.post(f"/issues/{issue_id}/preview")
    assert svar.status_code == 502
    # Knappen visar felet, så det måste följa med i svaret.
    assert "Inte autentiserad." in svar.json()["error"]


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
    assert "media_host" in response.text


def alla_varden(html: str) -> list[str]:
    """Alla titlar formuläret skickar, även de som inte ska aktiveras."""
    import html as htmlmod
    import re

    return [
        htmlmod.unescape(m)
        for m in re.findall(r'name="alla" value="([^"]+)"', html)
    ]


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
    assert len(alla_varden(svar.text)) == 3
    assert len(valbara(svar.text)) == 2


def test_utan_titellista_anvands_det_inklistrade(client_utan_api):
    svar = client_utan_api.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    assert "Kunde inte hämta publikationslistan" in svar.text
    assert "Inte autentiserad" in svar.text
    # Blocken som klistrades in går ändå att spara.
    assert len(valbara(svar.text)) == 2


def test_granskning_markerar_befintliga(client):
    granskning = client.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    client.post(
        "/uppgifter/spara",
        data={"valda": valbara(granskning.text), "alla": alla_varden(granskning.text)},
        follow_redirects=True,
    )
    svar = client.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    assert "Finns redan" in svar.text


def test_text_utan_konfiguration_ger_besked(client):
    svar = client.post(
        "/uppgifter/granska", data={"klistrat": "bara brus"}, follow_redirects=True
    )
    assert "Hittade ingen konfiguration" in svar.text


def test_alla_titlar_sparas_men_bara_de_valda_aktiveras(client):
    """Katalogen ska bli komplett; bevakning är ett aktivt val."""
    granskning = client.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    varden = valbara(granskning.text)
    alla = alla_varden(granskning.text)
    assert len(varden) == 2
    assert len(alla) == 3

    import re

    olasbara = re.findall(r'name="olasbar" value="([^"]+)"', granskning.text)
    assert olasbara, "titeln utan läsrätt ska skickas med som sådan"
    svar = client.post(
        "/uppgifter/spara",
        data={"valda": varden[0], "alla": alla, "olasbar": olasbara},
        follow_redirects=True,
    )
    assert "1 sajt sparad" in svar.text
    assert "1 aktiverade" in svar.text

    from prenly_dl.db import repository as repo_module
    from prenly_dl.db.session import get_session

    with get_session() as session:
        # Uppgifterna skrivs en gång, inte en kopia per titel.
        assert len(repo_module.list_sites(session)) == 1
        publikationer = repo_module.list_publications(session)
        assert len(publikationer) == 3
        assert sum(1 for p in publikationer if p.enabled) == 1
        # Titeln utan läsrätt är känd men inte bevakad.
        utan = next(p for p in publikationer if p.title_id == 9999)
        assert utan.readable is False
        assert utan.enabled is False


def test_sparar_utan_nagot_alls_ger_besked(client):
    svar = client.post("/uppgifter/spara", data={}, follow_redirects=True)
    assert "Inget att spara" in svar.text


def test_bara_uppgifter_utan_att_aktivera_nagot(client):
    """Att förnya ett token ska inte kräva att man kryssar i något."""
    from prenly_dl.db import repository as repo_module
    from prenly_dl.db.session import get_session

    granskning = client.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    svar = client.post(
        "/uppgifter/spara",
        data={"alla": alla_varden(granskning.text)},
        follow_redirects=True,
    )
    assert "1 sajt sparad" in svar.text

    with get_session() as session:
        assert repo_module.list_sites(session)[0].auth == "def"
        # Titlarna är kända men ingenting bevakas.
        publikationer = repo_module.list_publications(session)
        assert len(publikationer) == 3
        assert not any(p.enabled for p in publikationer)


def test_fornyelse_lamnar_befintlig_bevakning_ifred(client):
    """En ny token ska inte stänga av det man redan bevakar."""
    from prenly_dl.db import repository as repo_module
    from prenly_dl.db.session import get_session

    granskning = client.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    client.post(
        "/uppgifter/spara",
        data={"valda": valbara(granskning.text), "alla": alla_varden(granskning.text)},
        follow_redirects=True,
    )
    with get_session() as session:
        aktiva_fore = {p.title_id for p in repo_module.list_publications(session) if p.enabled}
    assert aktiva_fore

    # Förnya utan att kryssa i något.
    ny = KONSOLUTSKRIFT.replace('"auth": "def"', '"auth": "farsk"')
    granskning2 = client.post("/uppgifter/granska", data={"klistrat": ny})
    svar = client.post(
        "/uppgifter/spara",
        data={"alla": alla_varden(granskning2.text)},
        follow_redirects=True,
    )
    assert "bevakningen oförändrad" in svar.text.lower()

    with get_session() as session:
        assert repo_module.list_sites(session)[0].auth == "farsk"
        aktiva_efter = {p.title_id for p in repo_module.list_publications(session) if p.enabled}
    assert aktiva_efter == aktiva_fore


def test_fornyade_uppgifter_galler_alla_sajtens_titlar(client):
    granskning = client.post("/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT})
    client.post(
        "/uppgifter/spara",
        data={"valda": valbara(granskning.text), "alla": alla_varden(granskning.text)},
        follow_redirects=True,
    )

    ny_token = KONSOLUTSKRIFT.replace('"auth": "def"', '"auth": "helt-ny-token"')
    granskning2 = client.post("/uppgifter/granska", data={"klistrat": ny_token})
    client.post(
        "/uppgifter/spara",
        data={"valda": valbara(granskning2.text), "alla": alla_varden(granskning2.text)},
        follow_redirects=True,
    )

    from prenly_dl.db import repository as repo_module
    from prenly_dl.db.session import get_session

    with get_session() as session:
        # En rad att förnya, och den gäller alla titlar under sajten.
        assert len(repo_module.list_sites(session)) == 1
        assert repo_module.list_sites(session)[0].auth == "helt-ny-token"
        assert {site.auth for site in repo_module.list_sites(session)} == {"helt-ny-token"}


ETT_BLOCK = """{
  "credentials": {"site": "https://etidning.dn.se", "cdn": "", "textalk-auth": "ny", "auth": "ny"},
  "publication": {"title": 3121, "limit": 1},
  "prefix": "Kundtidning DN - ",
  "folder": "Kundtidning DN"
}"""


def test_ett_enskilt_block_fungerar(client_utan_api):
    svar = client_utan_api.post("/uppgifter/granska", data={"klistrat": ETT_BLOCK})
    assert "1 publikation" in svar.text
    assert len(valbara(svar.text)) == 1


def test_ett_block_fornyar_hela_sajten(client_utan_api):
    granskning = client_utan_api.post(
        "/uppgifter/granska", data={"klistrat": KONSOLUTSKRIFT}
    )
    client_utan_api.post(
        "/uppgifter/spara",
        data={"valda": valbara(granskning.text), "alla": alla_varden(granskning.text)},
        follow_redirects=True,
    )

    # Ett block räcker för att förnya uppgifterna för sajtens alla titlar.
    granskning2 = client_utan_api.post("/uppgifter/granska", data={"klistrat": ETT_BLOCK})
    assert "Finns redan" in granskning2.text
    client_utan_api.post(
        "/uppgifter/spara",
        data={"valda": valbara(granskning2.text), "alla": alla_varden(granskning2.text)},
        follow_redirects=True,
    )

    from prenly_dl.db import repository as repo_module
    from prenly_dl.db.session import get_session

    with get_session() as session:
        assert len(repo_module.list_sites(session)) == 1
        assert repo_module.list_sites(session)[0].auth == "ny"
        # Bilagan rördes aldrig i det andra blocket men fick ändå nya
        # uppgifter, eftersom de hör till sajten.
        assert repo_module.list_sites(session)[0].auth == "ny"


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




def test_inklistringsytan_kommer_forst_pa_uppgiftssidan(client):
    svar = client.get("/uppgifter")
    assert svar.status_code == 200
    assert 'id="klistra"' in svar.text
    assert 'hx-post="/uppgifter/granska"' in svar.text
    # Ytan man kommer för ska ligga före instruktionerna och koden.
    assert svar.text.index('id="klistra"') < svar.text.index("Så här får du tag i dem")
    assert svar.text.index('id="klistra"') < svar.text.index('id="snutt"')


def test_gamla_lagg_till_sidan_ar_borta(client):
    """Ett gammalt bokmärke landar på översikten, inte på ett fel."""
    svar = client.get("/publications/new", follow_redirects=False)
    assert svar.status_code == 303
    assert svar.headers["location"] == "/"


BARA_UPPGIFTER = """{
  "credentials": {
    "site": "https://etidning.dn.se",
    "cdn": "",
    "textalk-auth": "klient",
    "auth": "sessionstoken"
  }
}"""


def test_bara_uppgifter_racker(client):
    """Konsolsnutten skickar inga publikationer - de hämtas från API:t."""
    svar = client.post("/uppgifter/granska", data={"klistrat": BARA_UPPGIFTER})
    assert svar.status_code == 200
    assert "2 publikationer" in svar.text
    assert "Kundtidning DN" in svar.text
    assert "Bilagan" in svar.text


def test_bara_uppgifter_sparar_sajten(client):
    granskning = client.post("/uppgifter/granska", data={"klistrat": BARA_UPPGIFTER})
    svar = client.post(
        "/uppgifter/spara",
        data={
            "valda": valbara(granskning.text),
            "alla": alla_varden(granskning.text),
        },
        follow_redirects=True,
    )
    assert "1 sajt sparad" in svar.text

    from prenly_dl.db import repository as repo_module
    from prenly_dl.db.session import get_session

    with get_session() as session:
        sajter = repo_module.list_sites(session)
        assert len(sajter) == 1
        assert sajter[0].auth == "sessionstoken"
        assert sajter[0].textalk_auth == "klient"
        # Alla titlar från API:t hamnade i katalogen.
        assert len(repo_module.list_publications(session)) == 3


def test_uppgifter_utan_titellista_sparar_anda_sajten(client_utan_api):
    """Går API:t inte att nå ska uppgifterna ändå gå att lägga in."""
    granskning = client_utan_api.post(
        "/uppgifter/granska", data={"klistrat": BARA_UPPGIFTER}
    )
    assert "Kunde inte hämta publikationslistan" in granskning.text

    svar = client_utan_api.post(
        "/uppgifter/spara",
        data={"alla": [BARA_UPPGIFTER], "valda": [BARA_UPPGIFTER]},
        follow_redirects=True,
    )
    assert "1 sajt sparad" in svar.text

    from prenly_dl.db import repository as repo_module
    from prenly_dl.db.session import get_session

    with get_session() as session:
        assert len(repo_module.list_sites(session)) == 1


def test_valj_alla_finns_i_fragmentet(client):
    """Rutan och dess script måste följa med det htmx swappar in."""
    svar = client.post(
        "/uppgifter/granska",
        data={"klistrat": KONSOLUTSKRIFT},
        headers={"HX-Request": "true"},
    )
    assert 'id="valj-alla"' in svar.text
    # Scriptet ligger i fragmentet, annars körs det aldrig efter en swap.
    assert "indeterminate" in svar.text
    # Låsta titlar ska undantas från både räkning och kryssning.
    assert 'input[name="valda"]:not([disabled])' in svar.text


def test_sparande_koar_synk_sa_omslagen_finns_direkt(client):
    """Katalogen ska ha omslag utan att man ber om det."""
    granskning = client.post("/uppgifter/granska", data={"klistrat": BARA_UPPGIFTER})
    svar = client.post(
        "/uppgifter/spara",
        data={"valda": valbara(granskning.text), "alla": alla_varden(granskning.text)},
        follow_redirects=True,
    )
    assert "hämtar omslag i bakgrunden" in svar.text.lower()

    jobb = client.get("/api/jobs").json()
    assert [j["type"] for j in jobb] == ["site_sync"]
    assert jobb[0]["status"] == "queued"


def test_publikationens_knappar_stannar_kvar(client, monkeypatch):
    """Testa uppgifter tog förut användaren till katalogen."""
    import prenly_dl.web.routes as routes_module

    monkeypatch.setattr(routes_module, "verify_site", lambda session, site: None)
    publication_id, slug = _skapa_publikation()

    svar = client.post(
        f"/publications/{publication_id}/verify", headers={"HX-Request": "true"}
    )
    assert svar.status_code == 200
    assert 'id="pub-detalj"' in svar.text
    assert "<!doctype html>" not in svar.text.lower()
    assert "kortbesked" in svar.text

    # Utan JavaScript: tillbaka till publikationen, inte till katalogen.
    svar = client.post(f"/publications/{publication_id}/verify", follow_redirects=False)
    assert svar.status_code == 303
    assert svar.headers["location"] == f"/publications/{slug}"


def test_katalogkontroll_uppdaterar_utgavorna(client, monkeypatch):
    import prenly_dl.web.routes as routes_module

    monkeypatch.setattr(
        routes_module, "poll_publication",
        lambda session, publication: {"found": 3, "new": 1, "queued": 1},
    )
    publication_id, slug = _skapa_publikation()

    svar = client.post(
        f"/publications/{publication_id}/poll", headers={"HX-Request": "true"}
    )
    assert svar.status_code == 200
    assert "3 utgåvor i katalogen, 1 nya, 1 köade" in svar.text
    # Utgåvetabellen ska hämtas om, den kan ha fått nya rader.
    assert svar.headers.get("HX-Trigger") == "utgavor-andrade"


def test_nedladdning_svarar_med_utgavesektionen(client):
    """Knappen ska byta ut tabellen på plats, inte ladda om sidan."""
    from prenly_dl.db.session import get_session

    publication_id, slug = _skapa_publikation()
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        issue, _ = repo.upsert_issue(
            session, publication, {"uid": "9", "name": "2026-01-01"}
        )
        issue_id = issue.id

    svar = client.post(
        f"/issues/{issue_id}/download",
        data={"page": 1},
        headers={"HX-Request": "true"},
    )
    assert svar.status_code == 200
    assert 'id="utgavor"' in svar.text
    assert "<!doctype html>" not in svar.text.lower()
    assert "Köade nedladdning av 2026-01-01" in svar.text
    # Något är på gång, så tabellen ska hålla sig uppdaterad.
    assert "every 5s" in svar.text


def test_hamtad_utgava_saknar_ladda_ner_knappen(client):
    """Ladda ner gör ingenting på en hämtad utgåva - visa bara Tvinga om."""
    from prenly_dl.db.models import IssueStatus
    from prenly_dl.db.session import get_session

    publication_id, slug = _skapa_publikation()
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        klar, _ = repo.upsert_issue(session, publication, {"uid": "1", "name": "klar"})
        klar.status = IssueStatus.DONE.value
        ny, _ = repo.upsert_issue(session, publication, {"uid": "2", "name": "ny"})
        session.add(klar)

    text = client.get(f"/publications/{slug}").text
    rad_klar = text[text.index("klar"):text.index("klar") + 1400]
    rad_ny = text[text.index(">ny<") if ">ny<" in text else text.index("ny"):][:1400]
    assert "Tvinga om" in rad_klar
    assert "Ladda ner" not in rad_klar.split("Tvinga om")[0]
    assert "Ladda ner" in rad_ny


def test_hamtad_fil_kan_oppnas_fran_utgaveraden(temp_env, client):
    from prenly_dl.db.models import IssueStatus
    from prenly_dl.db.session import get_session

    publication_id, slug = _skapa_publikation()
    mapp = temp_env / "output" / "Tidningen"
    mapp.mkdir(parents=True)
    fil = mapp / "Tidningen - 2026-01-01.pdf"
    fil.write_bytes(b"%PDF-1.4 test")

    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        publication.folder = "Tidningen"
        issue, _ = repo.upsert_issue(
            session, publication, {"uid": "1", "name": "2026-01-01"}
        )
        issue.status = IssueStatus.DONE.value
        issue.file_path = str(fil)
        session.add_all([publication, issue])

    text = client.get(f"/publications/{slug}").text
    assert "/library/file/Tidningen/Tidningen%20-%202026-01-01.pdf" in text

    # En fil som tagits bort utanför prenly-dl ska inte länkas.
    fil.unlink()
    assert "/library/file/Tidningen" not in client.get(f"/publications/{slug}").text


def test_hamtad_utgava_saknar_forhandsgranskning(client):
    """Filen finns redan och filnamnet är en länk till den."""
    from prenly_dl.db.models import IssueStatus
    from prenly_dl.db.session import get_session

    publication_id, slug = _skapa_publikation()
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        klar, _ = repo.upsert_issue(session, publication, {"uid": "1", "name": "hamtad"})
        klar.status = IssueStatus.DONE.value
        ny, _ = repo.upsert_issue(session, publication, {"uid": "2", "name": "ohamtad"})
        session.add(klar)
        session.flush()
        klar_id, ny_id = klar.id, ny.id

    text = client.get(f"/publications/{slug}").text
    assert f'data-preview="/issues/{ny_id}/preview"' in text
    assert f'data-preview="/issues/{klar_id}/preview"' not in text


def _publikation_med_utgavor(antal=3, hamtade=0):
    from prenly_dl.db.models import IssueStatus
    from prenly_dl.db.session import get_session

    publication_id, slug = _skapa_publikation()
    ids = []
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        for i in range(antal):
            issue, _ = repo.upsert_issue(
                session, publication.title, {"uid": str(i), "name": f"2026-01-{i + 1:02d}"}
            )
            if i < hamtade:
                issue.status = IssueStatus.DONE.value
                session.add(issue)
            session.flush()
            ids.append(issue.id)
    return publication_id, slug, ids


def test_batch_koar_flera_utgavor(client):
    publication_id, slug, ids = _publikation_med_utgavor(3)
    svar = client.post(
        f"/publications/{publication_id}/issues/batch",
        data={"valda": [str(i) for i in ids], "page": 1},
        headers={"HX-Request": "true"},
    )
    assert svar.status_code == 200
    assert "Köade 3 nedladdningar" in svar.text
    assert len(client.get("/api/jobs").json()) == 3


def test_batch_hoppar_over_redan_hamtade(client):
    """Ladda ner på en hämtad utgåva skulle ändå svara att filen fanns."""
    publication_id, slug, ids = _publikation_med_utgavor(3, hamtade=2)
    svar = client.post(
        f"/publications/{publication_id}/issues/batch",
        data={"valda": [str(i) for i in ids], "page": 1},
        headers={"HX-Request": "true"},
    )
    assert "Köade 1 nedladdning" in svar.text
    assert "2 redan hämtade" in svar.text
    assert len(client.get("/api/jobs").json()) == 1


def test_batch_tvinga_om_tar_aven_hamtade(client):
    publication_id, slug, ids = _publikation_med_utgavor(3, hamtade=2)
    svar = client.post(
        f"/publications/{publication_id}/issues/batch",
        data={"valda": [str(i) for i in ids], "force": "on", "page": 1},
        headers={"HX-Request": "true"},
    )
    assert "Köade 3 nedladdningar" in svar.text


def test_batch_tar_bara_utgavor_i_ratt_publikation(client):
    """En tillrättalagd post ska inte kunna köa jobb i en annan publikation."""
    from prenly_dl.db.session import get_session

    publication_id, slug, ids = _publikation_med_utgavor(2)
    with get_session() as session:
        site = repo.find_site(session, "https://a.se")
        annan_titel, _ = repo.upsert_title_access(session, site, 5555, "Annan")
        annan_utgava, _ = repo.upsert_issue(
            session, annan_titel, {"uid": "x", "name": "2026-02-01"}
        )
        session.flush()
        frammande_id = annan_utgava.id

    svar = client.post(
        f"/publications/{publication_id}/issues/batch",
        data={"valda": [str(ids[0]), str(frammande_id)], "page": 1},
        headers={"HX-Request": "true"},
    )
    assert "Köade 1 nedladdning" in svar.text
    jobb = client.get("/api/jobs").json()
    assert len(jobb) == 1


def test_batch_utan_markering_ger_besked(client):
    publication_id, slug, ids = _publikation_med_utgavor(2)
    svar = client.post(
        f"/publications/{publication_id}/issues/batch",
        data={"page": 1},
        headers={"HX-Request": "true"},
    )
    assert "Inga utgåvor markerade" in svar.text
    assert client.get("/api/jobs").json() == []
