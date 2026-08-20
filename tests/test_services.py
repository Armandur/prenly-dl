
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

    assert purge_unused_covers({"4019-anvands.jpg"}) == 1
    assert (root / "4019-anvands.jpg").exists()
    assert not (root / "4019-gammal.jpg").exists()
