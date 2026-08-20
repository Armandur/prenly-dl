"""HTTP-rutter för webbgränssnittet."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ..api import Credentials, build_session
from ..config import output_root, timezone_name
from ..covers import cached_cover_path, fetch_and_cache_cover
from ..db import repository as repo
from ..db.models import AuthStatus, Publication
from ..db.session import get_session
from ..errors import PrenlyError
from ..importer import import_conf, import_directory, strip_json_comments
from ..scheduler import (
    DEFAULT_JOB_HISTORY_DAYS,
    DEFAULT_POLL_CRON,
    JOB_HISTORY_DAYS_KEY,
    POLL_CRON_KEY,
    next_poll_time,
    reschedule_poll,
)
from ..services import (
    discover_titles,
    ensure_catalogue_depth,
    poll_publication,
    verify_credentials,
)
from .auth import check_password, is_authenticated
from .rendering import STATUS_LABELS, back, flash, render

logger = logging.getLogger(__name__)

router = APIRouter()

# Antal utgåvor per sida i utgåvetabellen.
PAGE_SIZE = 20


# --------------------------------------------------------------------------
# Inloggning
# --------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if is_authenticated(request):
        return back("/")
    return render(request, "login.html")


@router.post("/login")
def login(request: Request, password: str = Form("")):
    if check_password(password):
        request.session["authenticated"] = True
        return back("/")
    flash(request, "Fel lösenord", "error")
    return back("/login")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


# --------------------------------------------------------------------------
# Översikt
# --------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    with get_session() as session:
        publications = repo.list_publications(session)
        stats = repo.dashboard_stats(session)
        jobs = repo.list_jobs(session, limit=10)
        issues = repo.list_issues(session, limit=10)
        rows = [
            {"publication": publication, "latest": repo.latest_issue(session, publication.id)}
            for publication in publications
        ]
    return render(
        request,
        "index.html",
        stats=stats,
        rows=rows,
        jobs=jobs,
        issues=issues,
        next_poll=next_poll_time(),
        output_dir=output_root(),
    )


# --------------------------------------------------------------------------
# Publikationer
# --------------------------------------------------------------------------


@router.get("/covers/{publication_id}/{checksum}.jpg")
def cover_image(publication_id: int, checksum: str):
    """Servera ett cachat omslag, och hämta det vid första förfrågan.

    Bilden är innehållsadresserad via checksumman, så den kan cachas hårt
    i webbläsaren.
    """
    headers = {"Cache-Control": "public, max-age=604800, immutable"}
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            return JSONResponse({"error": "okänd publikation"}, status_code=404)
        title_id = publication.title_id
        creds = repo.credentials_for(publication)

    path = cached_cover_path(title_id, checksum)
    if path is None:
        path = fetch_and_cache_cover(build_session(), creds, title_id, checksum)
    if path is None:
        return JSONResponse({"error": "omslaget kunde inte hämtas"}, status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers=headers)


@router.get("/publications/new", response_class=HTMLResponse)
def new_publication_form(request: Request):
    return render(request, "publication_form.html", publication=None, titles=None)


def _credentials_from_form(site: str, textalk_auth: str, auth: str, cdn: str):
    return Credentials(site=site, textalk_auth=textalk_auth, auth=auth, cdn=cdn)


@router.post("/publications/discover", response_class=HTMLResponse)
def discover(
    request: Request,
    conf_json: str = Form(""),
    site: str = Form(""),
    textalk_auth: str = Form(""),
    auth: str = Form(""),
    cdn: str = Form(""),
):
    """Visa vilka publikationer uppgifterna ger tillgång till."""
    prefill = {"site": site, "textalk_auth": textalk_auth, "auth": auth, "cdn": cdn}
    if conf_json.strip():
        try:
            conf = json.loads(strip_json_comments(conf_json))
            credentials = conf.get("credentials", {})
            prefill = {
                "site": credentials.get("site", ""),
                "textalk_auth": credentials.get("textalk-auth", ""),
                "auth": credentials.get("auth", ""),
                "cdn": credentials.get("cdn", ""),
            }
        except ValueError as error:
            flash(request, f"Kunde inte tolka JSON: {error}", "error")
            return back("/publications/new")

    try:
        titles = discover_titles(_credentials_from_form(**prefill))
    except PrenlyError as error:
        flash(request, f"Kunde inte hämta publikationer: {error}", "error")
        return render(
            request, "publication_form.html", publication=None, titles=None, prefill=prefill
        )

    return render(
        request, "publication_form.html", publication=None, titles=titles, prefill=prefill
    )


@router.post("/publications")
def create_publication(
    request: Request,
    conf_json: str = Form(""),
    site: str = Form(""),
    textalk_auth: str = Form(""),
    auth: str = Form(""),
    cdn: str = Form(""),
    title_id: str = Form(""),
    name: str = Form(""),
    prefix: str = Form(""),
    folder: str = Form(""),
    watch_limit: int = Form(1),
):
    if conf_json.strip():
        try:
            conf = json.loads(strip_json_comments(conf_json))
        except ValueError as error:
            flash(request, f"Kunde inte tolka JSON: {error}", "error")
            return back("/publications/new")
        with get_session() as session:
            try:
                publication = import_conf(session, conf)
            except (KeyError, ValueError) as error:
                flash(request, f"Konfigurationen saknar uppgifter: {error}", "error")
                return back("/publications/new")
            slug = publication.slug
        flash(request, f"Lade till {slug}", "success")
        return back(f"/publications/{slug}")

    if not site or not title_id:
        flash(request, "Sajt och publikations-ID krävs", "error")
        return back("/publications/new")

    with get_session() as session:
        display_name = name.strip() or f"Publikation {title_id}"
        publication = repo.find_publication(session, site, int(title_id))
        if publication is None:
            publication = Publication(
                title_id=int(title_id),
                site=site.rstrip("/"),
                slug=repo.unique_slug(session, display_name),
            )
        publication.name = display_name
        publication.textalk_auth = textalk_auth
        publication.auth = auth
        publication.cdn = cdn
        publication.prefix = prefix
        publication.folder = folder
        publication.watch_limit = max(1, watch_limit)
        session.add(publication)
        session.flush()
        slug = publication.slug
    flash(request, f"Lade till {display_name}", "success")
    return back(f"/publications/{slug}")


@router.get("/publications/{slug}", response_class=HTMLResponse)
def publication_detail(request: Request, slug: str, page: int = 1):
    page = max(1, page)
    with get_session() as session:
        publication = repo.get_publication_by_slug(session, slug)
        if publication is None:
            flash(request, "Publikationen finns inte", "error")
            return back("/")

        # Bläddring bakåt fyller på ur katalogen. Redan hämtade utgåvor
        # läses ur databasen och kostar inga anrop.
        try:
            ensure_catalogue_depth(session, publication, page * PAGE_SIZE)
        except PrenlyError as error:
            flash(request, f"Kunde inte hämta äldre utgåvor: {error}", "error")

        total = repo.count_issues(session, publication.id)
        # Katalogen kan ta slut mitt i en begärd sida - backa till den sista
        # som faktiskt har utgåvor i stället för att visa en tom tabell.
        sista_sidan = max(1, -(-total // PAGE_SIZE))
        page = min(page, sista_sidan)
        issues = repo.list_issues(
            session, publication.id, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
        )
        latest = repo.latest_issue(session, publication.id)
        har_fler = total > page * PAGE_SIZE or not publication.catalogue_complete
        jobs = [j for j in repo.list_jobs(session, limit=200)
                if j.publication_id == publication.id][:20]
    return render(
        request,
        "publication.html",
        publication=publication,
        issues=issues,
        latest=latest,
        jobs=jobs,
        page=page,
        page_size=PAGE_SIZE,
        total_issues=total,
        har_fler=har_fler,
    )


@router.post("/publications/{publication_id}/edit")
def edit_publication(
    request: Request,
    publication_id: int,
    name: str = Form(""),
    site: str = Form(""),
    textalk_auth: str = Form(""),
    auth: str = Form(""),
    cdn: str = Form(""),
    prefix: str = Form(""),
    folder: str = Form(""),
    watch_limit: int = Form(1),
    enabled: str = Form(""),
):
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            flash(request, "Publikationen finns inte", "error")
            return back("/")
        publication.name = name.strip() or publication.name
        publication.site = site.rstrip("/") or publication.site
        publication.textalk_auth = textalk_auth
        publication.auth = auth
        publication.cdn = cdn
        publication.prefix = prefix
        publication.folder = folder
        publication.watch_limit = max(1, watch_limit)
        publication.enabled = enabled == "on"
        session.add(publication)
        slug = publication.slug
    flash(request, "Sparat", "success")
    return back(f"/publications/{slug}")


@router.post("/publications/{publication_id}/verify")
def verify_publication(request: Request, publication_id: int):
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            return back("/")
        status = verify_credentials(session, publication)
        slug = publication.slug
        message = publication.auth_message
    if status is AuthStatus.OK:
        flash(request, "Uppgifterna fungerar", "success")
    else:
        flash(request, f"{STATUS_LABELS.get(status.value, status.value)}: {message}", "error")
    return back(f"/publications/{slug}")


@router.post("/publications/{publication_id}/poll")
def poll_now(request: Request, publication_id: int):
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            return back("/")
        slug = publication.slug
        try:
            result = poll_publication(session, publication)
        except PrenlyError as error:
            flash(request, f"Katalogkontrollen misslyckades: {error}", "error")
            return back(f"/publications/{slug}")
    flash(
        request,
        f"{result['found']} utgåvor i katalogen, {result['new']} nya, {result['queued']} köade",
        "success",
    )
    return back(f"/publications/{slug}")


@router.post("/publications/{publication_id}/delete")
def delete_publication(request: Request, publication_id: int):
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is not None:
            session.delete(publication)
    flash(request, "Publikationen togs bort", "success")
    return back("/")


# --------------------------------------------------------------------------
# Utgåvor och jobb
# --------------------------------------------------------------------------


@router.post("/issues/{issue_id}/download")
def download_issue_now(request: Request, issue_id: int, force: str = Form("")):
    with get_session() as session:
        issue = repo.get_issue(session, issue_id)
        if issue is None:
            return back("/")
        publication = repo.get_publication(session, issue.publication_id)
        slug = publication.slug if publication else ""
        job = repo.enqueue_download(session, issue, force=force == "on")
    if job is None:
        flash(request, "Utgåvan har redan ett aktivt jobb", "info")
    else:
        flash(request, f"Köade nedladdning av {issue.name}", "success")
    return back(f"/publications/{slug}" if slug else "/jobs")


# --------------------------------------------------------------------------
# Inställningar
# --------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with get_session() as session:
        poll_cron = repo.get_setting(session, POLL_CRON_KEY, DEFAULT_POLL_CRON)
        history_days = repo.get_setting(
            session, JOB_HISTORY_DAYS_KEY, str(DEFAULT_JOB_HISTORY_DAYS)
        )
    return render(
        request,
        "settings.html",
        poll_cron=poll_cron,
        history_days=history_days,
        output_dir=output_root(),
        timezone=timezone_name(),
        next_poll=next_poll_time(),
    )


@router.post("/settings")
def save_settings(
    request: Request,
    poll_cron: str = Form(DEFAULT_POLL_CRON),
    history_days: int = Form(DEFAULT_JOB_HISTORY_DAYS),
):
    try:
        reschedule_poll(poll_cron.strip())
    except ValueError as error:
        flash(request, f"Ogiltigt cron-uttryck: {error}", "error")
        return back("/settings")
    with get_session() as session:
        repo.set_setting(session, POLL_CRON_KEY, poll_cron.strip())
        repo.set_setting(session, JOB_HISTORY_DAYS_KEY, str(max(1, history_days)))
    flash(request, "Inställningarna sparade", "success")
    return back("/settings")


@router.post("/settings/import")
def import_configs(request: Request, directory: str = Form("")):
    path = Path(directory.strip()).expanduser()
    if not path.is_dir():
        flash(request, f"Katalogen {path} finns inte", "error")
        return back("/settings")
    with get_session() as session:
        imported = import_directory(session, path)
    flash(request, f"Importerade {len(imported)} konfigurationsfiler", "success")
    return back("/settings")
