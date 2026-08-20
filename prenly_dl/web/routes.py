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
from ..downloader import safe_filename
from ..db import repository as repo
from ..db.models import AuthStatus
from ..db.session import get_session
from ..errors import PrenlyError
from ..importer import (
    extract_configs,
    import_directory,
)
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
    publication_output_dir,
    verify_site,
)
from .auth import check_password, is_authenticated
from .library import folder_usage
from .rendering import back, flash, render

logger = logging.getLogger(__name__)

router = APIRouter()

# Antal utgåvor per sida i utgåvetabellen.
PAGE_SIZE = 20
# Hur många sidnummer som renderas runt den aktuella. En publikation som
# samlat på sig tusen utgåvor ska inte ge femtio länkar.
PAGE_WINDOW = 2


def sidnummer(aktuell: int, sista: int) -> list[int | None]:
    """Sidnumren att visa: första, sista och ett fönster runt den aktuella.

    None markerar ett hopp i följden och renderas som ellips.
    """
    if sista <= 7:
        return list(range(1, sista + 1))
    visa = {1, sista}
    visa.update(range(max(1, aktuell - PAGE_WINDOW), min(sista, aktuell + PAGE_WINDOW) + 1))
    ut: list[int | None] = []
    forra = 0
    for nummer in sorted(visa):
        if forra and nummer > forra + 1:
            ut.append(None)
        ut.append(nummer)
        forra = nummer
    return ut


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
        # Översikten visar det man valt att bevaka; hela katalogen finns
        # under /katalog.
        publications = repo.list_publications(session, enabled_only=True)
        stats = repo.dashboard_stats(session)
        jobs = repo.list_jobs(session, limit=10)
        issues = repo.list_issues(session, limit=10)
        rows = []
        for publication in publications:
            antal, storlek = folder_usage(publication_output_dir(publication))
            rows.append({
                "publication": publication,
                "latest": repo.latest_issue(session, publication.id),
                "filer": antal,
                "storlek": storlek,
            })
        job_publications = {p.id: p for p in publications}
    return render(
        request,
        "index.html",
        stats=stats,
        rows=rows,
        job_publications=job_publications,
        jobs=jobs,
        issues=issues,
        next_poll=next_poll_time(),
        output_dir=output_root(),
    )


# --------------------------------------------------------------------------
# Publikationer
# --------------------------------------------------------------------------


@router.get("/katalog", response_class=HTMLResponse)
def catalogue(request: Request):
    """Alla kända titlar per sajt, med växel för bevakning.

    Läses ur databasen: katalogen visar vad kontot hade tillgång till även
    när uppgifterna slutat gälla.
    """
    with get_session() as session:
        sajter = []
        for site in repo.list_sites(session):
            publikationer = repo.list_publications(session, site_id=site.id)
            sajter.append({
                "site": site,
                # Antalet utgåvor räknas i databasen: att ladda relationen
                # för att mäta dess längd hämtar tusentals rader i onödan.
                "rader": [
                    {
                        "pub": p,
                        "utgavor": repo.count_issues(session, p.id),
                        "senaste": repo.latest_issue(session, p.id),
                    }
                    for p in publikationer
                ],
                "antal": len(publikationer),
                "aktiva": sum(1 for p in publikationer if p.enabled),
                "lasbara": sum(1 for p in publikationer if p.readable),
            })
    return render(request, "catalogue.html", sajter=sajter)


@router.post("/katalog/{publication_id}/toggle")
def toggle_publication(request: Request, publication_id: int, aktiv: str = Form("")):
    """Slå på eller av bevakning av en titel."""
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            return back("/katalog")
        publication.enabled = aktiv == "on"
        session.add(publication)
        namn, pa = publication.name, publication.enabled
    flash(request, f"{namn} {'bevakas nu' if pa else 'bevakas inte längre'}", "success")
    return back("/katalog")


@router.post("/sites/{site_id}/verify")
def verify_site_route(request: Request, site_id: int):
    """Testa en sajts uppgifter och uppdatera dess titellista."""
    with get_session() as session:
        site = repo.get_site(session, site_id)
        if site is None:
            return back("/katalog")
        status = verify_site(session, site)
        url, message = site.url, site.auth_message
    if status is AuthStatus.OK:
        flash(request, f"Uppgifterna för {url} fungerar", "success")
    else:
        flash(request, f"{url}: {message}", "error")
    return back("/katalog")


@router.post("/sites/{site_id}/sync")
def sync_site(request: Request, site_id: int):
    """Köa en synk av sajtens titlar och deras omslag."""
    with get_session() as session:
        if repo.get_site(session, site_id) is None:
            return back("/katalog")
        job = repo.enqueue_site_sync(session, site_id)
    if job is None:
        flash(request, "Sajten synkas redan", "info")
    else:
        flash(request, "Hämtar titlar och omslag, följ förloppet under Jobb", "success")
    return back("/katalog")


