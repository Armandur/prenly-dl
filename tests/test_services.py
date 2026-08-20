
from prenly_dl import downloader, services
from prenly_dl.db import repository as repo
from prenly_dl.db.models import IssueStatus, Job, JobStatus
from prenly_dl.errors import PrenlyAuthError, PrenlyError


def _publikation(session):
    site = repo.upsert_site(
        session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
    )
    publication, _ = repo.upsert_publication(session, site, 1, "Tidningen")
    return publication


def _koa_nedladdning(db, force=False):
    with db() as session:
        publication = _publikation(session)
        issue, _ = repo.upsert_issue(session, publication, {"uid": "1", "name": "2026-01-01"})
        job = repo.enqueue_download(session, issue, force=force)
        return job.id, issue.id


def test_jobb_som_misslyckas_lamnar_utgavan_i_fel(db, monkeypatch):
    job_id, issue_id = _koa_nedladdning(db)

    def _spricker(*args, **kwargs):
        raise PrenlyError("nätverket dog på sidan 40")

    monkeypatch.setattr(services, "download_issue", _spricker)

    with db() as session:
        job = repo.claim_next_job(session)
        services.execute_job(session, job)

    with db() as session:
        issue = repo.get_issue(session, issue_id)
        job = session.get(Job, job_id)
        # Ingen utgåva får bli kvar som "downloading" efter ett kraschat jobb.
        assert issue.status == IssueStatus.ERROR.value
        assert "sidan 40" in issue.error
        assert job.status == JobStatus.ERROR.value


def test_authfel_markerar_sajten(db, monkeypatch):
    _koa_nedladdning(db)

    def _utloggad(*args, **kwargs):
        raise PrenlyAuthError("Inte autentiserad.")

    monkeypatch.setattr(services, "download_issue", _utloggad)

    with db() as session:
        job = repo.claim_next_job(session)
        services.execute_job(session, job)

    with db() as session:
        # Ett utgånget token är sajtens problem, inte titelns.
        site = repo.list_sites(session)[0]
        assert site.auth_status == "auth_error"
        assert site.auth_message == "Inte autentiserad."
        assert repo.list_publications(session)[0].readable is True


def test_force_foljer_med_till_nedladdningen(db, monkeypatch):
    _koa_nedladdning(db, force=True)
    sedda = {}

    def _fangar(session, creds, title_id, uid, output_dir, prefix="", force=False, progress=None):
        sedda["force"] = force
        return downloader.DownloadResult(output_dir / "x.pdf", 3)

    monkeypatch.setattr(services, "download_issue", _fangar)

    with db() as session:
        job = repo.claim_next_job(session)
        services.execute_job(session, job)

    assert sedda["force"] is True


def test_forlopp_skrivs_utan_att_committa_jobbets_session(db, monkeypatch):
    job_id, issue_id = _koa_nedladdning(db)

    def _rapporterar_forlopp(session, creds, title_id, uid, output_dir,
                             prefix="", force=False, progress=None):
        progress(40, 68)
        raise PrenlyError("dog efter sidan 40")

    monkeypatch.setattr(services, "download_issue", _rapporterar_forlopp)

    with db() as session:
        job = repo.claim_next_job(session)
        services.execute_job(session, job)

    with db() as session:
        job = [j for j in repo.list_jobs(session) if j.id == job_id][0]
        assert (job.progress_done, job.progress_total) == (40, 68)
        assert job.status == JobStatus.ERROR.value
        assert repo.get_issue(session, issue_id).status == IssueStatus.ERROR.value


def test_omslagsrensning_sparar_de_som_anvands(temp_env):
    from prenly_dl.covers import cover_cache_root, purge_unused_covers

    root = cover_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "4019-anvands.jpg").write_bytes(b"x")
    (root / "4019-gammal.jpg").write_bytes(b"x")
    (root / "4019-anvands-stor.jpg").write_bytes(b"x")

    assert purge_unused_covers({"4019-anvands.jpg"}) == 1
    assert (root / "4019-anvands.jpg").exists()
    assert not (root / "4019-gammal.jpg").exists()
    assert (root / "4019-anvands-stor.jpg").exists()


