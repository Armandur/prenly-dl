import pytest
from fastapi.testclient import TestClient

from prenly_dl.db import repository as repo
from prenly_dl.db.models import Publication


@pytest.fixture
def client(temp_env, monkeypatch):
    # Schemaläggaren ska inte starta i testerna.
    import prenly_dl.web.app as app_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
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