@router.post("/sites/{site_id}/delete")
def delete_site(request: Request, site_id: int):
    with get_session() as session:
        site = repo.get_site(session, site_id)
        if site is not None:
            session.delete(site)
    flash(request, "Sajten och dess publikationer togs bort", "success")
    return back("/katalog")


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


def _konsolsnutt() -> str:
    return (Path(__file__).parent / "static" / "konsolsnutt.js").read_text(
        encoding="utf-8"
    )


@router.get("/uppgifter", response_class=HTMLResponse)
def credentials_help(request: Request):
    """Konsolsnutten och ytan att klistra in resultatet på."""
    return render(
        request, "credentials.html", snutt=_konsolsnutt(), fynd=None,
        fyndfel=None, fyndsajter=None,
    )


def _rendera_fynd(
    request: Request,
    fynd: list | None,
    fyndfel: str | None,
    fyndvarning: str | None = None,
    fyndsajter: list | None = None,
):
    """Bara resultatlistan för htmx, hela sidan utan JavaScript.

    Utan JavaScript renderas den sida användaren kom ifrån - ytan finns
    både under Hämta inloggningsuppgifter och Lägg till publikation.
    """
    if request.headers.get("HX-Request"):
        return render(
            request, "credentials_fynd.html", fynd=fynd, fyndfel=fyndfel,
            fyndvarning=fyndvarning, fyndsajter=fyndsajter, flashes=[],
        )
    return render(
        request, "credentials.html", snutt=_konsolsnutt(), fynd=fynd,
        fyndfel=fyndfel, fyndvarning=fyndvarning, fyndsajter=fyndsajter,
    )


@router.post("/uppgifter/granska", response_class=HTMLResponse)
def review_pasted(request: Request, klistrat: str = Form("")):
    """Visa vad som hittades i den klistrade texten innan något sparas."""
    if not klistrat.strip():
        return _rendera_fynd(
            request, None, "Klistra in det du fick från konsolsnutten först"
        )

    konfigurationer = extract_configs(klistrat)
    if not konfigurationer:
        return _rendera_fynd(
            request,
            None,
            "Hittade ingen konfiguration i texten. Kopiera hela utskriften från "
            "konsolen, eller ett helt JSON-block.",
        )

    fynd, varning, sajtrader = _fynd_fran_konfigurationer(konfigurationer)
    return _rendera_fynd(request, fynd, None, varning, sajtrader)


def _konfig_for_titel(credentials: dict, titel: dict) -> dict:
    """Bygg en konfiguration för en titel ur titellistan."""
    namn = safe_filename(str(titel.get("name") or f"Publikation {titel['id']}"))
    return {
        "credentials": credentials,
        "publication": {"title": titel["id"], "limit": 1},
        "prefix": f"{namn} - ",
        "folder": namn,
    }


def _fynd_fran_konfigurationer(
    konfigurationer: list[dict],
) -> tuple[list, str | None, list]:
    """Bygg listan att kryssa i.

    Uppgifterna i texten räcker för att fråga Prenly vilka publikationer
    kontot har tillgång till, så listan byggs från titellistan i stället
    för från de block som råkade följa med i det som klistrades in. Ett
    enda block räcker alltså för att lägga till alla publikationer på en
    sajt. Svarar inte API:t används blocken som de är.
    """
    # Samma sajt kan förekomma i flera block med samma uppgifter.
    per_sajt: dict[str, dict] = {}
    for conf in konfigurationer:
        site = str(conf["credentials"].get("site", "")).rstrip("/")
        per_sajt.setdefault(site, conf["credentials"])

    valda_titlar = {
        (str(c["credentials"].get("site", "")).rstrip("/"), int(c["publication"]["title"]))
        for c in konfigurationer
        if (c.get("publication") or {}).get("title") is not None
    }

    fynd: list[dict] = []
    misslyckade: list[str] = []
    with get_session() as session:
        for site, credentials in per_sajt.items():
            creds = Credentials(
                site=site,
                textalk_auth=credentials.get("textalk-auth", ""),
                auth=credentials.get("auth", ""),
                cdn=credentials.get("cdn", "") or "",
            )
            try:
                titlar = discover_titles(creds)
            except PrenlyError as error:
                logger.warning("Kunde inte lista titlar för %s: %s", site, error)
                misslyckade.append(f"{site}: {error}")
                titlar = None

            if titlar is None:
                # Falla tillbaka på titlar som fanns i det inklistrade.
                # Konsolsnutten skickar bara uppgifter, så listan kan bli
                # tom - då finns inget att välja bland.
                rader = [
                    (int(c["publication"]["title"]),
                     c.get("folder") or c.get("prefix", "").strip(" -"),
                     True, c)
                    for c in konfigurationer
                    if str(c["credentials"].get("site", "")).rstrip("/") == site
                    and (c.get("publication") or {}).get("title") is not None
                ]
            else:
                rader = [
                    (t["id"], t.get("name") or f"Publikation {t['id']}",
                     bool(t.get("user_readable")), _konfig_for_titel(credentials, t))
                    for t in titlar
                ]

            for title_id, namn, lasbar, conf in rader:
                befintlig = repo.find_publication(session, site, title_id)
                fynd.append({
                    "conf": json.dumps(conf, ensure_ascii=False),
                    "namn": namn or f"Publikation {title_id}",
                    "title_id": title_id,
                    "site": site,
                    "lasbar": lasbar,
                    # Kryssa i det som går att läsa och som användaren redan
                    # pekat ut eller redan har.
                    "vald": lasbar and (
                        (site, title_id) in valda_titlar or befintlig is not None
                    ),
                    "befintlig": befintlig.name if befintlig else None,
                    "slug": befintlig.slug if befintlig else None,
                })

    varning = None
    if misslyckade:
        varning = (
            "Kunde inte hämta publikationslistan från Prenly: "
            + "; ".join(misslyckade)
        )
    # Sajternas uppgifter går att spara även utan titellista - då kan man
    # åtminstone lägga in ett förnyat token och hämta listan senare.
    sajtrader = [
        {"url": url, "conf": json.dumps({"credentials": credentials}, ensure_ascii=False)}
        for url, credentials in per_sajt.items()
    ]
    return fynd, varning, sajtrader


