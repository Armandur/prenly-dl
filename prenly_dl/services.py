"""Domänlogik som delas av schemaläggaren, webbui:t och CLI:t."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from .api import (
    Credentials,
    build_session,
    get_catalogue_issues,
    get_context_token,
    get_titles,
)
from .config import output_root
from .covers import fetch_and_cache_cover
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
from .downloader import (
    download_issue,
    preview_output_dir,
    safe_filename,
    safe_output_directory,
)
from .errors import PrenlyAuthError, PrenlyError, PrenlySubscriptionError

logger = logging.getLogger(__name__)


def publication_output_dir(publication: Publication) -> Path:
    folder = publication.folder.strip() or publication.name
    if publication.main_publication is None:
        return safe_output_directory(output_root(), folder)
    # Bilagan hamnar under huvudpublikationen, och båda leden måste hålla
    # sig inom sökvägsgränsen.
    huvud = publication.main_publication
    huvudmapp = huvud.folder.strip() or huvud.name
    return safe_output_directory(
        safe_output_directory(output_root(), huvudmapp), folder
    )


def set_main_publication(
    session: Session,
    publication: Publication,
    main_publication: Publication | None,
    *,
    folder: str | None = None,
    name: str | None = None,
) -> int:
    """Koppla om en bilaga och flytta dess befintliga katalog."""
    moved, _ = update_publication_storage(
        session,
        publication,
        main_publication,
        folder=folder,
        name=name,
    )
    return moved


def update_publication_storage(
    session: Session,
    publication: Publication,
    main_publication: Publication | None,
    *,
    folder: str | None = None,
    name: str | None = None,
    prefix: str | None = None,
) -> tuple[int, int]:
    """Uppdatera publikationen och flytta eller döp om hämtade filer."""
    if main_publication is publication:
        raise ValueError("En publikation kan inte vara bilaga till sig själv")
    if main_publication is not None and main_publication.main_publication is not None:
        raise ValueError("En bilaga kan inte vara huvudpublikation för en annan bilaga")
    if main_publication is not None and publication.attachments:
        raise ValueError("En huvudpublikation med bilagor kan inte själv bli bilaga")
    current_main_id = (
        publication.main_publication.id if publication.main_publication is not None else None
    )
    new_main_id = main_publication.id if main_publication is not None else None
    new_folder = publication.folder if folder is None else folder
    new_name = publication.name if name is None else name
    new_prefix = publication.prefix if prefix is None else prefix
    if (
        current_main_id == new_main_id
        and publication.folder == new_folder
        and publication.name == new_name
        and publication.prefix == new_prefix
    ):
        return 0, 0

    old_dir = publication_output_dir(publication)
    old_main = publication.main_publication
    old_folder = publication.folder
    old_name = publication.name
    publication.main_publication = main_publication
    publication.folder = new_folder
    publication.name = new_name
    new_dir = publication_output_dir(publication)
    publication.main_publication = old_main
    publication.folder = old_folder
    publication.name = old_name

    if old_dir != new_dir and new_dir.exists():
        raise ValueError(f"Målkatalogen finns redan: {new_dir}")

    moved = 0
    renamed = 0
    paths: list[tuple[Issue, Path, Path]] = []
    if old_dir.is_dir():
        old_root = old_dir.resolve()
        # Frågan går till databasen: title.issues kan vara läst innan
        # importen skapade diskposterna, och då hade de inte döpts om.
        for issue in repo.issues_with_files(session, publication.title_id):
            if not issue.file_path:
                continue
            path = Path(issue.file_path)
            try:
                relative = path.resolve().relative_to(old_root)
            except (OSError, ValueError):
                continue
            path = old_dir / relative
            target = new_dir / relative
            if path.is_file() and publication.prefix != new_prefix:
                target = new_dir / f"{safe_filename(f'{new_prefix}{issue.name}')}.pdf"
            paths.append((issue, path, target))

    destinations: dict[Path, Issue] = {}
    for issue, source, target in paths:
        if target in destinations:
            raise ValueError(f"Flera utgåvor skulle få samma filnamn: {target.name}")
        destinations[target] = issue
        existing_target = (
            old_dir / target.relative_to(new_dir) if old_dir != new_dir else target
        )
        if source != target and existing_target.exists() and existing_target != source:
            raise ValueError(f"Målfilen finns redan: {target}")

    if old_dir.is_dir() and old_dir != new_dir:
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        old_dir.rename(new_dir)
        moved = sum(1 for path in new_dir.rglob("*.pdf") if path.is_file())

    for issue, source, target in paths:
        current = new_dir / source.relative_to(old_dir) if old_dir != new_dir else source
        if current != target:
            current.rename(target)
            renamed += 1
        if issue.file_path != str(target):
            issue.file_path = str(target)
            session.add(issue)

    publication.main_publication = main_publication
    publication.folder = new_folder
    publication.name = new_name
    publication.prefix = new_prefix
    session.add(publication)
    return moved, renamed


def build_issue_preview(
    creds: Credentials,
    title_id: int,
    issue_uid: str,
    issue_id: int,
    prefix: str = "",
) -> Path:
    """Bygg eller återanvänd en PDF utan att ändra utgåvan i databasen."""
    output_dir = preview_output_dir(issue_id)
    cached = next(output_dir.glob("*.pdf"), None) if output_dir.is_dir() else None
    if cached is not None:
        return cached
    result = download_issue(
        build_session(),
        creds,
        title_id,
        issue_uid,
        output_dir,
        prefix=prefix,
    )
    return result.path


def _site_for_publication(session: Session, publication: Publication):
    site = repo.site_for_title(session, publication.title_id)
    if site is None:
        raise PrenlySubscriptionError(
            f"Ingen av dina inloggningsuppgifter ger läsrätt till {publication.name}"
        )
    return site


def _record_error(
    session: Session, publication: Publication, site, error: Exception
) -> None:
    """Skriv titel- eller autentiseringsfelet där det hör hemma."""
    publication.last_error = str(error)
    session.add(publication)
    if isinstance(error, PrenlySubscriptionError):
        if site is not None:
            for access in publication.accesses:
                if access.site_id == site.id:
                    access.readable = False
                    session.add(access)
        return
    if isinstance(error, PrenlyAuthError):
        repo.set_site_status(session, site, AuthStatus.AUTH_ERROR, str(error))
    elif site is not None:
        repo.set_site_status(session, site, AuthStatus.ERROR, str(error))


def synka_titlar(session: Session, site, titles: list[dict]) -> tuple[int, int]:
    """Spegla sajtens titellista till publikationer. Returnerar (nya, totalt).

    Varje titel sajten exponerar får en rad, så katalogen går att bläddra i
    utan API-anrop och visar vad kontot hade tillgång till även när
    uppgifterna slutat gälla.
    """
    nya = 0
    for titel in titles:
        title_id = titel.get("id")
        if title_id is None:
            continue
        _, created = repo.upsert_title_access(
            session,
            site,
            int(title_id),
            str(titel.get("name") or f"Publikation {title_id}"),
            readable=bool(titel.get("user_readable", True)),
        )
        if created:
            nya += 1
    repo.remove_missing_title_accesses(
        session, site, {int(t["id"]) for t in titles if t.get("id") is not None}
    )
    repo.set_site_status(session, site, AuthStatus.OK, None)
    return nya, len(titles)


def verify_site(session: Session, site) -> AuthStatus:
    """Kontrollera sajtens uppgifter och uppdatera dess titlar."""
    http = build_session()
    creds = repo.credentials_for_site(site)
    try:
        token = get_context_token(http, creds)
        titles = get_titles(http, creds, token)
    except PrenlyAuthError as error:
        repo.set_site_status(session, site, AuthStatus.AUTH_ERROR, str(error))
        return AuthStatus(site.auth_status)
    except PrenlyError as error:
        repo.set_site_status(session, site, AuthStatus.ERROR, str(error))
        return AuthStatus(site.auth_status)

    synka_titlar(session, site, titles)
    return AuthStatus(site.auth_status)


def verify_credentials(session: Session, publication: Publication) -> AuthStatus:
    """Kontrollera sajtens uppgifter utifrån en av dess publikationer."""
    return verify_site(session, _site_for_publication(session, publication))


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
    site = _site_for_publication(session, publication)
    creds = repo.credentials_for_site(site)
    limit = max(1, publication.watch_limit)

    token = get_context_token(http, creds)
    entries = get_catalogue_issues(http, creds, token, publication.title_id, limit)
    titles = get_titles(http, creds, token)

    new_issues = 0
    queued = 0
    for entry in entries:
        # Omslaget hämtas oavsett om PDF:en går att ladda ner, så kortet
        # kan visa titelsidan även för en publikation utan prenumeration.
        issue, created = _spara_katalogpost(session, publication, http, creds, entry)
        if created:
            new_issues += 1
        if enqueue and issue.status in (IssueStatus.NEW.value, IssueStatus.ERROR.value):
            if repo.enqueue_download(session, issue):
                queued += 1

    publication.catalogue_offset = max(publication.catalogue_offset + new_issues, len(entries))
    publication.last_error = None
    session.add(publication)
    # Katalogen svarar även när prenumerationen gått ut, så titellistan är
    # det som avgör om utgåvorna faktiskt går att hämta.
    synka_titlar(session, site, titles)
    logger.info(
        "%s: %d utgåvor i katalogen, %d nya, %d köade",
        publication.name, len(entries), new_issues, queued,
    )
    return {"found": len(entries), "new": new_issues, "queued": queued}


# Hur många katalogposter som hämtas i taget. API:t kapar limit vid 100,
# men offset går djupt - en dagstidning har flera tusen utgåvor i arkivet,
# så det lönar sig att ta största tillåtna bit per anrop.
CATALOGUE_CHUNK = 100


def _spara_katalogpost(
    session: Session, publication: Publication, http, creds, entry: dict,
    hamta_omslag: bool = True,
):
    issue, created = repo.upsert_issue(session, publication, entry)
    if hamta_omslag and issue.cover_checksum:
        fetch_and_cache_cover(http, creds, publication.title_id, issue.cover_checksum)
    return issue, created


def ensure_catalogue_depth(
    session: Session,
    publication: Publication,
    behovs: int,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Se till att minst `behovs` utgåvor är kända, genom att bläddra bakåt.

    Utgåvor som redan hämtats läses ur databasen: en äldre utgåva ändras
    inte, så den behöver aldrig hämtas om. Returnerar antalet nya utgåvor.
    """
    if publication.catalogue_complete:
        return 0
    if repo.count_catalogue_issues(session, publication.id) >= behovs:
        return 0

    http = build_session()
    site = _site_for_publication(session, publication)
    creds = repo.credentials_for_site(site)
    token = get_context_token(http, creds)

    nya = 0
    # Varje varv flyttar catalogue_offset framåt, så loopen kan inte fastna.
    while (
        not publication.catalogue_complete
        and repo.count_catalogue_issues(session, publication.id) < behovs
    ):
        entries = get_catalogue_issues(
            http, creds, token, publication.title_id,
            limit=CATALOGUE_CHUNK, offset=publication.catalogue_offset,
        )
        if not entries:
            publication.catalogue_complete = True
            break
        for entry in entries:
            # Omslagen hämtas inte här: 20 bilder i följd skulle göra
            # sidladdningen halvminuten lång. /covers-rutten hämtar dem
            # lat och parallellt när webbläsaren efterfrågar dem.
            _, created = _spara_katalogpost(
                session, publication, http, creds, entry, hamta_omslag=False
            )
            if created:
                nya += 1
        publication.catalogue_offset += len(entries)
        if len(entries) < CATALOGUE_CHUNK:
            publication.catalogue_complete = True
        session.add(publication)
        session.commit()
        if progress:
            kanda = repo.count_catalogue_issues(session, publication.id)
            progress(kanda, kanda if publication.catalogue_complete else behovs)

    if nya:
        logger.info("%s: hämtade %d äldre utgåvor ur katalogen", publication.name, nya)
    session.add(publication)
    return nya


