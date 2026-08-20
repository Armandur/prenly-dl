from prenly_dl.db import repository as repo
from prenly_dl.db.models import IssueStatus, JobStatus, Publication


def _publikation(session, namn="Tidningen"):
    publication = Publication(
        title_id=1, name=namn, slug=repo.unique_slug(session, namn),
        site="https://a.se", textalk_auth="k", auth="t",
    )
    session.add(publication)
    session.flush()
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
