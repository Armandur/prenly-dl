import pytest

from prenly_dl import services
from prenly_dl.db import repository as repo
from prenly_dl.web.library import folder_usage, import_existing_files


@pytest.fixture
def client(temp_env, monkeypatch):
    from fastapi.testclient import TestClient
    import prenly_dl.web.app as app_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
    with TestClient(app_module.create_app()) as test_client:
        yield test_client


def _create_publication(session, title_id, name, *, enabled=True):
    site = repo.find_site(session, "https://a.se")
    if site is None:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
        site.auth_status = "ok"
    publication, _ = repo.upsert_publication(session, site, title_id, name)
    publication.enabled = enabled
    publication.folder = name
    session.add(publication)
    return publication


def test_bilaga_lagras_under_huvudpublikationen_och_kan_flyttas_tillbaka(db, temp_env):
    with db() as session:
        main = _create_publication(session, 1, "Huvud")
        attachment = _create_publication(session, 2, "Bilaga")
        issue, _ = repo.upsert_issue(
            session, attachment, {"uid": "b1", "name": "2026-08-20"}
        )
        old_dir = services.publication_output_dir(attachment)
        old_dir.mkdir(parents=True)
        pdf = old_dir / "Bilaga - 2026-08-20.pdf"
        pdf.write_bytes(b"pdf")
        issue.file_path = str(pdf)
        session.add(issue)

        assert services.set_main_publication(session, attachment, main) == 1
        nested = temp_env / "output" / "Huvud" / "Bilaga"
        assert services.publication_output_dir(attachment) == nested
        assert (nested / pdf.name).is_file()
        assert issue.file_path == str(nested / pdf.name)
        assert not old_dir.exists()

        assert services.set_main_publication(session, attachment, None) == 1
        assert pdf.is_file()
        assert issue.file_path == str(pdf)


def test_bilaga_far_inte_ha_egen_bilaga(db):
    with db() as session:
        main = _create_publication(session, 1, "Huvud")
        attachment = _create_publication(session, 2, "Bilaga")
        child = _create_publication(session, 3, "Bilagans bilaga")
        services.set_main_publication(session, attachment, main)

        with pytest.raises(ValueError, match="kan inte vara huvudpublikation"):
            services.set_main_publication(session, child, attachment)


def test_huvudpublikationens_diskstorlek_raknar_inte_bilagan(db, temp_env):
    with db() as session:
        main = _create_publication(session, 1, "Huvud")
        attachment = _create_publication(session, 2, "Bilaga")
        services.set_main_publication(session, attachment, main)
        main_dir = services.publication_output_dir(main)
        attachment_dir = services.publication_output_dir(attachment)
        main_dir.mkdir(parents=True)
        attachment_dir.mkdir(parents=True)
        (main_dir / "huvud.pdf").write_bytes(b"aa")
        (attachment_dir / "bilaga.pdf").write_bytes(b"bbb")

        assert folder_usage(main_dir) == (1, 2)
        assert folder_usage(attachment_dir) == (1, 3)


def test_import_matchar_huvud_och_bilaga_i_varsin_katalog(db, temp_env):
    with db() as session:
        main = _create_publication(session, 1, "Huvud")
        attachment = _create_publication(session, 2, "Bilaga")
        main.prefix = attachment.prefix = ""
        services.set_main_publication(session, attachment, main)
        main_issue, _ = repo.upsert_issue(
            session, main, {"uid": "h1", "name": "2026-08-20"}
        )
        attachment_issue, _ = repo.upsert_issue(
            session, attachment, {"uid": "b1", "name": "2026-08-20"}
        )
        for publication in (main, attachment):
            path = services.publication_output_dir(publication) / "2026-08-20.pdf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"pdf")

        report = import_existing_files(session)

        assert {item["publication"] for item in report.matched} == {"Huvud", "Bilaga"}
        assert main_issue.file_path == str(
            temp_env / "output" / "Huvud" / "2026-08-20.pdf"
        )
        assert attachment_issue.file_path == str(
            temp_env / "output" / "Huvud" / "Bilaga" / "2026-08-20.pdf"
        )
        assert report.misplaced == []


