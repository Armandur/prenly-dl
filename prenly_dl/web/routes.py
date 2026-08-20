"""HTTP-rutter för webbgränssnittet."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..api import Credentials
from ..config import output_root, timezone_name
from ..db import repository as repo
from ..db.models import AuthStatus, IssueStatus, JobStatus, Publication
from ..db.session import get_session
from ..errors import PrenlyError
from ..importer import import_conf, import_directory, load_conf, strip_json_comments
from ..scheduler import (
    DEFAULT_JOB_HISTORY_DAYS,
    DEFAULT_POLL_CRON,
    JOB_HISTORY_DAYS_KEY,
    POLL_CRON_KEY,
    next_poll_time,
    reschedule_poll,
)
from ..services import discover_titles, poll_publication, verify_credentials
from .auth import auth_enabled, check_password, is_authenticated
from .filters import register_filters

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
register_filters(templates.env)

STATUS_LABELS = {
    AuthStatus.OK.value: "OK",
    AuthStatus.UNKNOWN.value: "Okänd",
    AuthStatus.AUTH_ERROR.value: "Utloggad",
    AuthStatus.SUBSCRIPTION_ERROR.value: "Ingen prenumeration",
    AuthStatus.ERROR.value: "Fel",
}


def flash(request: Request, message: str, level: str = "info") -> None:
    messages = request.session.setdefault("flashes", [])
    messages.append({"text": message, "level": level})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("flashes", [])


def render(request: Request, template: str, **context) -> HTMLResponse:
    context.setdefault("flashes", pop_flashes(request))
    context.setdefault("auth_enabled", auth_enabled())
    context.setdefault("status_labels", STATUS_LABELS)
    return templates.TemplateResponse(request, template, context)


def back(url: str = "/") -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


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
        jobs = repo.list_jobs(session, limit=10)
        issues = repo.list_issues(session, limit=10)
        rows = []
        for publication in publications:
            pub_issues = repo.list_issues(session, publication.id, limit=1)
            rows.append({
                "publication": publication,
                "latest": pub_issues[0] if pub_issues else None,
            })
    return render(
        request,
        "index.html",
        rows=rows,
        jobs=jobs,
        issues=issues,
        next_poll=next_poll_time(),
        output_dir=output_root(),
    )


# --------------------------------------------------------------------------
# Publikationer
# --------------------------------------------------------------------------


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
def publication_detail(request: Request, slug: str):
    with get_session() as session:
        publication = repo.get_publication_by_slug(session, slug)
        if publication is None:
            flash(request, "Publikationen finns inte", "error")
            return back("/")
        issues = repo.list_issues(session, publication.id, limit=100)
        jobs = [j for j in repo.list_jobs(session, limit=200)
                if j.publication_id == publication.id][:20]
    return render(
        request,
        "publication.html",
        publication=publication,
        issues=issues,
        jobs=jobs,
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


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    with get_session() as session:
        jobs = repo.list_jobs(session, limit=100)
        publications = {p.id: p for p in repo.list_publications(session)}
        issues = {i.id: i for i in repo.list_issues(session, limit=500)}
    return render(
        request, "jobs.html", jobs=jobs, publications=publications, issues=issues
    )


@router.post("/jobs/poll-all")
def poll_all(request: Request):
    with get_session() as session:
        repo.enqueue_poll(session)
    flash(request, "Katalogkontroll köad för alla aktiva publikationer", "success")
    return back("/jobs")


@router.get("/api/jobs")
def jobs_json():
    with get_session() as session:
        jobs = repo.list_jobs(session, limit=25)
        payload = [
            {
                "id": job.id,
                "type": job.type,
                "status": job.status,
                "progress_done": job.progress_done,
                "progress_total": job.progress_total,
                "message": job.message,
                "error": job.error,
            }
            for job in jobs
        ]
    return JSONResponse(payload)


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
