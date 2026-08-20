"""Migreringen från uppgifter-per-publikation till uppgifter-per-sajt."""

import sqlite3

from sqlalchemy import inspect

GAMMALT_SCHEMA = """
CREATE TABLE publications (
    id INTEGER PRIMARY KEY,
    title_id INTEGER NOT NULL,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(255) NOT NULL UNIQUE,
    site VARCHAR(255) NOT NULL,
    textalk_auth TEXT NOT NULL,
    auth TEXT NOT NULL,
    cdn VARCHAR(255) DEFAULT '',
    prefix VARCHAR(255) DEFAULT '',
    folder VARCHAR(255) DEFAULT '',
    watch_limit INTEGER DEFAULT 1,
    enabled BOOLEAN DEFAULT 1,
    catalogue_offset INTEGER DEFAULT 0,
    catalogue_complete BOOLEAN DEFAULT 0,
    auth_status VARCHAR(32) DEFAULT 'unknown',
    auth_message TEXT,
    auth_checked_at DATETIME,
    cover_checksum VARCHAR(64),
    created_at DATETIME,
    updated_at DATETIME,
    UNIQUE (site, title_id)
);
CREATE TABLE issues (
    id INTEGER PRIMARY KEY,
    publication_id INTEGER NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    uid VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    publication_date VARCHAR(32),
    cover_checksum VARCHAR(64),
    status VARCHAR(32) DEFAULT 'new',
    file_path TEXT,
    page_count INTEGER,
    error TEXT,
    downloaded_at DATETIME,
    calibre_id INTEGER,
    created_at DATETIME,
    updated_at DATETIME
);
"""


def _bygg_gammal_databas(path):
    """En databas som den såg ut innan uppgifterna flyttade till sajten."""
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(GAMMALT_SCHEMA)
        # Tre DN-titlar med samma uppgifter i varsin kopia, plus en
        # publikation på en annan sajt utan prenumeration.
        for index, (title_id, namn, slug) in enumerate(
            [(2357, "Dagens Nyheter", "dagens-nyheter"),
             (2359, "Del B", "del-b"),
             (2369, "Helgen!", "helgen")], start=1
        ):
            conn.execute(
                "INSERT INTO publications (id, title_id, name, slug, site,"
                " textalk_auth, auth, cdn, prefix, folder, watch_limit,"
                " auth_status, catalogue_offset, catalogue_complete)"
                " VALUES (?, ?, ?, ?, 'https://etidning.dn.se/', 'klient', 'token',"
                " 'https://cdn.example', ?, ?, 3, 'ok', 31, 1)",
                (index, title_id, namn, slug, f"{namn} - ", namn),
            )
        conn.execute(
            "INSERT INTO publications (id, title_id, name, slug, site,"
            " textalk_auth, auth, cdn, prefix, folder, watch_limit, auth_status,"
            " catalogue_offset, catalogue_complete)"
            " VALUES (9, 1775, 'Kyrkans Tidning', 'kyrkans-tidning',"
            " 'https://etidning.kyrkanstidning.se', 'kt-klient', 'kt-token', '',"
            " 'Kyrkans Tidning - ', 'Kyrkans Tidning', 1, 'subscription_error', 0, 0)"
        )
        # Utgåvor som inte får tappas bort.
        for i in range(5):
            conn.execute(
                "INSERT INTO issues (publication_id, uid, name, publication_date,"
                " status) VALUES (1, ?, ?, ?, 'done')",
                (str(1000 + i), f"2026-08-{20 - i:02d}", f"2026-08-{20 - i:02d}"),
            )


def test_uppgifterna_flyttar_till_sajter_utan_att_tappa_data(temp_env, monkeypatch):
    db = temp_env / "test.db"
    _bygg_gammal_databas(db)

    from prenly_dl.db import repository as repo
    from prenly_dl.db.session import get_session, init_db

    init_db()

    with get_session() as session:
        publikationer = repo.list_publications(session)
        assert len(publikationer) == 4

        # Två sajter, inte fyra kopior av uppgifterna.
        sajter = repo.list_sites(session)
        assert len(sajter) == 2
        per_url = {s.url: s for s in sajter}
        assert "https://etidning.dn.se" in per_url, "URL:en ska normaliseras"

        dn = per_url["https://etidning.dn.se"]
        assert dn.textalk_auth == "klient"
        assert dn.auth == "token"
        assert dn.cdn == "https://cdn.example"
        assert dn.auth_status == "ok"

        # De tre DN-titlarna pekar på samma sajt.
        dn_titlar = [p for p in publikationer if p.site_id == dn.id]
        assert len(dn_titlar) == 3
        assert {p.title_id for p in dn_titlar} == {2357, 2359, 2369}

        # Per-titel-inställningar överlever.
        helgen = next(p for p in dn_titlar if p.title_id == 2369)
        assert helgen.prefix == "Helgen! - "
        assert helgen.watch_limit == 3
        assert helgen.catalogue_offset == 31
        assert helgen.catalogue_complete is True

        # Befintliga publikationer var aktiva, och läsrätten härleds ur
        # den gamla statusen.
        assert all(p.enabled for p in publikationer)
        kt = next(p for p in publikationer if p.title_id == 1775)
        assert kt.readable is False
        assert helgen.readable is True

        # Utgåvorna hänger kvar. issues har ON DELETE CASCADE mot
        # publications, så en ombyggd tabell tar dem lätt med sig.
        assert repo.count_issues(session, 1) == 5
        alla_utgavor = repo.list_issues(session, limit=100)
        assert len(alla_utgavor) == 5
        assert {i.publication_id for i in alla_utgavor} == {1}

    # De gamla kolumnerna är borta.
    from prenly_dl.db.session import make_session_factory

    kolumner = {
        c["name"]
        for c in inspect(make_session_factory().kw["bind"]).get_columns("publications")
    }
    assert "textalk_auth" not in kolumner
    assert "site" not in kolumner
    assert "site_id" in kolumner


def test_migreringen_kors_bara_en_gang(temp_env):
    db = temp_env / "test.db"
    _bygg_gammal_databas(db)

    import prenly_dl.db.session as session_module
    from prenly_dl.db import repository as repo
    from prenly_dl.db.session import get_session, init_db

    init_db()
    session_module._engine = None
    session_module._session_factory = None
    init_db()

    with get_session() as session:
        assert len(repo.list_sites(session)) == 2
        assert len(repo.list_publications(session)) == 4


def test_ny_databas_migreras_inte(temp_env):
    from prenly_dl.db import repository as repo
    from prenly_dl.db.session import get_session, init_db

    init_db()
    with get_session() as session:
        assert repo.list_sites(session) == []