def test_forhandsgranskning_syns_inte_i_bibliotek_eller_mappstorlek(
    temp_env, monkeypatch
):
    from prenly_dl.config import output_root
    from prenly_dl.web.library import folder_usage, scan_library

    def _skapar_pdf(
        session, creds, title_id, uid, output_dir, prefix="", force=False, progress=None
    ):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "Tidningen - 2026-01-01.pdf"
        path.write_bytes(b"pdf")
        return downloader.DownloadResult(path, 1)

    monkeypatch.setattr(services, "build_session", lambda: None)
    monkeypatch.setattr(services, "download_issue", _skapar_pdf)

    path = services.build_issue_preview(None, 1, "uid-1", 17, "Tidningen - ")

    assert not path.is_relative_to(output_root())
    assert scan_library() == []
    assert folder_usage(output_root() / "Tidningen") == (0, 0)


def test_forhandsgranskning_andrar_inte_utgavans_status_eller_filfalt(
    db, monkeypatch
):
    def _skapar_pdf(
        session, creds, title_id, uid, output_dir, prefix="", force=False, progress=None
    ):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "Tidningen - 2026-01-01.pdf"
        path.write_bytes(b"pdf")
        return downloader.DownloadResult(path, 1)

    monkeypatch.setattr(services, "build_session", lambda: None)
    monkeypatch.setattr(services, "download_issue", _skapar_pdf)

    with db() as session:
        publication = _publikation(session)
        issue, _ = repo.upsert_issue(
            session, publication, {"uid": "preview", "name": "2026-01-01"}
        )
        issue_id = issue.id
        path = services.build_issue_preview(
            repo.credentials_for(publication),
            publication.title_id,
            issue.uid,
            issue.id,
            publication.prefix,
        )

    assert path.is_file()
    with db() as session:
        issue = repo.get_issue(session, issue_id)
        assert issue.status == IssueStatus.NEW.value
        assert issue.file_path is None
        assert issue.downloaded_at is None


def test_daglig_stadning_tar_gamla_previews_men_sparar_farska(db):
    import os
    from datetime import datetime, timedelta, timezone

    from prenly_dl.downloader import preview_cache_root
    from prenly_dl.scheduler import purge_history

    root = preview_cache_root()
    gammal = root / "1"
    farsk = root / "2"
    gammal.mkdir(parents=True)
    farsk.mkdir()
    (gammal / "gammal.pdf").write_bytes(b"gammal")
    (farsk / "farsk.pdf").write_bytes(b"farsk")

    nu = datetime.now(timezone.utc)
    gammal_tid = (nu - timedelta(days=1, minutes=1)).timestamp()
    os.utime(gammal, (gammal_tid, gammal_tid))

    purge_history()

    assert not gammal.exists()
    assert (farsk / "farsk.pdf").is_file()


def test_sajtsynk_hamtar_omslag_aven_utan_lasratt(db, monkeypatch):
    """Katalogen svarar oavsett prenumeration, så alla titlar får bild."""
    from prenly_dl.db.models import Job, JobType

    titlar = [
        {"id": 1, "name": "Läsbar", "user_readable": True},
        {"id": 2, "name": "Utan läsrätt", "user_readable": False},
    ]
    hamtade = []

    monkeypatch.setattr(services, "build_session", lambda: None)
    monkeypatch.setattr(services, "get_context_token", lambda *a, **k: "token")
    monkeypatch.setattr(services, "get_titles", lambda *a, **k: titlar)
    monkeypatch.setattr(
        services, "get_catalogue_issues",
        lambda http, creds, token, title_id, limit=1, offset=0: [
            {"uid": f"u{title_id}", "name": "2026-01-01",
             "publication_date": "2026-01-01", "media": {"checksum": f"ck{title_id}"}}
        ],
    )
    monkeypatch.setattr(
        services, "fetch_and_cache_cover",
        lambda http, creds, title_id, checksum: hamtade.append(checksum) or True,
    )

    with db() as session:
        site = repo.upsert_site(session, "https://a.se", {"textalk-auth": "k", "auth": "t"})
        job = Job(type=JobType.SITE_SYNC.value, site_id=site.id)
        session.add(job)
        session.flush()
        summering = services.run_site_sync_job(session, job)

    assert sorted(hamtade) == ["ck1", "ck2"]
    assert "2 omslag" in summering

    with db() as session:
        for publication in repo.list_publications(session):
            assert repo.latest_issue(session, publication.id) is not None