@router.post("/uppgifter/spara")
async def save_pasted(request: Request):
    """Spara sajtens uppgifter och aktivera de titlar som kryssats i.

    Uppgifterna skrivs en gång per sajt. Alla titlar sajten exponerade när
    listan hämtades sparas som kända, så katalogen är komplett - de som
    kryssats i aktiveras för bevakning.
    """
    form = await request.form()
    valda = form.getlist("valda")
    alla = form.getlist("alla") or valda
    if not valda:
        flash(request, "Inga publikationer valda", "error")
        return back("/uppgifter")

    def _tolka(rader):
        for rad in rader:
            try:
                yield json.loads(rad)
            except ValueError:
                continue

    aktiverade_nycklar = {
        (str(c["credentials"].get("site", "")).rstrip("/"), int(c["publication"]["title"]))
        for c in _tolka(valda)
        if c.get("credentials") and c.get("publication")
    }
    # Läsrätten hör till titeln och följer inte med konfigurationen, så den
    # skickas som en egen lista över det kontot inte kommer åt.
    olasbara = set()
    for rad in form.getlist("olasbar"):
        url, _, title = rad.rpartition("|")
        if title.isdigit():
            olasbara.add((url, int(title)))

    nya, aktiverade, sajter = 0, 0, set()
    with get_session() as session:
        for conf in _tolka(alla):
            credentials = conf.get("credentials") or {}
            url = str(credentials.get("site", "")).rstrip("/")
            if not url:
                continue
            # Sajtens uppgifter sparas oavsett - ett block utan titel bär
            # bara dem, vilket är vad konsolsnutten skickar.
            site = repo.upsert_site(session, url, credentials)
            sajter.add(url)
            try:
                title_id = int(conf["publication"]["title"])
            except (KeyError, TypeError, ValueError):
                continue

            namn = (
                conf.get("folder") or conf.get("prefix", "").strip(" -")
                or f"Publikation {title_id}"
            )
            publication, created = repo.upsert_publication(
                session, site, title_id, namn,
                readable=(url, title_id) not in olasbara,
            )
            if created:
                nya += 1
            if (url, title_id) in aktiverade_nycklar and not publication.enabled:
                publication.enabled = True
                aktiverade += 1
            session.add(publication)

    delar = [f"{len(sajter)} {'sajt' if len(sajter) == 1 else 'sajter'} sparad"]
    if nya:
        delar.append(f"{nya} nya titlar i katalogen")
    if aktiverade:
        delar.append(f"{aktiverade} aktiverade")
    flash(request, ", ".join(delar).capitalize(), "success")
    return back("/katalog")