def run_poll_job(session: Session, job: Job) -> str:
    """Kör ett poll-jobb för en eller alla aktiva publikationer."""
    if job.publication_id:
        publication = repo.get_publication(session, job.publication_id)
        publications = [publication] if publication else []
    else:
        publications = repo.list_publications(session, enabled_only=True)

    totals = {"found": 0, "new": 0, "queued": 0}
    failures: list[str] = []
    for publication in publications:
        site = repo.site_for_title(session, publication.title_id)
        try:
            result = poll_publication(session, publication)
        except PrenlyError as error:
            _record_error(session, publication, site, error)
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


def run_site_sync_job(session: Session, job: Job) -> str:
    """Uppdatera en sajts titlar och hämta senaste utgåvan för var och en.

    Ger katalogen ett omslag per titel. Katalog-API:t kan ta flera
    title_ids i ett anrop, men returnerar då bara de nyaste utgåvorna över
    alla titlar - för Ångermanland täckte det 10 av 30. Ett anrop per titel
    är alltså vad som krävs, och därför hör det hemma i kön.
    """
    site = repo.get_site(session, job.site_id) if job.site_id else None
    if site is None:
        raise PrenlyError("Jobbet saknar sajt")

    http = build_session()
    creds = repo.credentials_for_site(site)
    token = get_context_token(http, creds)
    nya_titlar, totalt = synka_titlar(session, site, get_titles(http, creds, token))
    session.commit()

    # Sajten används här uttryckligen: synkjobbet ska prova varje titel som
    # just den sajten exponerar, även när läsrätten är falsk.
    titlar = [access.title for access in site.title_accesses]
    job_id = job.id
    omslag = 0
    for index, title in enumerate(titlar, start=1):
        try:
            entries = get_catalogue_issues(
                http, creds, token, title.title_id, limit=1
            )
        except PrenlyError as error:
            logger.warning("Kunde inte hämta %s: %s", title.name, error)
            continue
        for entry in entries:
            issue, _ = repo.upsert_issue(session, title, entry)
            title.cover_checksum = issue.cover_checksum
            if issue.cover_checksum and fetch_and_cache_cover(
                http, creds, title.title_id, issue.cover_checksum
            ):
                omslag += 1
        session.add(title)
        session.commit()
        repo.update_job_progress(job_id, index, len(titlar))

    return f"{totalt} titlar ({nya_titlar} nya), {omslag} omslag hämtade"


