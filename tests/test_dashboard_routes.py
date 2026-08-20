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

    publication_id = _publikation()
    with get_session() as session:
        site = repo.get_site(session, repo.get_publication(session, publication_id).site_id)
        inaktiv, _ = repo.upsert_publication(session, site, 5000, "Inte bevakad")
        inaktiv.enabled = False
        session.add(inaktiv)

    svar = client.get("/")
    assert "Tidningen" in svar.text
    assert "Inte bevakad" not in svar.text
    # Men den finns i katalogen.
    assert "Inte bevakad" in client.get("/katalog").text


def test_omslag_for_okand_publikation_ger_404(client):
    assert client.get("/covers/999/abc.jpg").status_code == 404


def test_omslag_hamtas_och_cachas(client, monkeypatch):
    publication_id = _publikation()
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

    first = client.get(f"/covers/{publication_id}/abc123.jpg")
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/jpeg"

    # Andra anropet ska serveras ur cachen utan ny hämtning.
    second = client.get(f"/covers/{publication_id}/abc123.jpg")
    assert second.status_code == 200
    assert hamtningar == ["abc123"]


def test_kortet_visar_storlek_pa_disk(temp_env, client):
    from prenly_dl.db.session import get_session
    from prenly_dl.web.library import folder_usage

    with get_session() as session:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
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