def _utgavekontext(session, publication, page: int) -> dict:
    """Allt utgåvetabellen behöver för en given sida."""
    # Bläddring bakåt fyller på ur katalogen. Redan hämtade utgåvor
    # läses ur databasen och kostar inga anrop.
    fel = None
    try:
        ensure_catalogue_depth(session, publication, page * PAGE_SIZE)
    except PrenlyError as error:
        fel = f"Kunde inte hämta äldre utgåvor: {error}"

    total = repo.count_issues(session, publication.id)
    # Katalogen kan ta slut mitt i en begärd sida - backa till den sista
    # som faktiskt har utgåvor i stället för att visa en tom tabell.
    sista_sidan = max(1, -(-total // PAGE_SIZE))
    page = min(page, sista_sidan)
    return {
        "publication": publication,
        "issues": repo.list_issues(
            session, publication.id, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
        ),
        "page": page,
        "page_size": PAGE_SIZE,
        "total_issues": total,
        "sista_sidan": sista_sidan,
        "har_fler": total > page * PAGE_SIZE or not publication.catalogue_complete,
        # Sidnummer och årshopp kräver att katalogen är helt hämtad: annars
        # räknas de ut från halva underlaget och pekar fel.
        "sidor": sidnummer(page, sista_sidan) if publication.catalogue_complete else [],
        "ar_lista": (
            repo.issue_years(session, publication.id)
            if publication.catalogue_complete
            else []
        ),
        "_fel": fel,
    }


@router.get("/publications/{slug}/issues", response_class=HTMLResponse)
def publication_issues(request: Request, slug: str, page: int = 1):
    """Bara utgåvesektionen, för bläddring utan omladdning."""
    with get_session() as session:
        publication = repo.get_publication_by_slug(session, slug)
        if publication is None:
            return back("/")
        kontext = _utgavekontext(session, publication, max(1, page))
    if kontext.pop("_fel"):
        flash(request, "Kunde inte hämta äldre utgåvor", "error")
    return render(request, "issues_section.html", flashes=[], **kontext)


@router.get("/publications/{slug}", response_class=HTMLResponse)
def publication_detail(request: Request, slug: str, page: int = 1):
    page = max(1, page)
    with get_session() as session:
        publication = repo.get_publication_by_slug(session, slug)
        if publication is None:
            flash(request, "Publikationen finns inte", "error")
            return back("/")

        kontext = _utgavekontext(session, publication, page)
        latest = repo.latest_issue(session, publication.id)
        jobs = [j for j in repo.list_jobs(session, limit=200)
                if j.publication_id == publication.id][:20]
    fel = kontext.pop("_fel")
    if fel:
        flash(request, fel, "error")
    return render(
        request, "publication.html", latest=latest, jobs=jobs, **kontext
    )


@router.post("/publications/{publication_id}/edit")
def edit_publication(
    request: Request,
    publication_id: int,
    name: str = Form(""),
    prefix: str = Form(""),
    folder: str = Form(""),
    watch_limit: int = Form(1),
    enabled: str = Form(""),
):
    """Publikationens egna inställningar. Uppgifterna bor på sajten."""
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            flash(request, "Publikationen finns inte", "error")
            return back("/")
        publication.name = name.strip() or publication.name
        publication.prefix = prefix
        publication.folder = folder
        publication.watch_limit = max(1, watch_limit)
        publication.enabled = enabled == "on"
        session.add(publication)
        slug = publication.slug
    flash(request, "Sparat", "success")
    return back(f"/publications/{slug}")


@router.post("/publications/{publication_id}/catalogue")
def fetch_catalogue(request: Request, publication_id: int):
    """Köa en full kataloghämtning."""
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            return back("/")
        slug = publication.slug
        job = repo.enqueue_catalogue(session, publication_id)
    if job is None:
        flash(request, "Katalogen hämtas redan", "info")
    else:
        flash(request, "Hämtar hela katalogen, följ förloppet under Jobb", "success")
    return back(f"/publications/{slug}")


@router.get("/publications/{slug}/year")
def jump_to_year(request: Request, slug: str, ar: str = ""):
    """Hoppa till sidan där ett år börjar.

    Utgåvor har inget sidnummer i användarens huvud - de har ett årtal.
    Årshoppet räknar om till sidnummer och behåller ?page som enda adress.
    """
    with get_session() as session:
        publication = repo.get_publication_by_slug(session, slug)
        if publication is None:
            flash(request, "Publikationen finns inte", "error")
            return back("/")
        if not ar.isdigit():
            return back(f"/publications/{slug}")
        page = repo.issues_newer_than_year(session, publication.id, ar) // PAGE_SIZE + 1
        if not request.headers.get("HX-Request"):
            return back(f"/publications/{slug}?page={page}")
        kontext = _utgavekontext(session, publication, page)
        kontext.pop("_fel")
    svar = render(request, "issues_section.html", flashes=[], **kontext)
    # htmx pushar annars årshoppets egen adress; sidan är den delbara.
    svar.headers["HX-Push-Url"] = f"/publications/{slug}?page={kontext['page']}"
    return svar


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
