"""Läs- och skrivhjälpare mot databasen.

Håller SQL-detaljerna borta från schemaläggaren och webblagret.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import func, or_, select
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
    Site,
    Title,
    TitleAccess,
    utcnow,
)

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    lowered = value.lower()
    for source, target in (("å", "a"), ("ä", "a"), ("ö", "o"), ("é", "e")):
        lowered = lowered.replace(source, target)
    return _SLUG_UNSAFE.sub("-", lowered).strip("-") or "publikation"


def unique_slug(session: Session, base: str, exclude_title_id: int | None = None) -> str:
    slug = slugify(base)
    candidate = slug
    counter = 2
    while True:
        stmt = select(Title).where(Title.slug == candidate)
        if exclude_title_id is not None:
            stmt = stmt.where(Title.title_id != exclude_title_id)
        if session.execute(stmt).first() is None:
            return candidate
        candidate = f"{slug}-{counter}"
        counter += 1


# --------------------------------------------------------------------------
# Sajter och titelåtkomst
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
    url = url.rstrip("/")
    site = find_site(session, url)
    if site is None:
        site = Site(url=url)
    site.textalk_auth = credentials.get("textalk-auth", credentials.get("textalk_auth", ""))
    site.auth = credentials.get("auth", "")
    # Bara en angiven cdn skriver över. Inloggningen känner inte till
    # sajtens cdn, och två av dem har en egen som annars hade nollställts.
    ny_cdn = credentials.get("cdn", "") or ""
    if ny_cdn:
        site.cdn = ny_cdn
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


def delete_site(session: Session, site: Site) -> None:
    """Ta bort sajten och titlar som därefter saknar både åtkomst och bevakning."""
    session.delete(site)
    session.flush()
    orphaner = session.execute(
        select(Title).where(~Title.accesses.any(), ~Title.publication.has())
    ).unique().scalars()
    for title in orphaner:
        session.delete(title)


def list_titles(session: Session) -> list[Title]:
    return list(
        session.execute(
            select(Title).where(Title.accesses.any()).order_by(Title.name)
        ).unique().scalars()
    )


def get_title(session: Session, title_id: int) -> Title | None:
    return session.execute(
        select(Title).where(Title.title_id == title_id)
    ).unique().scalar_one_or_none()


def get_title_by_slug(session: Session, slug: str) -> Title | None:
    return session.execute(
        select(Title).where(Title.slug == slug)
    ).unique().scalar_one_or_none()


def upsert_title_access(
    session: Session, site: Site, title_id: int, namn: str, readable: bool = True
) -> tuple[Title, bool]:
    """Skapa titeln vid behov och uppdatera sajtens åtkomstrad."""
    title = get_title(session, title_id)
    created = title is None
    if title is None:
        title = Title(title_id=title_id, name=namn, slug=unique_slug(session, namn))
        session.add(title)
        session.flush()
    elif namn and title.name != namn:
        title.name = namn

    access = session.execute(
        select(TitleAccess).where(
            TitleAccess.title_id == title_id, TitleAccess.site_id == site.id
        )
    ).unique().scalar_one_or_none()
    if access is None:
        access = TitleAccess(title_id=title_id, site_id=site.id)
    access.readable = readable
    session.add_all([title, access])
    session.flush()
    ensure_publication(session, title)
    return title, created


def remove_missing_title_accesses(
    session: Session, site: Site, current_title_ids: set[int]
) -> None:
    for access in list(site.title_accesses):
        if access.title_id not in current_title_ids:
            session.delete(access)


def title_accesses(session: Session, title_id: int) -> list[TitleAccess]:
    return list(session.execute(
        select(TitleAccess)
        .where(TitleAccess.title_id == title_id)
        .order_by(TitleAccess.site_id)
    ).unique().scalars())


def site_title_counts(session: Session, site_id: int) -> tuple[int, int]:
    total = session.execute(
        select(func.count(TitleAccess.id)).where(TitleAccess.site_id == site_id)
    ).scalar_one()
    readable = session.execute(
        select(func.count(TitleAccess.id)).where(
            TitleAccess.site_id == site_id, TitleAccess.readable.is_(True)
        )
    ).scalar_one()
    return total, readable


def title_is_readable(title: Title) -> bool:
    return any(access.readable for access in title.accesses)


def sites_for_title(session: Session, title_id: int) -> list[Site]:
    """Sajter som ger läsrätt till titeln, bäst först.

    Flera uppgifter kan ge åtkomst till samma tidning, och då ska ett
    utgånget token inte stoppa nedladdningen. Ordningen sätter fungerande
    uppgifter först; sajter utan läsrätt utesluts helt, eftersom de ger 403
    oavsett tokenets skick.
    """
    # Sortera i Python: ordningen är en bedömning, inte en kolumn.
    rang = {AuthStatus.OK.value: 0, AuthStatus.UNKNOWN.value: 1}
    sajter = list(
        session.execute(
            select(Site)
            .join(TitleAccess)
            .where(
                TitleAccess.title_id == title_id,
                TitleAccess.readable.is_(True),
            )
        ).scalars()
    )
    return sorted(sajter, key=lambda s: (rang.get(s.auth_status, 2), s.url))


def site_for_title(session: Session, title_id: int) -> Site | None:
    """Den sajt som bör användas i första hand, eller None."""
    sajter = sites_for_title(session, title_id)
    return sajter[0] if sajter else None


# --------------------------------------------------------------------------
# Bevakningar
# --------------------------------------------------------------------------


def list_publications(
    session: Session, enabled_only: bool = False, site_id: int | None = None
) -> list[Publication]:
    stmt = select(Publication).join(Title).order_by(Title.name)
    if enabled_only:
        stmt = stmt.where(Publication.enabled.is_(True))
    if site_id is not None:
        stmt = stmt.join(TitleAccess).where(TitleAccess.site_id == site_id)
    return list(session.execute(stmt).unique().scalars())


def get_publication(session: Session, publication_id: int) -> Publication | None:
    return session.execute(
        select(Publication).where(Publication.id == publication_id)
    ).unique().scalar_one_or_none()


def get_publication_for_title(session: Session, title_id: int) -> Publication | None:
    return session.execute(
        select(Publication).where(Publication.title_id == title_id)
    ).unique().scalar_one_or_none()


def get_publication_by_slug(session: Session, slug: str) -> Publication | None:
    return session.execute(
        select(Publication).join(Title).where(Title.slug == slug)
    ).unique().scalar_one_or_none()


def find_publication(session: Session, site: str, title_id: int) -> Publication | None:
    return session.execute(
        select(Publication)
        .join(TitleAccess, TitleAccess.title_id == Publication.title_id)
        .join(Site)
        .where(Site.url == site.rstrip("/"), Publication.title_id == title_id)
    ).unique().scalar_one_or_none()


def ensure_publication(session: Session, title: Title) -> tuple[Publication, bool]:
    publication = get_publication_for_title(session, title.title_id)
    created = publication is None
    if publication is None:
        publication = Publication(
            title_id=title.title_id,
            prefix=f"{title.name} - ",
            folder=title.name,
        )
        session.add(publication)
        session.flush()
    return publication, created


def upsert_publication(
    session: Session, site: Site, title_id: int, namn: str, readable: bool = True
) -> tuple[Publication, bool]:
    """Importhjälpare: säkra titelåtkomst och skapa dess bevakningsrad."""
    title, _ = upsert_title_access(session, site, title_id, namn, readable)
    return ensure_publication(session, title)


# --------------------------------------------------------------------------
# Utgåvor
# --------------------------------------------------------------------------


def _title_id(session: Session, publication_id: int) -> int:
    publication = get_publication(session, publication_id)
    return publication.title_id if publication else -1


def list_issues(
    session: Session,
    publication_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Issue]:
    stmt = select(Issue).order_by(Issue.publication_date.desc(), Issue.id.desc())
    if publication_id is not None:
        stmt = stmt.where(Issue.title_id == _title_id(session, publication_id))
    return list(session.execute(stmt.limit(limit).offset(offset)).unique().scalars())


def count_issues(session: Session, publication_id: int) -> int:
    return session.execute(
        select(func.count(Issue.id)).where(Issue.title_id == _title_id(session, publication_id))
    ).scalar_one()


def count_catalogue_issues(session: Session, publication_id: int) -> int:
    """Utgåvor som finns i Prenlys katalog, alltså utan diskfynden.

    Katalogdjupet mäts mot det här talet: räknades diskfynden in skulle
    bläddringen bakåt tro sig färdig innan katalogen var det.
    """
    return session.execute(
        select(func.count(Issue.id)).where(
            Issue.title_id == _title_id(session, publication_id),
            Issue.disk_only.is_(False),
        )
    ).scalar_one()


def count_title_issues(session: Session, title_id: int) -> int:
    return session.execute(
        select(func.count(Issue.id)).where(Issue.title_id == title_id)
    ).scalar_one()


def issues_with_files(session: Session, title_id: int) -> list[Issue]:
    """Titelns utgåvor som har en fil på disk, diskfynden inräknade."""
    return list(
        session.execute(
            select(Issue).where(
                Issue.title_id == title_id, Issue.file_path.is_not(None)
            )
        ).unique().scalars()
    )


def issue_numbers_by_date(session: Session, title: Title) -> dict[str, int]:
    """Utgivningsdatum till nummer inom året, för hela titeln på en gång.

    issue_number_in_year kostar en fråga per utgåva. Vid matchning mot
    Calibre behövs numret för varenda utgåva, och då blev det tusentals
    frågor: sidan tog sju sekunder.
    """
    if not title.catalogue_complete:
        return {}
    datum = list(
        session.execute(
            select(Issue.publication_date).where(
                Issue.title_id == title.title_id,
                Issue.disk_only.is_(False),
                Issue.publication_date.is_not(None),
            ).order_by(Issue.publication_date)
        ).scalars()
    )
    nummer: dict[str, int] = {}
    rakning: dict[str, int] = {}
    for dag in datum:
        ar = dag[:4]
        if not ar.isdigit():
            continue
        rakning[ar] = rakning.get(ar, 0) + 1
        nummer[dag] = rakning[ar]
    return nummer


def issue_number_in_year(session: Session, issue: Issue) -> int | None:
    """Utgåvans ordning inom sitt år, eller None när den inte går att lita på.

    Numret räknas ur katalogen, så det kräver att hela året finns hämtat.
    Är katalogen ofullständig kan årets tidiga nummer saknas, och då hade
    numreringen blivit fel utan att det syns.
    """
    if not issue.publication_date or not issue.title.catalogue_complete:
        return None
    ar = issue.publication_date[:4]
    if not ar.isdigit():
        return None
    datum = list(
        session.execute(
            select(Issue.publication_date).where(
                Issue.title_id == issue.title_id,
                Issue.disk_only.is_(False),
                Issue.publication_date.like(f"{ar}%"),
            ).order_by(Issue.publication_date)
        ).scalars()
    )
    try:
        return datum.index(issue.publication_date) + 1
    except ValueError:
        return None


def issues_without_calibre(session: Session, title_id: int) -> list[Issue]:
    """Titelns utgåvor som varken är kopplade eller bortvalda."""
    return list(
        session.execute(
            select(Issue).where(
                Issue.title_id == title_id,
                Issue.calibre_id.is_(None),
                Issue.calibre_ignored.is_(False),
            ).order_by(Issue.publication_date.desc())
        ).unique().scalars()
    )


def issues_with_calibre_id(session: Session, title_id: int) -> list[Issue]:
    """Titelns utgåvor som är kopplade till en bok i Calibre."""
    return list(
        session.execute(
            select(Issue).where(
                Issue.title_id == title_id, Issue.calibre_id.is_not(None)
            ).order_by(Issue.publication_date.desc())
        ).unique().scalars()
    )


def calibre_ids(session: Session) -> list[int]:
    """Alla bok-id prenly-dl känner till."""
    return list(
        session.execute(
            select(Issue.calibre_id).where(Issue.calibre_id.is_not(None))
        ).scalars()
    )


def get_issue(session: Session, issue_id: int) -> Issue | None:
    return session.execute(
        select(Issue).where(Issue.id == issue_id)
    ).unique().scalar_one_or_none()


def upsert_issue(
    session: Session, owner: Publication | Title, entry: dict
) -> tuple[Issue, bool]:
    title_id = owner.title_id
    uid = str(entry.get("uid") or entry.get("id"))
    issue = session.execute(
        select(Issue).where(Issue.title_id == title_id, Issue.uid == uid)
    ).unique().scalar_one_or_none()
    created = issue is None
    if issue is None:
        issue = Issue(title_id=title_id, uid=uid)
    issue.name = str(entry.get("name") or uid)
    issue.publication_date = entry.get("publication_date")
    issue.cover_checksum = (entry.get("media") or {}).get("checksum")
    if created:
        _ta_over_diskpost(session, issue)
    session.add(issue)
    session.flush()
    return issue, created


def _ta_over_diskpost(session: Session, issue: Issue) -> None:
    """Låt en katalogutgåva ärva filen från en diskpost med samma namn.

    Utan det får publikationen två rader för samma utgåva så fort en fil
    som fallit ur katalogen dyker upp i den igen. Namnet är det enda de
    två har gemensamt: diskposten har inget uid från Prenly.
    """
    diskpost = session.execute(
        select(Issue).where(
            Issue.title_id == issue.title_id,
            Issue.disk_only.is_(True),
            Issue.name == issue.name,
        )
    ).unique().scalar_one_or_none()
    if diskpost is None:
        return
    issue.file_path = diskpost.file_path
    issue.page_count = diskpost.page_count
    issue.downloaded_at = diskpost.downloaded_at
    issue.status = diskpost.status
    session.delete(diskpost)
    session.flush()


def latest_issue(session: Session, publication_id: int) -> Issue | None:
    issues = list_issues(session, publication_id, limit=1)
    return issues[0] if issues else None


def latest_title_issue(session: Session, title_id: int) -> Issue | None:
    return session.execute(
        select(Issue)
        .where(Issue.title_id == title_id)
        .order_by(Issue.publication_date.desc(), Issue.id.desc())
        .limit(1)
    ).unique().scalar_one_or_none()


def issues_pending_download(session: Session, publication_id: int) -> list[Issue]:
    return list(session.execute(
        select(Issue).where(
            Issue.title_id == _title_id(session, publication_id),
            Issue.status.in_([IssueStatus.NEW.value, IssueStatus.ERROR.value]),
        )
    ).unique().scalars())


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
    # En diskutgåva har inget uid hos Prenly. Knapparna är borta i
    # tabellen, men en sida som renderats innan importen kan fortfarande
    # posta id:t, och jobbet skulle då skriva över filen med ett fel.
    if issue.disk_only:
        return None
    if has_active_job(session, issue.id):
        return None
    publication = get_publication_for_title(session, issue.title_id)
    if publication is None:
        return None
    job = Job(
        type=JobType.DOWNLOAD.value,
        status=JobStatus.QUEUED.value,
        publication_id=publication.id,
        issue_id=issue.id,
        force=force,
    )
    issue.status = IssueStatus.QUEUED.value
    issue.error = None
    session.add_all([job, issue])
    session.flush()
    return job


def enqueue_poll(session: Session, publication_id: int | None = None) -> Job:
    job = Job(type=JobType.POLL.value, status=JobStatus.QUEUED.value,
              publication_id=publication_id)
    session.add(job)
    session.flush()
    return job


def enqueue_site_sync(session: Session, site_id: int) -> Job | None:
    pagaende = session.execute(select(Job).where(
        Job.type == JobType.SITE_SYNC.value,
        Job.site_id == site_id,
        Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
    )).first()
    if pagaende:
        return None
    job = Job(type=JobType.SITE_SYNC.value, status=JobStatus.QUEUED.value, site_id=site_id)
    session.add(job)
    session.flush()
    return job


def enqueue_catalogue(session: Session, publication_id: int) -> Job | None:
    pagaende = session.execute(select(Job).where(
        Job.type == JobType.CATALOGUE.value,
        Job.publication_id == publication_id,
        Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
    )).first()
    if pagaende:
        return None
    job = Job(type=JobType.CATALOGUE.value, status=JobStatus.QUEUED.value,
              publication_id=publication_id)
    session.add(job)
    session.flush()
    return job


def enqueue_headlines(session: Session, publication_id: int) -> Job | None:
    pagaende = session.execute(select(Job).where(
        Job.type == JobType.HEADLINES.value,
        Job.publication_id == publication_id,
        Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
    )).first()
    if pagaende:
        return None
    job = Job(type=JobType.HEADLINES.value, status=JobStatus.QUEUED.value,
              publication_id=publication_id)
    session.add(job)
    session.flush()
    return job


def enqueue_retitle(session: Session) -> Job | None:
    return _enqueue_singeljobb(session, JobType.RETITLE)


def enqueue_enrich(session: Session) -> Job | None:
    return _enqueue_singeljobb(session, JobType.ENRICH)


def _enqueue_singeljobb(session: Session, typ: JobType) -> Job | None:
    """Ett jobb av typen åt gången, oavsett publikation."""
    pagaende = session.execute(select(Job).where(
        Job.type == typ.value,
        Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
    )).first()
    if pagaende:
        return None
    job = Job(type=typ.value, status=JobStatus.QUEUED.value)
    session.add(job)
    session.flush()
    return job


def issues_without_headline(session: Session, title_id: int) -> list[Issue]:
    """Utgåvor vars rubrik inte är hämtat, nyast först."""
    return list(
        session.execute(
            select(Issue).where(
                Issue.title_id == title_id,
                Issue.disk_only.is_(False),
                or_(Issue.headline == "", Issue.headline.is_(None)),
            ).order_by(Issue.publication_date.desc())
        ).unique().scalars()
    )


def issue_years(session: Session, publication_id: int) -> list[str]:
    rows = session.execute(
        select(func.substr(Issue.publication_date, 1, 4))
        .where(Issue.title_id == _title_id(session, publication_id),
               Issue.publication_date.is_not(None))
        .distinct()
    ).scalars()
    return sorted((ar for ar in rows if ar), reverse=True)


def issues_newer_than_year(session: Session, publication_id: int, ar: str) -> int:
    return session.execute(select(func.count(Issue.id)).where(
        Issue.title_id == _title_id(session, publication_id),
        Issue.publication_date.is_not(None),
        Issue.publication_date >= f"{int(ar) + 1}-01-01",
    )).scalar_one()
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
