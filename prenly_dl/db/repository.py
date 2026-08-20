"""Läs- och skrivhjälpare mot databasen.

Håller SQL-detaljerna borta från schemaläggaren och webblagret.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..api import Credentials
from .models import (
    AuthStatus,
    Site,
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
# Sajter
# --------------------------------------------------------------------------


def list_sites(session: Session) -> list[Site]:
    return list(session.execute(select(Site).order_by(Site.url)).scalars())


def get_site(session: Session, site_id: int) -> Site | None:
    return session.get(Site, site_id)


def find_site(session: Session, url: str) -> Site | None:
    return session.execute(
        select(Site).where(Site.url == url.rstrip("/"))
    ).scalar_one_or_none()


def upsert_site(session: Session, url: str, credentials: dict) -> Site:
    """Skapa eller uppdatera en sajt med dess inloggningsuppgifter.

    Uppgifterna hör till sajten, så en förnyelse här gäller omedelbart för
    alla titlar sajten exponerar.
    """
    url = url.rstrip("/")
    site = find_site(session, url)
    if site is None:
        site = Site(url=url)
    site.textalk_auth = credentials.get("textalk-auth", credentials.get("textalk_auth", ""))
    site.auth = credentials.get("auth", "")
    site.cdn = credentials.get("cdn", "") or ""
    session.add(site)
    session.flush()
    return site


def credentials_for_site(site: Site) -> Credentials:
    return Credentials(
        site=site.url,
        textalk_auth=site.textalk_auth,
        auth=site.auth,
        cdn=site.cdn or "",
    )


def set_site_status(
    session: Session, site: Site, status: AuthStatus, message: str | None = None
) -> None:
    site.auth_status = status.value
    site.auth_message = message
    site.auth_checked_at = utcnow()
    session.add(site)


# --------------------------------------------------------------------------
# Publikationer
# --------------------------------------------------------------------------


def list_publications(
    session: Session, enabled_only: bool = False, site_id: int | None = None
) -> list[Publication]:
    stmt = select(Publication).order_by(Publication.name)
    if enabled_only:
        stmt = stmt.where(Publication.enabled.is_(True))
    if site_id is not None:
        stmt = stmt.where(Publication.site_id == site_id)
    return list(session.execute(stmt).scalars())


def get_publication(session: Session, publication_id: int) -> Publication | None:
    return session.get(Publication, publication_id)


def get_publication_by_slug(session: Session, slug: str) -> Publication | None:
    return session.execute(
        select(Publication).where(Publication.slug == slug)
    ).scalar_one_or_none()


def find_publication(session: Session, site: str, title_id: int) -> Publication | None:
    """Slå upp en publikation på sajtens URL och titel-ID."""
    row = session.execute(
        select(Publication)
        .join(Site)
        .where(Site.url == site.rstrip("/"), Publication.title_id == title_id)
    ).scalar_one_or_none()
    return row


def credentials_for(publication: Publication) -> Credentials:
    return credentials_for_site(publication.site)


def upsert_publication(
    session: Session, site: Site, title_id: int, namn: str, readable: bool = True,
) -> tuple[Publication, bool]:
    """Skapa eller uppdatera en titel under en sajt. Returnerar (rad, ny)."""
    publication = session.execute(
        select(Publication).where(
            Publication.site_id == site.id, Publication.title_id == title_id
        )
    ).scalar_one_or_none()

    created = publication is None
    if publication is None:
        publication = Publication(
            site_id=site.id,
            title_id=title_id,
            name=namn,
            slug=unique_slug(session, namn),
            prefix=f"{namn} - ",
            folder=namn,
        )
    elif namn and publication.name != namn:
        publication.name = namn
    publication.readable = readable
    session.add(publication)
    session.flush()
    return publication, created


# --------------------------------------------------------------------------
# Utgåvor
# --------------------------------------------------------------------------


def list_issues(
    session: Session,
    publication_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Issue]:
    stmt = select(Issue).order_by(Issue.publication_date.desc(), Issue.id.desc())
    if publication_id is not None:
        stmt = stmt.where(Issue.publication_id == publication_id)
    return list(session.execute(stmt.limit(limit).offset(offset)).scalars())


def count_issues(session: Session, publication_id: int) -> int:
    return session.execute(
        select(func.count(Issue.id)).where(Issue.publication_id == publication_id)
    ).scalar_one()


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


def latest_issue(session: Session, publication_id: int) -> Issue | None:
    """Senaste kända utgåvan, oavsett om den hämtats."""
    issues = list_issues(session, publication_id, limit=1)
    return issues[0] if issues else None


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
        force=force,
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


def enqueue_catalogue(session: Session, publication_id: int) -> Job | None:
    """Köa en full kataloghämtning om det inte redan finns en."""
    pagaende = session.execute(
        select(Job).where(
            Job.type == JobType.CATALOGUE.value,
            Job.publication_id == publication_id,
            Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
        )
    ).first()
    if pagaende:
        return None
    job = Job(
        type=JobType.CATALOGUE.value,
        status=JobStatus.QUEUED.value,
        publication_id=publication_id,
    )
    session.add(job)
    session.flush()
    return job


def issue_years(session: Session, publication_id: int) -> list[str]:
    """Åren som publikationens kända utgåvor spänner över, nyast först."""
    rows = session.execute(
        select(func.substr(Issue.publication_date, 1, 4))
        .where(
            Issue.publication_id == publication_id,
            Issue.publication_date.is_not(None),
        )
        .distinct()
    ).scalars()
    return sorted((ar for ar in rows if ar), reverse=True)


def issues_newer_than_year(session: Session, publication_id: int, ar: str) -> int:
    """Antal utgåvor som ligger före årets första, i visningsordningen."""
    return session.execute(
        select(func.count(Issue.id)).where(
            Issue.publication_id == publication_id,
            Issue.publication_date.is_not(None),
            Issue.publication_date >= f"{int(ar) + 1}-01-01",
        )
    ).scalar_one()


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


def update_job_progress(job_id: int, done: int, total: int) -> None:
    """Skriv förloppet i en egen transaktion.

    Nedladdningens session får inte committas mitt i jobbet: då blir ett
    halvfärdigt tillstånd bestående om utgåvan misslyckas efteråt.
    """
    from .session import get_session

    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.progress_done = done
        job.progress_total = total
        session.add(job)


def list_jobs(session: Session, limit: int = 50, active_only: bool = False) -> list[Job]:
    stmt = select(Job).order_by(Job.created_at.desc(), Job.id.desc())
    if active_only:
        stmt = stmt.where(
            Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value])
        )
    return list(session.execute(stmt.limit(limit)).scalars())


def job_counts(session: Session) -> dict[str, int]:
    """Antal jobb per status, för filterknapparna på jobbsidan."""
    rows = session.execute(
        select(Job.status, func.count(Job.id)).group_by(Job.status)
    ).all()
    counts = {status.value: 0 for status in JobStatus}
    for status, antal in rows:
        counts[status] = antal
    counts["alla"] = sum(antal for _, antal in rows)
    return counts


def dashboard_stats(session: Session) -> dict[str, int]:
    """Siffrorna överst på översikten."""
    publikationer = session.execute(select(func.count(Publication.id))).scalar_one()
    aktiva = session.execute(
        select(func.count(Publication.id)).where(Publication.enabled.is_(True))
    ).scalar_one()
    sajter = session.execute(select(func.count(Site.id))).scalar_one()
    utgavor = session.execute(select(func.count(Issue.id))).scalar_one()
    hamtade = session.execute(
        select(func.count(Issue.id)).where(Issue.status == IssueStatus.DONE.value)
    ).scalar_one()
    counts = job_counts(session)
    return {
        "publikationer": publikationer,
        "aktiva": aktiva,
        "sajter": sajter,
        "utgavor": utgavor,
        "hamtade": hamtade,
        "koade": counts.get(JobStatus.QUEUED.value, 0),
        "kor": counts.get(JobStatus.RUNNING.value, 0),
        "fel": counts.get(JobStatus.ERROR.value, 0),
    }


def get_job(session: Session, job_id: int) -> Job | None:
    return session.get(Job, job_id)


def list_jobs_filtered(
    session: Session, status: str | None = None, limit: int = 100
) -> list[Job]:
    stmt = select(Job).order_by(Job.created_at.desc(), Job.id.desc())
    if status and status != "alla":
        stmt = stmt.where(Job.status == status)
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
