import pytest
from fastapi.testclient import TestClient

from prenly_dl import services
from prenly_dl.db import repository as repo
from prenly_dl.db.models import Publication
from prenly_dl.errors import PrenlyAuthError


@pytest.fixture
def publikation(db):
    with db() as session:
        publication = Publication(
            title_id=4019, name="Tidningen", slug="tidningen", site="https://a.se",
            textalk_auth="k", auth="t",
        )
        session.add(publication)
        session.flush()
        return publication.id


def _katalog(antal_totalt):
    """Fejkad katalog med `antal_totalt` utgåvor, nyast först."""
    poster = [
        {"uid": str(1000 + i), "name": f"utgava-{i:03d}",
         "publication_date": f"2026-{12 - (i % 12):02d}-01",
         "media": {"checksum": f"ck{i:03d}"}}
        for i in range(antal_totalt)
    ]

    def _hamta(http, creds, token, title_id, limit=50, offset=0):
        _hamta.anrop.append((limit, offset))
        return poster[offset:offset + limit]

    _hamta.anrop = []
    return _hamta


def _koppla(monkeypatch, katalog):
    monkeypatch.setattr(services, "get_catalogue_issues", katalog)
    monkeypatch.setattr(services, "get_context_token", lambda *a, **k: "token")
    monkeypatch.setattr(services, "build_session", lambda: None)
    monkeypatch.setattr(services, "fetch_and_cache_cover", lambda *a, **k: None)


def test_bladdring_hamtar_bara_det_som_saknas(db, publikation, monkeypatch):
    katalog = _katalog(76)
    _koppla(monkeypatch, katalog)

    with db() as session:
        publication = repo.get_publication(session, publikation)
        assert services.ensure_catalogue_depth(session, publication, 20) == 20
        assert repo.count_issues(session, publication.id) == 20
    assert katalog.anrop == [(20, 0)]

    # Sida två hämtar nästa bit, inte om den första.
    with db() as session:
        publication = repo.get_publication(session, publikation)
        assert services.ensure_catalogue_depth(session, publication, 40) == 20
    assert katalog.anrop == [(20, 0), (20, 20)]

    # Tillbaka till sida ett: allt finns redan, inga anrop.
    with db() as session:
        publication = repo.get_publication(session, publikation)
        assert services.ensure_catalogue_depth(session, publication, 20) == 0
    assert katalog.anrop == [(20, 0), (20, 20)]


def test_slut_pa_katalogen_markeras_och_slutar_anropa(db, publikation, monkeypatch):
    katalog = _katalog(25)
    _koppla(monkeypatch, katalog)

    with db() as session:
        publication = repo.get_publication(session, publikation)
        services.ensure_catalogue_depth(session, publication, 100)
        assert publication.catalogue_complete is True
        assert repo.count_issues(session, publication.id) == 25

    antal_anrop = len(katalog.anrop)
    with db() as session:
        publication = repo.get_publication(session, publikation)
        assert services.ensure_catalogue_depth(session, publication, 100) == 0
    assert len(katalog.anrop) == antal_anrop


def test_authfel_vid_bladdring_bubblar_upp(db, publikation, monkeypatch):
    _koppla(monkeypatch, _katalog(76))
    monkeypatch.setattr(
        services, "get_context_token",
        lambda *a, **k: (_ for _ in ()).throw(PrenlyAuthError("Inte autentiserad.")),
    )
    with db() as session:
        publication = repo.get_publication(session, publikation)
        with pytest.raises(PrenlyAuthError):
            services.ensure_catalogue_depth(session, publication, 20)


@pytest.fixture
def client(temp_env, monkeypatch):
    import prenly_dl.web.app as app_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
    with TestClient(app_module.create_app()) as test_client:
        yield test_client


def test_publikationssidan_paginerar(client, publikation, monkeypatch):
    _koppla(monkeypatch, _katalog(76))

    forsta = client.get("/publications/tidningen")
    assert forsta.status_code == 200
    assert "utgava-000" in forsta.text
    assert "utgava-020" not in forsta.text
    assert "Visar 1-20" in forsta.text

    andra = client.get("/publications/tidningen?page=2")
    assert "utgava-020" in andra.text
    assert "utgava-000" not in andra.text
    assert "Visar 21-40" in andra.text


