"""Domänlogik som delas av schemaläggaren, webbui:t och CLI:t."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from .api import build_session, get_catalogue_issues, get_context_token, get_titles
from .config import output_root
from .db import repository as repo
from .db.models import (
    AuthStatus,
    Issue,
    IssueStatus,
    Job,
    JobStatus,
    JobType,
    Publication,
    utcnow,
)
from .downloader import download_issue, safe_filename
from .errors import PrenlyAuthError, PrenlyError, PrenlySubscriptionError

logger = logging.getLogger(__name__)


def publication_output_dir(publication: Publication) -> Path:
    folder = publication.folder.strip() or publication.name
    return output_root() / safe_filename(folder)


def _record_error(session: Session, publication: Publication, error: Exception) -> None:
    if isinstance(error, PrenlyAuthError):
        repo.set_auth_status(session, publication, AuthStatus.AUTH_ERROR, str(error))
    elif isinstance(error, PrenlySubscriptionError):
        repo.set_auth_status(
            session, publication, AuthStatus.SUBSCRIPTION_ERROR, str(error)
        )
    else:
        repo.set_auth_status(session, publication, AuthStatus.ERROR, str(error))


def verify_credentials(session: Session, publication: Publication) -> AuthStatus:
    """Kontrollera uppgifterna mot API:t och spara resultatet."""
    http = build_session()
    creds = repo.credentials_for(publication)
    try:
        token = get_context_token(http, creds)
        titles = get_titles(http, creds, token)
    except PrenlyError as error:
        _record_error(session, publication, error)
        return AuthStatus(publication.auth_status)

    match = next((t for t in titles if t.get("id") == publication.title_id), None)
    if match is None:
        repo.set_auth_status(
            session,
            publication,
            AuthStatus.SUBSCRIPTION_ERROR,
            f"Publikation {publication.title_id} finns inte på {publication.site}",
        )
    elif not match.get("user_readable", True):
        repo.set_auth_status(
            session,
            publication,
            AuthStatus.SUBSCRIPTION_ERROR,
            "E-tidningen ingår inte i prenumerationen",
        )
    else:
        repo.set_auth_status(session, publication, AuthStatus.OK, None)
    return AuthStatus(publication.auth_status)


def discover_titles(creds) -> list[dict]:
    """Lista publikationer som uppgifterna ger tillgång till."""
    http = build_session()
    token = get_context_token(http, creds)
    return get_titles(http, creds, token)


def poll_publication(
    session: Session, publication: Publication, enqueue: bool = True
) -> dict:
    """Hämta katalogen, spara nya utgåvor och köa nedladdning av dem."""
    http = build_session()
    creds = repo.credentials_for(publication)
    limit = max(1, publication.watch_limit)

    token = get_context_token(http, creds)
    entries = get_catalogue_issues(http, creds, token, publication.title_id, limit)

    new_issues = 0
    queued = 0
    for entry in entries:
        issue, created = repo.upsert_issue(session, publication, entry)
        if created:
            new_issues += 1
        if enqueue and issue.status in (IssueStatus.NEW.value, IssueStatus.ERROR.value):
            if repo.enqueue_download(session, issue):
                queued += 1

    repo.set_auth_status(session, publication, AuthStatus.OK, None)
    logger.info(
        "%s: %d utgåvor i katalogen, %d nya, %d köade",
        publication.name, len(entries), new_issues, queued,
    )
    return {"found": len(entries), "new": new_issues, "queued": queued}


def run_poll_job(session: Session, job: Job) -> str:
    """Kör ett poll-jobb för en eller alla aktiva publikationer."""
    if job.publication_id:
        publication = repo.get_publication(session, job.publication_id)
        publications = [publication] if publication else []
    else:
        publications = [p for p in repo.list_publications(session) if p.enabled]

    totals = {"found": 0, "new": 0, "queued": 0}
    failures: list[str] = []
    for publication in publications:
        try:
            result = poll_publication(session, publication)
        except PrenlyError as error:
            _record_error(session, publication, error)
            failures.append(f"{publication.name}: {error}")
            logger.warning("Poll misslyckades för %s: %s", publication.name, error)
            session.commit()
            continue
        for key in totals:
            totals[key] += result[key]
        # Släpp skrivlåset mellan publikationerna: nästa varv gör HTTP-anrop.
        session.commit()

    summary = (
        f"{len(publications)} publikationer, {totals['new']} nya utgåvor, "
        f"{totals['queued']} köade"
    )
    if failures:
        job.error = "\n".join(failures)
        summary += f", {len(failures)} misslyckades"
    return summary


def run_download_job(session: Session, job: Job) -> str:
    """Ladda ner utgåvan som jobbet pekar på."""
    issue: Issue | None = repo.get_issue(session, job.issue_id) if job.issue_id else None
    if issue is None:
        raise PrenlyError("Jobbet saknar utgåva")

    publication = repo.get_publication(session, issue.publication_id)
    if publication is None:
        raise PrenlyError("Utgåvan saknar publikation")

    issue.status = IssueStatus.DOWNLOADING.value
    session.add(issue)
    session.commit()

    job_id = job.id

    def progress(done: int, total: int) -> None:
        repo.update_job_progress(job_id, done, total)

    http = build_session()
    try:
        result = download_issue(
            http,
            repo.credentials_for(publication),
            publication.title_id,
            issue.uid,
            publication_output_dir(publication),
            prefix=publication.prefix,
            force=job.force,
            progress=progress,
        )
    except PrenlyError as error:
        issue.status = IssueStatus.ERROR.value
        issue.error = str(error)
        session.add(issue)
        _record_error(session, publication, error)
        raise

    issue.status = IssueStatus.DONE.value
    issue.error = None
    issue.file_path = str(result.path)
    if not result.skipped:
        issue.page_count = result.page_count
        issue.downloaded_at = utcnow()
    session.add(issue)
    repo.set_auth_status(session, publication, AuthStatus.OK, None)

    if result.skipped:
        return f"{result.path.name} fanns redan"
    return f"{result.path.name} ({result.page_count} sidor)"


def execute_job(session: Session, job: Job) -> None:
    """Kör ett jobb och skriv utfallet till jobbraden."""
    try:
        if job.type == JobType.POLL.value:
            summary = run_poll_job(session, job)
        elif job.type == JobType.DOWNLOAD.value:
            summary = run_download_job(session, job)
        else:
            raise PrenlyError(f"Okänd jobbtyp: {job.type}")
    except Exception as error:  # noqa: BLE001 - jobbet ska aldrig fälla arbetaren
        logger.exception("Jobb %s misslyckades", job.id)
        repo.finish_job(session, job, JobStatus.ERROR, error=str(error))
    else:
        repo.finish_job(session, job, JobStatus.DONE, message=summary)
