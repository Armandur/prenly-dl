import pytest
from fastapi.testclient import TestClient

from prenly_dl.covers import cover_cache_root
from prenly_dl.db import repository as repo
from prenly_dl.web.library import resolve_file, scan_library


@pytest.fixture
def client(temp_env, monkeypatch):
    import prenly_dl.web.app as app_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
    with TestClient(app_module.create_app()) as test_client:
        yield test_client


def _publikation():
    from prenly_dl.db.session import get_session

    with get_session() as session:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
        site.auth_status = "ok"
        publication, _ = repo.upsert_publication(session, site, 4019, "Tidningen")
        publication.enabled = True
        session.add(publication)
        session.flush()
        return publication.id


def test_biblioteket_listar_pdfer_men_inte_tempfiler(temp_env, client):
    output = temp_env / "output" / "Tidningen"
    output.mkdir(parents=True)
    (output / "Tidningen - 2026-01-01.pdf").write_bytes(b"%PDF-1.4 test")
    # Pågående nedladdning: sidfiler i en tempkatalog ska inte räknas.
    temp = output / "prenly-42-abc"
    temp.mkdir()
    (temp / "00001.pdf").write_bytes(b"%PDF-1.4 sida")

    filer = scan_library()
    assert [f.name for f in filer] == ["Tidningen - 2026-01-01.pdf"]

    response = client.get("/library")
    assert response.status_code == 200
    assert "Tidningen - 2026-01-01.pdf" in response.text
    assert "00001.pdf" not in response.text