def run_catalogue_job(session: Session, job: Job) -> str:
    """Hämta hela katalogen för en publikation.

    Katalogen är som mest ett hundratal poster, så det här är billigt -
    men det är flera anrop i följd och hör därför hemma i kön.
    """
    publication = (
        repo.get_publication(session, job.publication_id) if job.publication_id else None
    )
    if publication is None:
        raise PrenlyError("Jobbet saknar publikation")

    innan = repo.count_catalogue_issues(session, publication.id)
    job_id = job.id

    def progress(gjort: int, totalt: int) -> None:
        repo.update_job_progress(job_id, gjort, totalt)

    # Ett tak som ligger långt över vad API:t levererar, så loopen alltid
    # slutar på catalogue_complete och inte på taket. En dagstidning har
    # flera tusen utgåvor, så förloppet är värt att visa.
    nya = ensure_catalogue_depth(session, publication, 100_000, progress=progress)
    totalt = repo.count_catalogue_issues(session, publication.id)
    return f"{totalt} utgåvor i katalogen, {nya} nya (hade {innan})"


def run_download_job(session: Session, job: Job) -> str:
    """Ladda ner utgåvan som jobbet pekar på."""
    issue: Issue | None = repo.get_issue(session, job.issue_id) if job.issue_id else None
    if issue is None:
        raise PrenlyError("Jobbet saknar utgåva")

    publication = repo.get_publication_for_title(session, issue.title_id)
    if publication is None:
        raise PrenlyError("Utgåvan saknar publikation")

    issue.status = IssueStatus.DOWNLOADING.value
    session.add(issue)
    session.commit()

    job_id = job.id

    def progress(done: int, total: int) -> None:
        repo.update_job_progress(job_id, done, total)

    sajter = repo.sites_for_title(session, publication.title_id)
    if not sajter:
        fel = PrenlySubscriptionError(
            f"Ingen av dina inloggningsuppgifter ger läsrätt till {publication.name}"
        )
        issue.status = IssueStatus.ERROR.value
        issue.error = str(fel)
        session.add(issue)
        raise fel

    http = build_session()
    result, site, provade = None, None, []
    for kandidat in sajter:
        try:
            result = download_issue(
                http,
                repo.credentials_for_site(kandidat),
                publication.title_id,
                issue.uid,
                publication_output_dir(publication),
                prefix=publication.prefix,
                force=job.force,
                progress=progress,
            )
        except (PrenlyAuthError, PrenlySubscriptionError) as error:
            # Behörighetsfel gäller den här sajten. En annan uppgift kan
            # fungera, så gå vidare i stället för att fälla jobbet.
            _record_error(session, publication, kandidat, error)
            session.commit()
            provade.append(f"{kandidat.url}: {error}")
            continue
        except PrenlyError as error:
            # Nätverksfel och trasiga filer botas inte av en annan auth.
            issue.status = IssueStatus.ERROR.value
            issue.error = str(error)
            session.add(issue)
            _record_error(session, publication, kandidat, error)
            raise
        site = kandidat
        break

    if result is None:
        fel = PrenlyError(
            f"Ingen fungerande inloggning för {publication.name}: "
            + "; ".join(provade)
        )
        issue.status = IssueStatus.ERROR.value
        issue.error = str(fel)
        session.add(issue)
        raise fel

    if site is not sajter[0]:
        logger.info(
            "%s hämtades via %s sedan %s fallerat",
            publication.name, site.url, sajter[0].url,
        )

    issue.status = IssueStatus.DONE.value
    issue.error = None
    issue.file_path = str(result.path)
    if not result.skipped:
        issue.page_count = result.page_count
        issue.downloaded_at = utcnow()
    session.add(issue)
    # En lyckad nedladdning bevisar att både uppgifterna och läsrätten
    # fungerar.
    publication.last_error = None
    session.add(publication)
    repo.set_site_status(session, site, AuthStatus.OK, None)

    if result.skipped:
        return f"{result.path.name} fanns redan"
    return f"{result.path.name} ({result.page_count} sidor)"


def execute_job(session: Session, job: Job) -> None:
    """Kör ett jobb och skriv utfallet till jobbraden."""
    try:
        if job.type == JobType.POLL.value:
            summary = run_poll_job(session, job)
        elif job.type == JobType.CATALOGUE.value:
            summary = run_catalogue_job(session, job)
        elif job.type == JobType.SITE_SYNC.value:
            summary = run_site_sync_job(session, job)
        elif job.type == JobType.DOWNLOAD.value:
            summary = run_download_job(session, job)
        else:
            raise PrenlyError(f"Okänd jobbtyp: {job.type}")
    except Exception as error:  # noqa: BLE001 - jobbet ska aldrig fälla arbetaren
        logger.exception("Jobb %s misslyckades", job.id)
        repo.finish_job(session, job, JobStatus.ERROR, error=str(error))
    else:
        repo.finish_job(session, job, JobStatus.DONE, message=summary)
