import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Isolerad databas och utdatakatalog per test."""
    monkeypatch.setenv("PRENLY_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("PRENLY_OUTPUT", str(tmp_path / "output"))
    monkeypatch.delenv("PRENLY_PASSWORD", raising=False)

    import prenly_dl.db.session as session_module

    session_module._engine = None
    session_module._session_factory = None
    yield tmp_path
    session_module._engine = None
    session_module._session_factory = None


@pytest.fixture
def db(temp_env):
    from prenly_dl.db.session import get_session, init_db

    init_db()
    return get_session


@pytest.fixture
def skapa_publikation(db):
    """Skapa en publikation under en sajt, som modellen ser ut sedan
    uppgifterna flyttade från titeln till sajten."""
    from prenly_dl.db import repository as repo

    def _skapa(
        namn="Tidningen",
        title_id=4019,
        site_url="https://a.se",
        enabled=True,
        readable=True,
        **falt,
    ):
        with db() as session:
            site = repo.upsert_site(
                session, site_url,
                {"textalk-auth": "k", "auth": "t", "cdn": falt.pop("cdn", "")},
            )
            site.auth_status = "ok"
            publication, _ = repo.upsert_publication(
                session, site, title_id, namn, readable=readable
            )
            publication.enabled = enabled
            for nyckel, varde in falt.items():
                setattr(publication, nyckel, varde)
            session.add(publication)
            session.flush()
            return publication.id, publication.slug, site.id

    return _skapa