def test_biblioteksfil_kan_hamtas(temp_env, client):
    output = temp_env / "output" / "Tidningen"
    output.mkdir(parents=True)
    (output / "en.pdf").write_bytes(b"%PDF-1.4 test")

    response = client.get("/library/file/Tidningen/en.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_biblioteksfil_slipper_inte_ut_ur_katalogen(temp_env, client):
    (temp_env / "hemligt.pdf").write_bytes(b"%PDF-1.4 hemligt")
    assert resolve_file("../hemligt.pdf") is None
    assert client.get("/library/file/../hemligt.pdf").status_code == 404


def test_jobbfiltret_visar_bara_vald_status(client):
    publication_id = _publikation()
    from prenly_dl.db.models import JobStatus
    from prenly_dl.db.session import get_session

    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        issue, _ = repo.upsert_issue(session, publication, {"uid": "1", "name": "2026-01-01"})
        klart = repo.enqueue_download(session, issue)
        repo.finish_job(session, klart, JobStatus.DONE, message="klart")
        repo.enqueue_poll(session)

    assert client.get("/jobs?status=done").text.count("2026-01-01") >= 1
    koade = client.get("/jobs?status=queued")
    assert "2026-01-01" not in koade.text
    assert "poll" in koade.text


def test_jobbdetaljsidan(client):
    publication_id = _publikation()
    from prenly_dl.db.session import get_session

    with get_session() as session:
        job = repo.enqueue_poll(session, publication_id)
        job_id = job.id

    response = client.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    assert "Tidningen" in response.text


def test_okant_jobb_leder_till_jobblistan(client):
    response = client.get("/jobs/999", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/jobs"


def test_statistikkorten(client):
    _publikation()
    response = client.get("/stats/cards")
    assert response.status_code == 200
    assert "Bevakas" in response.text
    assert "av 1 i katalogen" in response.text
    assert "1 sajt" in response.text
    assert "stats-cards" in response.text


def test_oversikten_visar_bara_bevakade(client):
    from prenly_dl.db.session import get_session

    _publikation()
    with get_session() as session:
        site = repo.list_sites(session)[0]
        inaktiv, _ = repo.upsert_publication(session, site, 5000, "Inte bevakad")
        inaktiv.enabled = False
        session.add(inaktiv)

    svar = client.get("/")
    assert "Tidningen" in svar.text
    assert "Inte bevakad" not in svar.text
    # Men den finns i katalogen.
    assert "Inte bevakad" in client.get("/catalogue").text


def test_omslag_for_okand_publikation_ger_404(client):
    assert client.get("/covers/999/abc.jpg").status_code == 404


def test_omslag_hamtas_och_cachas(client, monkeypatch):
    _publikation()
    import prenly_dl.web.routes as routes_module

    hamtningar = []

    def _fejkad(session, creds, title_id, checksum):
        hamtningar.append(checksum)
        path = cover_cache_root()
        path.mkdir(parents=True, exist_ok=True)
        target = path / f"{title_id}-{checksum}.jpg"
        target.write_bytes(b"\xff\xd8\xff fejkad jpeg")
        return target

    monkeypatch.setattr(routes_module, "fetch_and_cache_cover", _fejkad)

    first = client.get("/covers/4019/abc123.jpg")
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/jpeg"

    # Andra anropet ska serveras ur cachen utan ny hämtning.
    second = client.get("/covers/4019/abc123.jpg")
    assert second.status_code == 200
    assert hamtningar == ["abc123"]


def test_stort_omslag_hamtas_och_cachas_separat(client, monkeypatch):
    _publikation()
    import prenly_dl.web.routes as routes_module

    hamtningar = []

    def _fejkad(session, creds, title_id, checksum, *, stor=False):
        hamtningar.append((checksum, stor))
        path = cover_cache_root()
        path.mkdir(parents=True, exist_ok=True)
        target = path / f"{title_id}-{checksum}-stor.jpg"
        target.write_bytes(b"\xff\xd8\xff stor fejkad jpeg")
        return target

    monkeypatch.setattr(routes_module, "fetch_and_cache_cover", _fejkad)

    url = "/covers/4019/abc123.jpg?stor=1"
    first = client.get(url)
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/jpeg"
    assert (cover_cache_root() / "4019-abc123-stor.jpg").is_file()

    second = client.get(url)
    assert second.status_code == 200
    assert hamtningar == [("abc123", True)]


def test_omslagsmallarna_pekar_pa_stor_variant(client):
    from prenly_dl.db.session import get_session

    publication_id = _publikation()
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        repo.upsert_issue(
            session,
            publication,
            {
                "uid": "omslag",
                "name": "2026-08-20",
                "media": {"checksum": "omslag123"},
            },
        )
        slug = publication.slug

    attribut = (
        'data-lightbox-src="/covers/4019/omslag123.jpg?stor=1"'
    )
    oversikt = client.get("/")
    katalog = client.get("/catalogue")
    sajtkort = client.post(
        f"/catalogue/{publication_id}/toggle",
        data={"aktiv": "on"},
        headers={"HX-Request": "true"},
    )
    detalj = client.get(f"/publications/{slug}")
    utgavor = client.get(f"/publications/{slug}/issues?page=1")

    assert attribut in oversikt.text
    assert attribut in katalog.text
    assert attribut in sajtkort.text
    assert detalj.text.count(attribut) == 2
    assert attribut in utgavor.text
    assert 'id="cover-lightbox"' in oversikt.text
    assert 'document.body.addEventListener("click"' in oversikt.text


def test_kortet_visar_storlek_pa_disk(temp_env, client):
    from prenly_dl.db.session import get_session
    from prenly_dl.web.library import folder_usage

    with get_session() as session:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
        site.auth_status = "ok"
        publication, _ = repo.upsert_publication(session, site, 4019, "Tidningen")
        publication.folder = "Tidningen"
        publication.enabled = True
        session.add(publication)

    mapp = temp_env / "output" / "Tidningen"
    mapp.mkdir(parents=True)
    (mapp / "en.pdf").write_bytes(b"x" * 2048)
    (mapp / "tva.pdf").write_bytes(b"x" * 1024)
    # Pågående nedladdning ska inte räknas in.
    temp = mapp / "prenly-9-abc"
    temp.mkdir()
    (temp / "00001.pdf").write_bytes(b"x" * 999)

    assert folder_usage(mapp) == (2, 3072)

    svar = client.get("/")
    assert "2 filer på disk" in svar.text
    assert "3.0 kB" in svar.text


def _sajt_och_titlar(client):
    from prenly_dl.db.session import get_session

    with get_session() as session:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
        site.auth_status = "ok"
        pub, _ = repo.upsert_publication(session, site, 4019, "Tidningen")
        pub.enabled = False
        session.add(pub)
        session.flush()
        return site.id, pub.id


def test_katalogatgarder_svarar_med_sajtkortet_for_htmx(client, monkeypatch):
    import prenly_dl.web.routes as routes_module

    monkeypatch.setattr(routes_module, "verify_site", lambda session, site: None)
    site_id, pub_id = _sajt_och_titlar(client)

    toggle = client.post(
        f"/catalogue/{pub_id}/toggle", data={"aktiv": "on"},
        headers={"HX-Request": "true"},
    )
    assert toggle.status_code == 200
    assert 'id="katalogtabell"' in toggle.text
    assert "<!doctype html>" not in toggle.text.lower()

    synk = client.post(f"/sites/{site_id}/sync", headers={"HX-Request": "true"})
    assert synk.status_code == 200
    assert f'id="sajt-{site_id}"' in synk.text
    assert "kortbesked" in synk.text


def test_katalogatgarder_utan_javascript_gar_tillbaka(client):
    site_id, pub_id = _sajt_och_titlar(client)
    svar = client.post(
        f"/catalogue/{pub_id}/toggle", data={"aktiv": "on"}, follow_redirects=False
    )
    assert svar.status_code == 303
    assert svar.headers["location"] == "/catalogue"


def test_toggle_slar_pa_och_av(client):
    from prenly_dl.db.session import get_session

    site_id, pub_id = _sajt_och_titlar(client)

    client.post(f"/catalogue/{pub_id}/toggle", data={"aktiv": "on"}, follow_redirects=True)
    with get_session() as session:
        assert repo.get_publication(session, pub_id).enabled is True

    client.post(f"/catalogue/{pub_id}/toggle", data={"aktiv": ""}, follow_redirects=True)
    with get_session() as session:
        assert repo.get_publication(session, pub_id).enabled is False


def test_katalogen_deduplicerar_titel_och_visar_sajtvis_lasratt(client):
    """Samma globala titel renderas en gång med båda åtkomstraderna."""
    from prenly_dl.db.session import get_session

    with get_session() as session:
        dalarna = repo.upsert_site(session, "https://dalarna.se", {"auth": "d"})
        angermanland = repo.upsert_site(session, "https://angermanland.se", {"auth": "a"})
        dalarna.auth_status = angermanland.auth_status = "ok"
        repo.upsert_title_access(session, dalarna, 259, "Expressen", readable=False)
        repo.upsert_title_access(session, angermanland, 259, "Expressen", readable=True)

    svar = client.get("/catalogue")
    assert svar.status_code == 200
    assert svar.text.count(">Expressen</a>") == 1
    assert "https://dalarna.se: Ingen läsrätt" in svar.text
    assert "https://angermanland.se: Läsrätt" in svar.text


def test_uppgifter_listar_sajt_med_antal_titlar(client):
    from prenly_dl.db.session import get_session

    with get_session() as session:
        site = repo.upsert_site(session, "https://a.se", {"auth": "token"})
        site.auth_status = "ok"
        repo.upsert_title_access(session, site, 1, "Läsbar", readable=True)
        repo.upsert_title_access(session, site, 2, "Låst", readable=False)

    svar = client.get("/credentials")
    assert svar.status_code == 200
    assert "https://a.se" in svar.text
    assert "2 titlar, 1 läsbara" in svar.text
    assert "Testa uppgifter" in svar.text
    assert "Hämta titlar och omslag" in svar.text


def test_jobbsidan_visar_vad_jobbet_gjorde(client, temp_env):
    """Sammanfattningen säger hur många, detaljerna säger vilka."""
    from prenly_dl.db.models import Job, JobStatus, JobType, utcnow
    from prenly_dl.db.session import get_session

    with get_session() as session:
        job = Job(
            type=JobType.ENRICH.value,
            status=JobStatus.DONE.value,
            progress_done=2,
            progress_total=2,
            message="2 böcker fick uppgifter",
            detail="2024-04-02: bok 1541 <- series = Hemslöjd\n2023-03-27: bok 1384 <- pubdate = 2023-03-27",
            started_at=utcnow(),
            finished_at=utcnow(),
        )
        session.add(job)
        session.flush()
        job_id = job.id

    text = client.get(f"/jobs/{job_id}").text

    # Jobbtypen på svenska, inte kodnamnet ensamt.
    assert "Fyller på uppgifter i Calibre" in text
    # Förloppet räknar böcker här, inte sidor.
    assert "2/2 böcker" in text
    assert "sidor" not in text
    assert "Vad jobbet gjorde" in text
    assert "bok 1541" in text and "bok 1384" in text


def test_jobbsidan_utan_detaljer_visar_ingen_tom_ruta(client, temp_env):
    from prenly_dl.db.models import Job, JobStatus, JobType
    from prenly_dl.db.session import get_session

    with get_session() as session:
        job = Job(type=JobType.POLL.value, status=JobStatus.DONE.value)
        session.add(job)
        session.flush()
        job_id = job.id

    text = client.get(f"/jobs/{job_id}").text
    assert "Vad jobbet gjorde" not in text
    assert "Katalogkontroll" in text
