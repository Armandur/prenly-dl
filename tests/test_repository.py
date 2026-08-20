from prenly_dl.db import repository as repo
from prenly_dl.db.models import IssueStatus, JobStatus


def _publikation(session, namn="Tidningen", title_id=1):
    """Publikationen ligger under en sajt som bär uppgifterna."""
    site = repo.upsert_site(
        session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
    )
    publication, _ = repo.upsert_publication(session, site, title_id, namn)
    return publication


def test_upsert_issue_ar_idempotent(db):
    with db() as session:
        publication = _publikation(session)
        entry = {"uid": "42", "name": "2026-01-01", "publication_date": "2026-01-01",
                 "media": {"checksum": "abc"}}
        issue, created = repo.upsert_issue(session, publication, entry)
        assert created is True
        again, created_again = repo.upsert_issue(session, publication, entry)
        assert created_again is False
        assert again.id == issue.id
        assert again.cover_checksum == "abc"


def test_enqueue_download_dubblerar_inte_aktivt_jobb(db):
    with db() as session:
        publication = _publikation(session)
        issue, _ = repo.upsert_issue(session, publication, {"uid": "1", "name": "x"})
        first = repo.enqueue_download(session, issue)
        assert first is not None
        assert issue.status == IssueStatus.QUEUED.value
        assert repo.enqueue_download(session, issue) is None


def test_claim_next_job_tar_ett_jobb_i_taget(db):
    with db() as session:
        publication = _publikation(session)
        issue, _ = repo.upsert_issue(session, publication, {"uid": "1", "name": "x"})
        repo.enqueue_download(session, issue)
        job = repo.claim_next_job(session)
        assert job is not None
        assert job.status == JobStatus.RUNNING.value
        assert repo.claim_next_job(session) is None


def test_reset_stale_running_jobs_markerar_avbrutna(db):
    with db() as session:
        publication = _publikation(session)
        issue, _ = repo.upsert_issue(session, publication, {"uid": "1", "name": "x"})
        repo.enqueue_download(session, issue)
        repo.claim_next_job(session)
    with db() as session:
        assert repo.reset_stale_running_jobs(session) == 1
        job = repo.list_jobs(session)[0]
        assert job.status == JobStatus.ERROR.value
        assert repo.list_issues(session)[0].status == IssueStatus.ERROR.value


def test_unique_slug_undviker_krock(db):
    with db() as session:
        _publikation(session, "Tidningen")
        assert repo.unique_slug(session, "Tidningen") == "tidningen-2"


def test_uppgifterna_delas_av_sajtens_titlar(db):
    """En förnyelse på sajten gäller alla dess titelåtkomster."""
    with db() as session:
        _publikation(session, "Tidningen", title_id=1)
        _publikation(session, "Bilagan", title_id=2)
        site = repo.list_sites(session)[0]
        assert {access.title_id for access in site.title_accesses} == {1, 2}

        repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "ny-klient", "auth": "ny-token"}
        )
        assert repo.credentials_for_site(site).auth == "ny-token"


def test_site_for_title_valjer_lasbar_sajt_med_fungerande_uppgifter(db):
    with db() as session:
        utan = repo.upsert_site(session, "https://utan.se", {"auth": "utan"})
        med = repo.upsert_site(session, "https://med.se", {"auth": "med"})
        utan.auth_status = med.auth_status = "ok"
        repo.upsert_title_access(session, utan, 259, "Expressen", readable=False)
        repo.upsert_title_access(session, med, 259, "Expressen", readable=True)

        assert repo.site_for_title(session, 259).url == "https://med.se"


def test_site_for_title_ger_none_utan_lasratt(db):
    with db() as session:
        site = repo.upsert_site(session, "https://a.se", {"auth": "token"})
        site.auth_status = "ok"
        repo.upsert_title_access(session, site, 259, "Expressen", readable=False)

        assert repo.site_for_title(session, 259) is None
