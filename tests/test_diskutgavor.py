"""Filer som ligger på disk utan motsvarighet i Prenlys katalog.

De faller ur katalogen när arkivet är grunt - DN listar en månad - eller
hämtades av den gamla containern för flera år sedan. Utan en rad i
tabellen syns de bara i Bibliotek, och prefixbytet lämnar dem kvar med
det gamla namnet.
"""

from prenly_dl import services
from prenly_dl.db import repository as repo
from prenly_dl.db.models import IssueStatus
from prenly_dl.web.library import derive_issue_name, import_existing_files


def _publikation(session, title_id, namn, prefix=""):
    site = repo.find_site(session, "https://a.se")
    if site is None:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
        site.auth_status = "ok"
    publication, _ = repo.upsert_publication(session, site, title_id, namn)
    publication.folder = namn
    publication.prefix = prefix
    session.add(publication)
    session.flush()
    return publication


def _skriv(publication, filnamn):
    path = services.publication_output_dir(publication) / filnamn
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"pdf")
    return path


def test_harleder_namn_genom_att_skala_bort_prefixet():
    assert derive_issue_name("Hemslojd 2011-03-15.pdf", "Hemslojd ") == "2011-03-15"
    # Ett annat prefix går inte att härleda - hela filnamnet blir namnet.
    assert (
        derive_issue_name("Tidningen Hemslojd - 2011-03-15.pdf", "Hemslojd ")
        == "Tidningen Hemslojd - 2011-03-15"
    )
    assert derive_issue_name("2011-03-15.pdf", "") == "2011-03-15"


def test_fil_utan_katalogutgava_blir_diskpost(db, temp_env):
    with db() as session:
        publication = _publikation(session, 1, "Hemslojd", prefix="Hemslojd ")
        path = _skriv(publication, "Hemslojd 2011-03-15.pdf")

        rapport = import_existing_files(session)

        assert rapport.unmatched == []
        assert [item["issue"] for item in rapport.disk_only] == ["2011-03-15"]
        session.expire(publication.title, ["issues"])
        issues = publication.title.issues
        assert len(issues) == 1
        issue = issues[0]
        assert issue.disk_only is True
        assert issue.uid == "disk:2011-03-15"
        assert issue.status == IssueStatus.DONE.value
        assert issue.file_path == str(path)
        # Datumet ur namnet, annars sorteras raden fel i tabellen.
        assert issue.publication_date == "2011-03-15"


def test_andra_importen_skapar_ingen_dubblett(db, temp_env):
    with db() as session:
        publication = _publikation(session, 1, "Hemslojd", prefix="Hemslojd ")
        _skriv(publication, "Hemslojd 2011-03-15.pdf")

        import_existing_files(session)
        import_existing_files(session)

        session.expire(publication.title, ["issues"])
        assert len(publication.title.issues) == 1


def test_diskpost_overlever_prefixbyte_utan_dubblett(db, temp_env):
    with db() as session:
        publication = _publikation(session, 1, "Hemslojd", prefix="Hemslojd ")
        _skriv(publication, "Hemslojd 2011-03-15.pdf")
        import_existing_files(session)

        services.update_publication_storage(
            session, publication, None, prefix="Nytt "
        )
        session.flush()

        ny_fil = services.publication_output_dir(publication) / "Nytt 2011-03-15.pdf"
        assert ny_fil.is_file(), "prefixbytet ska döpa om även diskfilen"

        import_existing_files(session)
        session.expire(publication.title, ["issues"])
        assert len(publication.title.issues) == 1
        assert publication.title.issues[0].file_path == str(ny_fil)


def test_katalogutgava_tar_over_diskposten(db, temp_env):
    with db() as session:
        publication = _publikation(session, 1, "Hemslojd", prefix="Hemslojd ")
        path = _skriv(publication, "Hemslojd 2011-03-15.pdf")
        import_existing_files(session)

        # Utgåvan dyker upp i katalogen igen.
        issue, created = repo.upsert_issue(
            session, publication, {"uid": "riktig-1", "name": "2011-03-15"}
        )

        assert created is True
        session.expire(publication.title, ["issues"])
        assert len(publication.title.issues) == 1
        assert issue.disk_only is False
        assert issue.file_path == str(path)
        assert issue.status == IssueStatus.DONE.value


def test_diskposter_raknas_inte_som_katalogutgavor(db, temp_env):
    with db() as session:
        publication = _publikation(session, 1, "Hemslojd", prefix="Hemslojd ")
        _skriv(publication, "Hemslojd 2011-03-15.pdf")
        import_existing_files(session)
        repo.upsert_issue(session, publication, {"uid": "k1", "name": "2026-08-20"})

        assert repo.count_issues(session, publication.id) == 2
        assert repo.count_catalogue_issues(session, publication.id) == 1


def _klient(temp_env, monkeypatch):
    from fastapi.testclient import TestClient
    import prenly_dl.web.app as app_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
    return TestClient(app_module.create_app())


def test_routen_raderar_filen_och_raden(db, temp_env, monkeypatch):
    with db() as session:
        publication = _publikation(session, 1, "Hemslojd", prefix="Hemslojd ")
        publication.enabled = True
        path = _skriv(publication, "Hemslojd 2011-03-15.pdf")
        import_existing_files(session)
        session.commit()
        issue_id = repo.list_issues(session, publication.id)[0].id

    with _klient(temp_env, monkeypatch) as client:
        svar = client.post(f"/issues/{issue_id}/delete-file", data={"page": 1})

    assert svar.status_code in (200, 303)
    assert not path.exists()
    with db() as session:
        assert repo.get_issue(session, issue_id) is None


def test_routen_vagrar_radera_en_katalogutgava(db, temp_env, monkeypatch):
    with db() as session:
        publication = _publikation(session, 1, "Hemslojd", prefix="Hemslojd ")
        publication.enabled = True
        issue, _ = repo.upsert_issue(
            session, publication, {"uid": "k1", "name": "2026-08-20"}
        )
        path = _skriv(publication, "Hemslojd 2026-08-20.pdf")
        issue.file_path = str(path)
        session.commit()
        issue_id = issue.id

    with _klient(temp_env, monkeypatch) as client:
        svar = client.post(
            f"/issues/{issue_id}/delete-file",
            data={"page": 1},
            headers={"HX-Request": "true"},
        )

    assert svar.status_code == 200
    assert "tas inte bort här" in svar.text
    assert path.is_file()
    with db() as session:
        assert repo.get_issue(session, issue_id) is not None


def test_tabellen_visar_diskposten_utan_hamtknappar(db, temp_env, monkeypatch):
    with db() as session:
        publication = _publikation(session, 1, "Hemslojd", prefix="Hemslojd ")
        publication.enabled = True
        _skriv(publication, "Hemslojd 2011-03-15.pdf")
        import_existing_files(session)
        session.commit()
        slug = publication.slug
        issue_id = repo.list_issues(session, publication.id)[0].id

    with _klient(temp_env, monkeypatch) as client:
        svar = client.get(f"/publications/{slug}")

    assert "bara på disk" in svar.text
    assert "delete-file" in svar.text
    # Batchraden i verktygsfältet har egna knappar med samma ord, så
    # kontrollen gäller radens knappar: de finns bara i ett formulär som
    # postar till /issues/<id>/download.
    assert f"/issues/{issue_id}/download" not in svar.text
    assert f"data-preview=\"/issues/{issue_id}/preview\"" not in svar.text
