"""Läs- och skrivhjälpare mot databasen.

Håller SQL-detaljerna borta från schemaläggaren och webblagret.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..api import Credentials
from .models import (
    AuthStatus,
    Issue,
    IssueStatus,
    Job,
    JobStatus,
    JobType,
    Publication,
    Setting,
    utcnow,
)

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    lowered = value.lower()
    for source, target in (("å", "a"), ("ä", "a"), ("ö", "o"), ("é", "e")):
        lowered = lowered.replace(source, target)
    return _SLUG_UNSAFE.sub("-", lowered).strip("-") or "publikation"


def unique_slug(session: Session, base: str, exclude_id: int | None = None) -> str:
    slug = slugify(base)
    candidate = slug
    counter = 2
    while True:
        stmt = select(Publication).where(Publication.slug == candidate)
        if exclude_id is not None:
            stmt = stmt.where(Publication.id != exclude_id)
        if session.execute(stmt).first() is None:
            return candidate
        candidate = f"{slug}-{counter}"
        counter += 1


# --------------------------------------------------------------------------
# Publikationer
# --------------------------------------------------------------------------


def list_publications(session: Session) -> list[Publication]:
    return list(session.execute(select(Publication).order_by(Publication.name)).scalars())


def get_publication(session: Session, publication_id: int) -> Publication | None:
    return session.get(Publication, publication_id)


def get_publication_by_slug(session: Session, slug: str) -> Publication | None:
    return session.execute(
        select(Publication).where(Publication.slug == slug)
    ).scalar_one_or_none()


def find_publication(session: Session, site: str, title_id: int) -> Publication | None:
    return session.execute(
        select(Publication).where(
            Publication.site == site.rstrip("/"), Publication.title_id == title_id
        )
    ).scalar_one_or_none()


def credentials_for(publication: Publication) -> Credentials:
    return Credentials(
        site=publication.site,
        textalk_auth=publication.textalk_auth,
        auth=publication.auth,
        cdn=publication.cdn or "",
    )


def set_auth_status(
    session: Session,
    publication: Publication,
    status: AuthStatus,
    message: str | None = None,
) -> None:
    publication.auth_status = status.value
    publication.auth_message = message
    publication.auth_checked_at = utcnow()
    session.add(publication)


# --------------------------------------------------------------------------
# Utgåvor
# --------------------------------------------------------------------------


def list_issues(
    session: Session, publication_id: int | None = None, limit: int = 100
) -> list[Issue]:
    stmt = select(Issue).order_by(Issue.publication_date.desc(), Issue.id.desc())
    if publication_id is not None:
        stmt = stmt.where(Issue.publication_id == publication_id)
    return list(session.execute(stmt.limit(limit)).scalars())


def get_issue(session: Session, issue_id: int) -> Issue | None:
    return session.get(Issue, issue_id)


def upsert_issue(
    session: Session, publication: Publication, entry: dict
) -> tuple[Issue, bool]:
    """Skapa eller uppdatera en utgåva från en katalogpost. Returnerar (utgåva, ny)."""
    uid = str(entry.get("uid") or entry.get("id"))
    issue = session.execute(
        select(Issue).where(
            Issue.publication_id == publication.id, Issue.uid == uid
        )
    ).scalar_one_or_none()

    created = issue is None
    if issue is None:
        issue = Issue(publication_id=publication.id, uid=uid)

    issue.name = str(entry.get("name") or uid)
    issue.publication_date = entry.get("publication_date")
    media = entry.get("media") or {}
    issue.cover_checksum = media.get("checksum")
    session.add(issue)
    session.flush()
    return issue, created


def issues_pending_download(session: Session, publication_id: int) -> list[Issue]:
    return list(
        session.execute(
            select(Issue).where(
                Issue.publication_id == publication_id,
                Issue.status.in_([IssueStatus.NEW.value, IssueStatus.ERROR.value]),
            )
        ).scalars()
    )


# --------------------------------------------------------------------------
# Jobb
# --------------------------------------------------------------------------


def has_active_job(session: Session, issue_id: int) -> bool:
    stmt = select(func.count(Job.id)).where(
        Job.issue_id == issue_id,
        Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
    )
    return bool(session.execute(stmt).scalar_one())


def enqueue_download(session: Session, issue: Issue, force: bool = False) -> Job | None:
    """Köa nedladdning av en utgåva om den inte redan har ett aktivt jobb."""
    if has_active_job(session, issue.id):
        return None
    job = Job(
        type=JobType.DOWNLOAD.value,
        status=JobStatus.QUEUED.value,
        publication_id=issue.publication_id,
        issue_id=issue.id,
        message="Tvingad omnedladdning" if force else None,
    )
    issue.status = IssueStatus.QUEUED.value
    issue.error = None
    session.add_all([job, issue])
    session.flush()
    return job


def enqueue_poll(session: Session, publication_id: int | None = None) -> Job:
    job = Job(
        type=JobType.POLL.value,
        status=JobStatus.QUEUED.value,
        publication_id=publication_id,
    )
    session.add(job)
    session.flush()
    return job


def claim_next_job(session: Session) -> Job | None:
    """Ta nästa köade jobb och märk det som pågående."""
    job = session.execute(
        select(Job)
        .where(Job.status == JobStatus.QUEUED.value)
        .order_by(Job.created_at, Job.id)
        .limit(1)
    ).scalar_one_or_none()
    if job is None:
        return None
    job.status = JobStatus.RUNNING.value
    job.started_at = utcnow()
    session.add(job)
    session.flush()
    return job


def finish_job(
    session: Session, job: Job, status: JobStatus, message: str | None = None,
    error: str | None = None,
) -> None:
    job.status = status.value
    job.finished_at = utcnow()
    if message is not None:
        job.message = message
    if error is not None:
        job.error = error
    session.add(job)


def update_job_progress(session: Session, job: Job, done: int, total: int) -> None:
    job.progress_done = done
    job.progress_total = total
    session.add(job)
    session.commit()


def list_jobs(session: Session, limit: int = 50, active_only: bool = False) -> list[Job]:
    stmt = select(Job).order_by(Job.created_at.desc(), Job.id.desc())
    if active_only:
        stmt = stmt.where(
            Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value])
        )
    return list(session.execute(stmt.limit(limit)).scalars())


def reset_stale_running_jobs(session: Session) -> int:
    """Jobb som satt som pågående när processen dog ska inte hänga kvar."""
    stale = list(
        session.execute(
            select(Job).where(Job.status == JobStatus.RUNNING.value)
        ).scalars()
    )
    for job in stale:
        job.status = JobStatus.ERROR.value
        job.error = "Avbrutet: tjänsten startades om under körning"
        job.finished_at = utcnow()
        if job.issue_id:
            issue = session.get(Issue, job.issue_id)
            if issue and issue.status in (
                IssueStatus.QUEUED.value, IssueStatus.DOWNLOADING.value
            ):
                issue.status = IssueStatus.ERROR.value
                issue.error = job.error
                session.add(issue)
        session.add(job)
    return len(stale)


def purge_old_jobs(session: Session, days: int = 30) -> int:
    cutoff = utcnow() - timedelta(days=days)
    old = list(
        session.execute(
            select(Job).where(
                Job.finished_at.is_not(None),
                Job.finished_at < cutoff,
            )
        ).scalars()
    )
    for job in old:
        session.delete(job)
    return len(old)


# --------------------------------------------------------------------------
# Inställningar
# --------------------------------------------------------------------------


def get_setting(session: Session, key: str, default: str = "") -> str:
    row = session.get(Setting, key)
    return row.value if row else default


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
    else:
        row.value = value
    session.add(row)


def all_settings(session: Session) -> dict[str, str]:
    rows = session.execute(select(Setting)).scalars()
    return {row.key: row.value for row in rows}
