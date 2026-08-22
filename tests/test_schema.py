"""Kolumner som modellerna fått sedan databasen skapades.

create_all skapar bara tabeller som saknas helt, aldrig kolumner i en som
redan finns. Efter en uppdatering på servern gav varje sida som rörde ett
nytt fält 500 med "no such column", medan healthz svarade som vanligt -
tjänsten såg frisk ut och bara vyerna föll.
"""

import sqlite3


def _gammal_databas(path):
    """Sites-tabellen som den såg ut innan inloggningen byggdes."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE sites (
          id INTEGER PRIMARY KEY,
          url VARCHAR(255) UNIQUE NOT NULL,
          name VARCHAR(255),
          textalk_auth TEXT NOT NULL,
          auth TEXT NOT NULL,
          cdn VARCHAR(255),
          auth_status VARCHAR(32),
          auth_message TEXT,
          auth_checked_at DATETIME,
          created_at DATETIME,
          updated_at DATETIME
        );
        INSERT INTO sites (url, textalk_auth, auth, auth_status)
        VALUES ('https://etidning.dn.se', 'nyckel', 'token', 'ok');
        """
    )
    db.commit()
    db.close()


def test_saknade_kolumner_laggs_till_vid_start(tmp_path, monkeypatch):
    path = tmp_path / "gammal.db"
    _gammal_databas(path)
    monkeypatch.setenv("PRENLY_DB", str(path))

    import prenly_dl.db.session as session_module

    session_module._engine = None
    session_module._session_factory = None
    from prenly_dl.db import repository as repo

    session_module.init_db()
    with session_module.get_session() as session:
        site = repo.list_sites(session)[0]
        # Raden finns kvar, med de nya fälten på sina defaultvärden.
        assert site.url == "https://etidning.dn.se"
        assert site.auth == "token"
        assert site.username == ""
        assert site.open_access is False

    session_module._engine = None
    session_module._session_factory = None


def test_befintliga_varden_ror_inte_vid_tillagg(tmp_path, monkeypatch):
    """Tillägget får inte skriva över det som redan står i tabellen."""
    path = tmp_path / "gammal.db"
    _gammal_databas(path)
    monkeypatch.setenv("PRENLY_DB", str(path))

    import prenly_dl.db.session as session_module

    session_module._engine = None
    session_module._session_factory = None
    session_module.init_db()

    db = sqlite3.connect(path)
    rad = db.execute("SELECT url, textalk_auth, auth, auth_status FROM sites").fetchone()
    kolumner = {r[1] for r in db.execute("PRAGMA table_info(sites)")}
    db.close()

    assert rad == ("https://etidning.dn.se", "nyckel", "token", "ok")
    assert {"username", "password", "open_access"} <= kolumner

    session_module._engine = None
    session_module._session_factory = None