def test_import_kopplar_inte_fil_i_fel_publikationskatalog(db, temp_env):
    with db() as session:
        main = _create_publication(session, 1, "Huvud")
        attachment = _create_publication(session, 2, "Bilaga")
        services.set_main_publication(session, attachment, main)
        issue, _ = repo.upsert_issue(
            session, attachment, {"uid": "b1", "name": "2026-08-20"}
        )
        wrong = services.publication_output_dir(main) / "Bilaga - 2026-08-20.pdf"
        wrong.parent.mkdir(parents=True)
        wrong.write_bytes(b"pdf")

        report = import_existing_files(session)

        assert report.matched == []
        assert report.misplaced[0]["file_path"] == "Huvud/Bilaga - 2026-08-20.pdf"
        assert report.misplaced[0]["matches"] == [
            {"publication": "Bilaga", "issue": "2026-08-20"}
        ]
        assert issue.file_path is None


def test_bilagekopplingen_overlever_titelsynk(db):
    with db() as session:
        main = _create_publication(session, 1, "Huvud")
        attachment = _create_publication(session, 2, "Bilaga")
        site = repo.find_site(session, "https://a.se")
        services.set_main_publication(session, attachment, main)
        services.synka_titlar(
            session,
            site,
            [
                {"id": 1, "name": "Huvud uppdaterad", "user_readable": True},
                {"id": 2, "name": "Bilaga uppdaterad", "user_readable": True},
            ],
        )
        session.flush()
        session.expire_all()

        refreshed = repo.get_publication(session, attachment.id)
        assert refreshed.main_publication_id == main.id
        assert refreshed.main_publication.name == "Huvud uppdaterad"


def test_webbformular_kopplar_flyttar_och_tar_bort_bilaga(client, temp_env):
    from prenly_dl.db.session import get_session

    with get_session() as session:
        main = _create_publication(session, 1, "Huvud")
        attachment = _create_publication(session, 2, "Bilaga")
        issue, _ = repo.upsert_issue(
            session, attachment, {"uid": "b1", "name": "2026-08-20"}
        )
        pdf = services.publication_output_dir(attachment) / "Bilaga - 2026-08-20.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"pdf")
        issue.file_path = str(pdf)
        session.add(issue)
        main_id = main.id
        attachment_id = attachment.id
        slug = attachment.slug

    response = client.post(
        f"/publications/{attachment_id}/edit",
        data={
            "name": "Bilaga",
            "prefix": "Bilaga - ",
            "folder": "Bilaga",
            "watch_limit": "1",
            "enabled": "on",
            "main_publication_id": str(main_id),
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Bilaga till" in response.text
    assert f'value="{main_id}" selected' in response.text
    nested = temp_env / "output" / "Huvud" / "Bilaga" / pdf.name
    assert nested.is_file()
    assert "Bilaga till Huvud" in client.get("/").text
    assert "Bilaga till Huvud" in client.get("/katalog").text

    response = client.post(
        f"/publications/{attachment_id}/edit",
        data={
            "name": "Bilaga",
            "prefix": "Bilaga - ",
            "folder": "Bilaga",
            "watch_limit": "1",
            "enabled": "on",
            "main_publication_id": "",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert pdf.is_file()
    with get_session() as session:
        attachment = repo.get_publication_by_slug(session, slug)
        assert attachment.main_publication_id is None
        assert repo.list_issues(session, attachment.id)[0].file_path == str(pdf)


def test_biblioteksimporten_rapporterar_fel_publikationskatalog(client, temp_env):
    from prenly_dl.db.session import get_session

    with get_session() as session:
        main = _create_publication(session, 1, "Huvud")
        attachment = _create_publication(session, 2, "Bilaga")
        services.set_main_publication(session, attachment, main)
        repo.upsert_issue(session, attachment, {"uid": "b1", "name": "2026-08-20"})
        wrong = services.publication_output_dir(main) / "Bilaga - 2026-08-20.pdf"
        wrong.parent.mkdir(parents=True)
        wrong.write_bytes(b"pdf")

    response = client.post("/library/import")

    assert response.status_code == 200
    assert "1 filer ligger i fel publikationskatalog" in response.text
    assert "Huvud/Bilaga - 2026-08-20.pdf" in response.text
    assert "Bilaga / 2026-08-20" in response.text
