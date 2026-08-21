"""HTTP-rutter för webbgränssnittet."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from ..api import Credentials, build_session
from ..config import output_root, timezone_name
from ..covers import cached_cover_path, fetch_and_cache_cover
from ..downloader import safe_filename
from ..db import repository as repo
from ..db.models import AuthStatus, IssueStatus
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
    build_issue_preview,
    discover_titles,
    ensure_catalogue_depth,
    poll_publication,
    publication_output_dir,
    update_publication_storage,
    verify_site,
)
from .auth import check_password, is_authenticated
from .. import calibre
from .library import folder_usage, resolve_file
from .path_browser import browse_roots, directory_payload, resolve_allowed_directory
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
        # under /catalogue.
        publications = repo.list_publications(session, enabled_only=True)
        stats = repo.dashboard_stats(session)
        jobs = repo.list_jobs(session, limit=10)
        issues = repo.list_issues(session, limit=10)
        rows = []
        for publication in publications:
            antal, storlek = folder_usage(publication_output_dir(publication))
            rows.append({
                "publication": publication,
                "site": repo.site_for_title(session, publication.title_id),
                "latest": repo.latest_issue(session, publication.id),
                "filer": antal,
                "storlek": storlek,
            })
        # Alla publikationer, inte bara de bevakade: ett jobb kan gälla en
        # publikation som inte längre bevakas, och raden ska ändå namnge den.
        job_publications = {p.id: p for p in repo.list_publications(session)}
        job_sites = {site.id: site for site in repo.list_sites(session)}
    return render(
        request,
        "index.html",
        stats=stats,
        rows=rows,
        job_publications=job_publications,
        job_sites=job_sites,
        jobs=jobs,
        issues=issues,
        next_poll=next_poll_time(),
        output_dir=output_root(),
    )


# --------------------------------------------------------------------------
# Publikationer
# --------------------------------------------------------------------------


def _sajtkontext(session, site) -> dict:
    """Allt en sajtrad på uppgiftssidan behöver."""
    antal, lasbara = repo.site_title_counts(session, site.id)
    return {"site": site, "antal": antal, "lasbara": lasbara}


def _sajtrad(request: Request, site_id: int, meddelande: str | None = None):
    """Rendera om en sajtrad för htmx, eller gå tillbaka till uppgifterna."""
    if not request.headers.get("HX-Request"):
        return back("/credentials")
    with get_session() as session:
        site = repo.get_site(session, site_id)
        if site is None:
            return HTMLResponse("")
        kontext = _sajtkontext(session, site)
    return render(
        request, "site_card.html", post=kontext, kortmeddelande=meddelande, flashes=[]
    )


def _katalogkontext(session) -> list[dict]:
    rader = []
    for title in repo.list_titles(session):
        publication = title.publication
        rader.append({
            "title": title,
            "pub": publication,
            "utgavor": repo.count_title_issues(session, title.title_id),
            "senaste": repo.latest_title_issue(session, title.title_id),
            "lasbar": repo.title_is_readable(title),
            "atkomster": title.accesses,
        })
    return rader


@router.get("/catalogue", response_class=HTMLResponse)
def catalogue(request: Request):
    """Alla kända titlar, deduplicerade på Prenlys globala titel-ID."""
    with get_session() as session:
        rader = _katalogkontext(session)
        har_sajter = bool(repo.list_sites(session))
    return render(request, "catalogue.html", rader=rader, har_sajter=har_sajter)


@router.post("/catalogue/{publication_id}/toggle")
def toggle_publication(request: Request, publication_id: int, aktiv: str = Form("")):
    """Slå på eller av bevakning av en titel."""
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            return back("/catalogue")
        if aktiv == "on" and not publication.readable:
            besked = f"Ingen av dina inloggningsuppgifter ger läsrätt till {publication.name}"
        else:
            publication.enabled = aktiv == "on"
            session.add(publication)
            besked = (
                f"{publication.name} bevakas nu"
                if publication.enabled
                else f"{publication.name} bevakas inte längre"
            )
        if request.headers.get("HX-Request"):
            rader = _katalogkontext(session)
            return render(
                request, "catalogue_table.html", rader=rader,
                kortmeddelande=besked, flashes=[],
            )
    flash(request, besked, "success")
    return back("/catalogue")


@router.post("/sites/{site_id}/verify")
def verify_site_route(request: Request, site_id: int):
    """Testa en sajts uppgifter och uppdatera dess titellista."""
    with get_session() as session:
        site = repo.get_site(session, site_id)
        if site is None:
            return back("/credentials")
        status = verify_site(session, site)
        url, message = site.url, site.auth_message
    besked = "Uppgifterna fungerar" if status is AuthStatus.OK else f"{message}"
    if request.headers.get("HX-Request"):
        return _sajtrad(request, site_id, besked)
    flash(request, f"{url}: {besked}", "success" if status is AuthStatus.OK else "error")
    return back("/credentials")


@router.post("/sites/{site_id}/sync")
def sync_site(request: Request, site_id: int):
    """Köa en synk av sajtens titlar och deras omslag."""
    with get_session() as session:
        if repo.get_site(session, site_id) is None:
            return back("/credentials")
        job = repo.enqueue_site_sync(session, site_id)
    besked = (
        "Sajten synkas redan"
        if job is None
        else "Hämtar titlar och omslag, följ förloppet under Jobb"
    )
    if request.headers.get("HX-Request"):
        return _sajtrad(request, site_id, besked)
    flash(request, besked, "info" if job is None else "success")
    return back("/credentials")


@router.post("/sites/{site_id}/delete")
def delete_site(request: Request, site_id: int):
    with get_session() as session:
        site = repo.get_site(session, site_id)
        if site is not None:
            repo.delete_site(session, site)
    if request.headers.get("HX-Request"):
        return HTMLResponse("")
    flash(request, "Sajten togs bort", "success")
    return back("/credentials")


@router.get("/covers/{title_id}/{checksum}.jpg")
def cover_image(title_id: int, checksum: str, stor: bool = False):
    """Servera ett cachat omslag och välj en fungerande sajt vid cachemiss."""
    headers = {"Cache-Control": "public, max-age=604800, immutable"}
    variant = {"stor": True} if stor else {}
    path = cached_cover_path(title_id, checksum, **variant)
    if path is None:
        with get_session() as session:
            title = repo.get_title(session, title_id)
            site = repo.site_for_title(session, title_id)
            if title is None:
                return JSONResponse({"error": "okänd titel"}, status_code=404)
            if site is None:
                return JSONResponse(
                    {"error": f"Ingen av dina inloggningsuppgifter ger läsrätt till {title.name}"},
                    status_code=403,
                )
            creds = repo.credentials_for_site(site)
        path = fetch_and_cache_cover(
            build_session(), creds, title_id, checksum, **variant
        )
    if path is None:
        return JSONResponse({"error": "omslaget kunde inte hämtas"}, status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers=headers)


def _konsolsnutt() -> str:
    return (Path(__file__).parent / "static" / "konsolsnutt.js").read_text(
        encoding="utf-8"
    )


@router.get("/credentials", response_class=HTMLResponse)
def credentials_help(request: Request):
    """Sparade sajter samt konsolsnutten och inklistringsytan."""
    with get_session() as session:
        sajter = [_sajtkontext(session, site) for site in repo.list_sites(session)]
    return render(
        request, "credentials.html", snutt=_konsolsnutt(), fynd=None,
        fyndfel=None, fyndsajter=None, sajter=sajter,
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
    with get_session() as session:
        sajter = [_sajtkontext(session, site) for site in repo.list_sites(session)]
    return render(
        request, "credentials.html", snutt=_konsolsnutt(), fynd=fynd,
        fyndfel=fyndfel, fyndvarning=fyndvarning, fyndsajter=fyndsajter,
        sajter=sajter,
    )


@router.post("/credentials/review", response_class=HTMLResponse)
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


@router.post("/credentials/save")
async def save_pasted(request: Request):
    """Spara sajtens uppgifter och aktivera de titlar som kryssats i.

    Uppgifterna skrivs en gång per sajt. Alla titlar sajten exponerade när
    listan hämtades sparas som kända, så katalogen är komplett - de som
    kryssats i aktiveras för bevakning.
    """
    form = await request.form()
    valda = form.getlist("valda")
    alla = form.getlist("alla") or valda
    # Att bara spara uppgifterna är ett giltigt val: man kan vilja förnya
    # ett token utan att ändra vad som bevakas.
    if not alla:
        flash(request, "Inget att spara", "error")
        return back("/credentials")

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

    # Hämta titlarnas senaste utgåva och omslag direkt, så katalogen är
    # komplett utan att man behöver be om det.
    with get_session() as session:
        for url in sajter:
            site = repo.find_site(session, url)
            if site is not None:
                repo.enqueue_site_sync(session, site.id)

    delar = [f"{len(sajter)} {'sajt' if len(sajter) == 1 else 'sajter'} sparad"]
    if nya:
        delar.append(f"{nya} nya titlar i katalogen")
    if aktiverade:
        delar.append(f"{aktiverade} aktiverade")
    if not aktiverade and not nya:
        delar.append("bevakningen oförändrad")
    delar.append("hämtar omslag i bakgrunden")
    flash(request, ", ".join(delar).capitalize(), "success")
    return back("/catalogue")


def _bibliotekssokvag(file_path: str | None) -> str | None:
    """Sökvägen som /library/file vill ha, eller None om filen är borta."""
    if not file_path:
        return None
    path = Path(file_path)
    if not path.is_file():
        return None
    try:
        return str(path.resolve().relative_to(output_root().resolve()))
    except ValueError:
        # Filen ligger utanför utdatakatalogen och kan inte serveras.
        return None


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
    katalogantal = repo.count_catalogue_issues(session, publication.id)
    # Katalogen kan ta slut mitt i en begärd sida - backa till den sista
    # som faktiskt har utgåvor i stället för att visa en tom tabell.
    sista_sidan = max(1, -(-total // PAGE_SIZE))
    page = min(page, sista_sidan)
    issues = repo.list_issues(
        session, publication.id, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
    )
    for issue in issues:
        # Biblioteksrutten vill ha en sökväg relativ utdatakatalogen, och
        # länken ska bara finnas när filen faktiskt är kvar på disk.
        issue.rel_path = _bibliotekssokvag(issue.file_path)
    return {
        "publication": publication,
        "issues": issues,
        # Pollningen ska bara vara igång medan något faktiskt händer.
        "pagar": any(
            i.status in (IssueStatus.QUEUED.value, IssueStatus.DOWNLOADING.value)
            for i in issues
        ),
        "page": page,
        "page_size": PAGE_SIZE,
        "total_issues": total,
        # Diskfynden är utgåvor användaren äger, men inte katalogutgåvor.
        "disk_antal": total - katalogantal,
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
        site = repo.site_for_title(session, publication.title_id)
        latest = repo.latest_issue(session, publication.id)
        jobs = [
            j for j in repo.list_jobs(session, limit=200) if j.publication_id == publication.id
        ][:20]
        main_candidates = [
            candidate
            for candidate in repo.list_publications(session, enabled_only=True)
            if candidate.id != publication.id
            and candidate.main_publication_id is None
            and not publication.attachments
        ]
        if publication.main_publication is not None and all(
            candidate.id != publication.main_publication.id for candidate in main_candidates
        ):
            main_candidates.append(publication.main_publication)
    fel = kontext.pop("_fel")
    if fel:
        flash(request, fel, "error")
    return render(
        request,
        "publication.html",
        latest=latest,
        jobs=jobs,
        site=site,
        main_candidates=main_candidates,
        standard_calibre_title=calibre.STANDARDTITEL,
        **kontext,
    )


@router.post("/publications/{publication_id}/edit")
def edit_publication(
    request: Request,
    publication_id: int,
    name: str = Form(""),
    prefix: str = Form(""),
    calibre_title: str = Form(""),
    folder: str = Form(""),
    watch_limit: int = Form(1),
    enabled: str = Form(""),
    main_publication_id: str = Form(""),
):
    """Publikationens egna inställningar. Uppgifterna bor på sajten."""
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            flash(request, "Publikationen finns inte", "error")
            return back("/")
        main_publication = None
        if main_publication_id:
            try:
                main_publication = repo.get_publication(session, int(main_publication_id))
            except ValueError:
                main_publication = None
            if main_publication is None:
                flash(request, "Huvudpublikationen finns inte", "error")
                return back(f"/publications/{publication.slug}")
        try:
            moved, renamed = update_publication_storage(
                session,
                publication,
                main_publication,
                folder=folder,
                name=name.strip() or publication.name,
                prefix=prefix,
            )
        except ValueError as error:
            flash(request, str(error), "error")
            return back(f"/publications/{publication.slug}")
        publication.watch_limit = max(1, watch_limit)
        publication.enabled = enabled == "on"
        publication.calibre_title = calibre_title.strip()
        session.add(publication)
        slug = publication.slug
    besked = "Sparat"
    if moved:
        besked += f" och flyttade {moved} hämtade filer"
    if renamed:
        besked += f" och döpte om {renamed} hämtade filer"
    flash(request, besked, "success")
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


def _publikationsfragment(request: Request, publication_id: int, meddelande: str):
    """Rendera om publikationens huvud för htmx, annars tillbaka till sidan."""
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            return back("/")
        slug = publication.slug
        latest = repo.latest_issue(session, publication.id)
        site = repo.site_for_title(session, publication.title_id)
        if not request.headers.get("HX-Request"):
            return None, slug
        svar = render(
            request, "publication_head.html", publication=publication,
            latest=latest, site=site, kortmeddelande=meddelande, flashes=[],
        )
    return svar, slug


@router.post("/publications/{publication_id}/verify")
def verify_publication(request: Request, publication_id: int):
    """Testa sajtens uppgifter, men stanna kvar på publikationen."""
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            return back("/")
        site = repo.site_for_title(session, publication.title_id)
        if site is None:
            status = AuthStatus.SUBSCRIPTION_ERROR
            besked = f"Ingen av dina inloggningsuppgifter ger läsrätt till {publication.name}"
        else:
            status = verify_site(session, site)
            besked = (
                "Uppgifterna fungerar"
                if status is AuthStatus.OK
                else site.auth_message or "Uppgifterna gäller inte längre"
            )
    svar, slug = _publikationsfragment(request, publication_id, besked)
    if svar is not None:
        return svar
    flash(request, besked, "success" if status is AuthStatus.OK else "error")
    return back(f"/publications/{slug}")


@router.post("/publications/{publication_id}/poll")
def poll_now(request: Request, publication_id: int):
    fel = False
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            return back("/")
        slug = publication.slug
        try:
            result = poll_publication(session, publication)
        except PrenlyError as error:
            besked, fel = f"Katalogkontrollen misslyckades: {error}", True
        else:
            besked = (
                f"{result['found']} utgåvor i katalogen, {result['new']} nya, "
                f"{result['queued']} köade"
            )
    svar, slug = _publikationsfragment(request, publication_id, besked)
    if svar is not None:
        # Utgåvetabellen kan ha fått nya rader, så den byts ut på köpet.
        svar.headers["HX-Trigger"] = "utgavor-andrade"
        return svar
    flash(request, besked, "error" if fel else "success")
    return back(f"/publications/{slug}")


@router.post("/publications/{publication_id}/delete")
def delete_publication(request: Request, publication_id: int):
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is not None:
            publication.enabled = False
            session.add(publication)
    flash(request, "Publikationen togs bort", "success")
    return back("/")


# --------------------------------------------------------------------------
# Utgåvor och jobb
# --------------------------------------------------------------------------


@router.post("/issues/{issue_id}/download")
def download_issue_now(
    request: Request, issue_id: int, force: str = Form(""), page: int = Form(1)
):
    with get_session() as session:
        issue = repo.get_issue(session, issue_id)
        if issue is None:
            return back("/")
        publication = repo.get_publication_for_title(session, issue.title_id)
        slug = publication.slug if publication else ""
        job = repo.enqueue_download(session, issue, force=force == "on")
        namn = issue.name
        disk_only = issue.disk_only
        kontext = _utgavekontext(session, publication, page) if publication else None

    if job is not None:
        besked = f"Köade nedladdning av {namn}"
    elif disk_only:
        besked = f"{namn} finns bara på disk och går inte att hämta"
    else:
        besked = "Utgåvan har redan ett aktivt jobb"
    if request.headers.get("HX-Request") and kontext is not None:
        kontext.pop("_fel", None)
        return render(
            request, "issues_section.html", kortmeddelande=besked, flashes=[], **kontext
        )
    flash(request, besked, "info" if job is None else "success")
    return back(f"/publications/{slug}" if slug else "/jobs")


@router.post("/issues/{issue_id}/delete-file")
def delete_disk_issue(request: Request, issue_id: int, page: int = Form(1)):
    """Radera en fil som bara finns på disk, och raden med den.

    Bara diskposter: en katalogutgåva går att hämta igen, och för den vore
    en radering inte oåterkallelig utan bara omväg till samma fil.
    """
    with get_session() as session:
        issue = repo.get_issue(session, issue_id)
        if issue is None:
            return back("/")
        publication = repo.get_publication_for_title(session, issue.title_id)
        slug = publication.slug if publication else ""
        namn = issue.name
        if not issue.disk_only:
            besked = f"{namn} finns i katalogen och tas inte bort här"
            kontext = _utgavekontext(session, publication, page) if publication else None
        else:
            besked = _radera_diskfil(session, issue)
            session.flush()
            kontext = _utgavekontext(session, publication, page) if publication else None

    if request.headers.get("HX-Request") and kontext is not None:
        kontext.pop("_fel", None)
        return render(
            request, "issues_section.html", kortmeddelande=besked, flashes=[], **kontext
        )
    flash(request, besked, "success")
    return back(f"/publications/{slug}" if slug else "/library")


def _radera_diskfil(session, issue) -> str:
    """Ta bort PDF:en och utgåveraden. Filen går inte att hämta igen."""
    namn = issue.name
    path = _bibliotekssokvag(issue.file_path)
    mal = resolve_file(path) if path is not None else None
    if mal is not None:
        try:
            mal.unlink()
        except OSError as error:
            # Raden är den enda pekaren till filen, så den får stå kvar.
            logger.warning("Kunde inte radera %s: %s", mal, error)
            return f"Kunde inte ta bort filen för {namn}: {error}"
    session.delete(issue)
    if mal is not None:
        return f"Tog bort {namn} och dess fil"
    return f"Tog bort raden {namn}, men filen fanns inte på disk"


@router.post("/publications/{publication_id}/issues/batch")
async def batch_download(request: Request, publication_id: int):
    """Köa nedladdning av flera markerade utgåvor på en gång."""
    form = await request.form()
    valda = [int(v) for v in form.getlist("valda") if str(v).isdigit()]
    force = form.get("force") == "on"
    page = int(form.get("page") or 1)

    koade, redan_hamtade, aktiva, bara_disk = 0, 0, 0, 0
    with get_session() as session:
        publication = repo.get_publication(session, publication_id)
        if publication is None:
            return back("/")
        slug = publication.slug
        for issue_id in valda:
            issue = repo.get_issue(session, issue_id)
            # Bara utgåvor som hör till den här publikationen, så en
            # tillrättalagd post inte kan köa jobb någon annanstans.
            if issue is None or issue.title_id != publication.title_id:
                continue
            if issue.disk_only:
                # Sidan visar ingen kryssruta för dem, men den kan ha
                # renderats innan importen skapade raden.
                bara_disk += 1
                continue
            if not force and issue.status == IssueStatus.DONE.value:
                # Nedladdningen skulle ändå svara "fanns redan".
                redan_hamtade += 1
                continue
            if repo.enqueue_download(session, issue, force=force) is None:
                aktiva += 1
            else:
                koade += 1
        kontext = _utgavekontext(session, publication, page)

    if not valda:
        besked = "Inga utgåvor markerade"
    else:
        delar = [f"Köade {koade} {'nedladdning' if koade == 1 else 'nedladdningar'}"]
        if redan_hamtade:
            delar.append(f"{redan_hamtade} redan hämtade")
        if aktiva:
            delar.append(f"{aktiva} hade redan ett aktivt jobb")
        if bara_disk:
            delar.append(f"{bara_disk} finns bara på disk och går inte att hämta")
        besked = ", ".join(delar)

    if request.headers.get("HX-Request"):
        kontext.pop("_fel", None)
        return render(
            request, "issues_section.html", kortmeddelande=besked, flashes=[], **kontext
        )
    flash(request, besked, "info" if not koade else "success")
    return back(f"/publications/{slug}?page={page}")


def _forhandsgranskningsdata(issue_id: int):
    """Uppgifterna som krävs för att bygga en förhandsgranskning."""
    with get_session() as session:
        issue = repo.get_issue(session, issue_id)
        if issue is None:
            return None, "Utgåvan finns inte"
        if issue.disk_only:
            return None, "Utgåvan finns bara på disk och går inte att bygga om"
        publication = repo.get_publication_for_title(session, issue.title_id)
        if publication is None:
            return None, "Publikationen finns inte"
        site = repo.site_for_title(session, publication.title_id)
        if site is None:
            return None, (
                f"Ingen av dina inloggningsuppgifter ger läsrätt till {publication.name}"
            )
        return (
            repo.credentials_for_site(site),
            publication.title_id,
            issue.uid,
            publication.prefix,
        ), None


@router.post("/issues/{issue_id}/preview")
async def build_preview(issue_id: int):
    """Bygg PDF:en och svara med adressen att öppna den på.

    Bygget kan ta minuter, så det är skilt från öppnandet: knappen visar
    en spinner under tiden och öppnar fliken först när filen finns.
    """
    data, fel = _forhandsgranskningsdata(issue_id)
    if data is None:
        return JSONResponse({"error": fel}, status_code=404)
    creds, title_id, issue_uid, prefix = data

    try:
        await run_in_threadpool(
            build_issue_preview, creds, title_id, issue_uid, issue_id, prefix
        )
    except PrenlyError as error:
        logger.warning("Kunde inte förhandsgranska utgåva %s: %s", issue_id, error)
        return JSONResponse(
            {"error": f"Förhandsgranskningen misslyckades: {error}"}, status_code=502
        )
    return JSONResponse({"url": f"/issues/{issue_id}/preview"})


@router.get("/issues/{issue_id}/preview")
async def open_preview(issue_id: int):
    """Servera en redan byggd förhandsgranskning, eller bygg den vid behov."""
    data, fel = _forhandsgranskningsdata(issue_id)
    if data is None:
        return JSONResponse({"error": fel}, status_code=404)
    creds, title_id, issue_uid, prefix = data

    try:
        path = await run_in_threadpool(
            build_issue_preview, creds, title_id, issue_uid, issue_id, prefix
        )
    except PrenlyError as error:
        return JSONResponse(
            {"error": f"Förhandsgranskningen misslyckades: {error}"}, status_code=502
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------
# Inställningar
# --------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with get_session() as session:
        calibre_config = calibre.load_config(session)
        poll_cron = repo.get_setting(session, POLL_CRON_KEY, DEFAULT_POLL_CRON)
        history_days = repo.get_setting(
            session, JOB_HISTORY_DAYS_KEY, str(DEFAULT_JOB_HISTORY_DAYS)
        )
        browse_rotter = browse_roots()
    return render(
        request,
        "settings.html",
        poll_cron=poll_cron,
        calibre=calibre_config,
        history_days=history_days,
        output_dir=output_root(),
        timezone=timezone_name(),
        next_poll=next_poll_time(),
        browse_roots=browse_rotter,
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


@router.post("/settings/calibre")
def save_calibre_settings(
    request: Request,
    enabled: str = Form(""),
    url: str = Form(""),
    library: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
):
    """Spara Calibre-uppgifterna.

    Ett tomt lösenordsfält betyder oförändrat, inte tomt: fältet fylls
    aldrig i från databasen och skulle annars nollas vid varje sparning.
    """
    with get_session() as session:
        repo.set_setting(session, calibre.ENABLED_KEY, "on" if enabled else "")
        repo.set_setting(session, calibre.URL_KEY, url.strip())
        repo.set_setting(session, calibre.LIBRARY_KEY, library.strip())
        repo.set_setting(session, calibre.USERNAME_KEY, username.strip())
        if password:
            repo.set_setting(session, calibre.PASSWORD_KEY, password)
    flash(request, "Calibre-inställningarna sparade", "success")
    return back("/settings")


@router.post("/settings/calibre/test")
def test_calibre(
    request: Request,
    enabled: str = Form(""),
    url: str = Form(""),
    library: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
):
    """Kör ett ofarligt calibredb-anrop och rapportera vad som händer.

    Uppgifterna tas ur formuläret och inte ur databasen, så man kan prova
    dem innan man sparar. Tomt lösenordsfält betyder det sparade, precis
    som vid sparning.
    """
    with get_session() as session:
        sparat = calibre.load_config(session)
    config = calibre.CalibreConfig(
        enabled=bool(enabled),
        url=url.strip(),
        library=library.strip(),
        username=username.strip(),
        password=password or sparat.password,
    )
    if not config.configured:
        besked = "Fyll i adress och bibliotek och slå på integrationen först"
    else:
        try:
            calibre.run(config, calibre.build_list_argv(config))
        except calibre.CalibreError as error:
            besked = f"Calibre svarade: {error}"
        else:
            besked = f"Kontakt med {config.with_library}"
    flash(request, besked, "info")
    return back("/settings")


@router.get("/settings/directories")
def list_directories(root: str, path: str = ""):
    """Lista en katalog, efter containment-kontroll här och inte bara i ui:t."""
    payload = directory_payload(root, path)
    if payload is None:
        return JSONResponse({"error": "Katalogen är inte tillåten"}, status_code=403)
    return JSONResponse(payload)


@router.post("/settings/import")
def import_configs(request: Request, directory: str = Form("")):
    path = resolve_allowed_directory(directory.strip())
    if path is None:
        flash(request, "Katalogen är inte tillåten eller finns inte", "error")
        return back("/settings")
    with get_session() as session:
        imported = import_directory(session, path)
    flash(request, f"Importerade {len(imported)} konfigurationsfiler", "success")
    return back("/settings")