def test_sidnummer_bortom_katalogen_backar_till_sista(client, publikation, monkeypatch):
    _koppla(monkeypatch, _katalog(25))
    response = client.get("/publications/tidningen?page=9")
    assert "Visar 21-25" in response.text
    assert "sida 2 av 2" in response.text
    # Ingen "Äldre"-knapp när katalogen är slut.
    assert "Äldre" not in response.text


def test_authfel_vid_bladdring_visar_besked_men_sidan_renderas(
    client, publikation, monkeypatch
):
    _koppla(monkeypatch, _katalog(76))
    monkeypatch.setattr(
        services, "get_context_token",
        lambda *a, **k: (_ for _ in ()).throw(PrenlyAuthError("Inte autentiserad.")),
    )
    response = client.get("/publications/tidningen", follow_redirects=True)
    assert response.status_code == 200
    assert "Kunde inte hämta äldre utgåvor" in response.text


def test_sidnummerfonstret_kapar_langa_listor():
    from prenly_dl.web.routes import sidnummer

    # Korta listor visas i sin helhet.
    assert sidnummer(1, 4) == [1, 2, 3, 4]
    assert sidnummer(1, 7) == [1, 2, 3, 4, 5, 6, 7]
    # Långa: första, sista och ett fönster runt den aktuella, med hopp.
    assert sidnummer(9, 45) == [1, None, 7, 8, 9, 10, 11, None, 45]
    assert sidnummer(1, 20) == [1, 2, 3, None, 20]
    assert sidnummer(45, 45) == [1, None, 43, 44, 45]
    # Aldrig fler än en handfull länkar, oavsett hur stor katalogen blivit.
    assert len(sidnummer(500, 1000)) <= 9


def test_arshopp_landar_pa_ratt_sida(client, publikation, monkeypatch):
    _koppla(monkeypatch, _katalog(76))
    client.get("/publications/tidningen?page=4")  # hämta hela katalogen

    from prenly_dl.db.session import get_session

    with get_session() as session:
        ar = repo.issue_years(session, publikation)
    assert ar, "årslistan ska inte vara tom"

    svar = client.get(f"/publications/tidningen/year/{ar[-1]}", follow_redirects=False)
    assert svar.status_code == 303
    assert "page=" in svar.headers["location"]

    # Årets första utgåva ska finnas på sidan man landar på.
    sida = client.get(svar.headers["location"])
    assert f"{ar[-1]}-" in sida.text


def test_arshopp_och_sidnummer_doljs_tills_katalogen_ar_hel(
    client, publikation, monkeypatch
):
    _koppla(monkeypatch, _katalog(200))
    svar = client.get("/publications/tidningen")
    # Halvhämtad katalog: inga sidnummer och inget årshopp, de skulle
    # räknas ut från ofullständigt underlag.
    assert "Hoppa till år" not in svar.text
    assert "page-numbers" not in svar.text
    assert "Hämta hela katalogen" in svar.text


def test_kataloghamtning_koas_som_jobb(client, publikation, monkeypatch):
    _koppla(monkeypatch, _katalog(200))
    svar = client.post(
        f"/publications/{publikation}/catalogue", follow_redirects=False
    )
    assert svar.status_code == 303

    jobb = client.get("/api/jobs").json()
    assert [j["type"] for j in jobb] == ["catalogue"]

    # Ingen dubblett medan jobbet ligger i kön.
    client.post(f"/publications/{publikation}/catalogue", follow_redirects=False)
    assert len(client.get("/api/jobs").json()) == 1


def test_kataloghamtningsjobbet_hamtar_allt(db, publikation, monkeypatch):
    from prenly_dl.db.models import JobStatus

    _koppla(monkeypatch, _katalog(76))
    with db() as session:
        repo.enqueue_catalogue(session, publikation)
    with db() as session:
        job = repo.claim_next_job(session)
        services.execute_job(session, job)
    with db() as session:
        publication = repo.get_publication(session, publikation)
        assert repo.count_issues(session, publikation) == 76
        assert publication.catalogue_complete is True
        job = repo.list_jobs(session)[0]
        assert job.status == JobStatus.DONE.value
        assert "76 utgåvor" in job.message
