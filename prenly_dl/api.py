"""HTTP-klient mot Prenly/Textalks web-reader-API.

Alla funktioner tar en `requests.Session` och ett `Credentials`-objekt
explicit, och kastar undantag ur `prenly_dl.errors` vid fel.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .errors import PrenlyAuthError, PrenlyError, PrenlyHTTPError, raise_for_rpc_error

logger = logging.getLogger(__name__)

DEFAULT_CDN = "https://mediacdn.prenly.com"
CONTENT_BASE = "https://content.textalk.se"
CATALOGUE_BASE = "https://apicdn.prenly.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:98.0) "
    "Gecko/20100101 Firefox/98.0"
)
DEFAULT_TIMEOUT = 60

IMAGE_CONTENT_TYPES = (
    "image/webp",
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/svg",
)


@dataclass
class Credentials:
    """Inloggningsuppgifter för en Prenly-sajt.

    `site` är läsarens webbadress, `textalk_auth` klientnyckeln och `auth`
    sessionstoken (utan "Bearer "). `cdn` skiljer sig mellan sajter.
    """

    site: str
    textalk_auth: str
    auth: str
    cdn: str = DEFAULT_CDN

    def __post_init__(self) -> None:
        self.site = (self.site or "").rstrip("/")
        self.cdn = (self.cdn or DEFAULT_CDN).rstrip("/")


def build_session() -> requests.Session:
    """Session med User-Agent och omförsök på tillfälliga serverfel."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _origin_headers(creds: Credentials) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Language": "sv-SE,en-US,en",
        "Origin": creds.site,
        "Referer": f"{creds.site}/",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }


def _rpc_headers(creds: Credentials) -> dict[str, str]:
    headers = _origin_headers(creds)
    headers.update({
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json-rpc",
        "X-Textalk-Content-Client-Authorize": creds.textalk_auth,
        "X-Textalk-Content-Product-Type": "webapp",
        "Authorization": f"Bearer {creds.auth}",
    })
    return headers


def _get_json(session: requests.Session, url: str, headers: dict[str, str]) -> Any:
    try:
        response = session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as error:
        raise PrenlyHTTPError(None, url, f"Nätverksfel mot {url}: {error}") from error
    if response.status_code != 200:
        raise PrenlyHTTPError(response.status_code, url)
    try:
        return response.json()
    except ValueError as error:
        raise PrenlyHTTPError(response.status_code, url, f"Ogiltigt JSON-svar från {url}") from error


# Klientnyckeln ligger i läsarens egen sida som PrenlyData.client.authToken.
# Den är inte hemlig - vem som helst kan läsa den - men den är sajtens, och
# utan den svarar API:t inte alls.
_KLIENTNYCKEL = re.compile(r'"authToken"\s*:\s*"([^"]+)"')


def fetch_client_key(session: requests.Session, site: str) -> str:
    """Hämta sajtens klientnyckel ur läsarens startsida."""
    url = site.rstrip("/") + "/"
    try:
        svar = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
        svar.raise_for_status()
    except requests.RequestException as error:
        raise PrenlyError(f"Kunde inte läsa {url}: {error}") from error
    traff = _KLIENTNYCKEL.search(svar.text)
    if not traff:
        raise PrenlyError(f"Hittade ingen klientnyckel på {url}")
    return traff.group(1)


def _session_headers(site: str, client_key: str, token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "User-Agent": USER_AGENT,
        "Origin": site.rstrip("/"),
        "Referer": site.rstrip("/") + "/",
        "X-Textalk-Content-Client-Authorize": client_key,
        "X-Textalk-Content-Product-Type": "webapp",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def create_session(session: requests.Session, site: str, client_key: str) -> str:
    """Skapa en anonym session och returnera dess token.

    Inloggningen sker i två steg: först en session, sedan en inloggning
    på den. Utan det första steget finns ingen Bearer-token att logga in
    med.
    """
    url = f"{CONTENT_BASE}/api/web-reader/v1/sessions"
    try:
        svar = session.post(url, headers=_session_headers(site, client_key), timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as error:
        raise PrenlyError(f"Kunde inte skapa session: {error}") from error
    if svar.status_code not in (200, 201):
        raise PrenlyError(f"Kunde inte skapa session: {svar.status_code} {svar.text[:120]}")
    token = (svar.json() or {}).get("token")
    if not token:
        raise PrenlyError("Sessionssvaret saknade token")
    return str(token)


def login(
    session: requests.Session,
    site: str,
    client_key: str,
    token: str,
    username: str,
    password: str,
) -> str:
    """Logga in på en befintlig session och returnera sessionstoken.

    Servern svarar med en token när den byter ut sessionen; gör den inte
    det är det den befintliga som blivit inloggad, och den gäller vidare.
    """
    url = f"{CONTENT_BASE}/api/web-reader/v1/sessions/authentications/username"
    headers = _session_headers(site, client_key, token)
    headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
    try:
        svar = session.post(
            url, headers=headers, timeout=DEFAULT_TIMEOUT,
            data={"username": username, "password": password, "ask_polite": "true"},
        )
    except requests.RequestException as error:
        raise PrenlyError(f"Inloggningen misslyckades: {error}") from error
    if svar.status_code in (400, 401, 403):
        besked = ""
        try:
            besked = (svar.json() or {}).get("message") or ""
        except ValueError:
            pass
        raise PrenlyAuthError(besked or "Fel användarnamn eller lösenord")
    if not svar.ok:
        raise PrenlyError(f"Inloggningen misslyckades: {svar.status_code} {svar.text[:120]}")
    try:
        ny = (svar.json() or {}).get("token")
    except ValueError:
        ny = None
    return str(ny) if ny else token


def get_context_token(session: requests.Session, creds: Credentials) -> str:
    """Hämta den kortlivade context-token som katalog-API:t kräver."""
    url = f"{CONTENT_BASE}/api/web-reader/v1/context-token"
    data = _get_json(session, url, _rpc_headers(creds))
    token = data.get("token")
    if not token:
        raise PrenlyHTTPError(200, url, "Svaret saknade context-token")
    return token


def get_titles(
    session: requests.Session, creds: Credentials, context_token: str
) -> list[dict]:
    """Alla publikationer sajten exponerar, med namn och läsbarhet."""
    url = f"{CATALOGUE_BASE}/api/web-reader/v1/titles?context_token={context_token}"
    data = _get_json(session, url, _origin_headers(creds))
    return data if isinstance(data, list) else []


def get_catalogue_issues(
    session: requests.Session,
    creds: Credentials,
    context_token: str,
    title_id: int,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Utgåvor för en publikation, nyast först.

    `offset` bläddrar bakåt i katalogen. Ett svar med färre poster än
    `limit` betyder att katalogen är slut.

    Returnerar råa katalogposter (uid, name, publication_date, media).
    """
    url = (
        f"{CATALOGUE_BASE}/api/web-reader/v1/issues"
        f"?title_ids[]={title_id}&limit={limit}&context_token={context_token}"
    )
    if offset:
        url += f"&offset={offset}"
    data = _get_json(session, url, _origin_headers(creds))
    return data if isinstance(data, list) else []


def get_issue(
    session: requests.Session, creds: Credentials, title_id: int, issue_uid: str
) -> dict:
    """Hämta hela utgåvan via JSON-RPC (innehåller uppslag och checksummor)."""
    url = f"{CONTENT_BASE}/api/v2/"
    payload = {
        "jsonrpc": "2.0",
        "method": "Issue.get",
        "params": {"title_id": str(title_id), "issue_uid": str(issue_uid)},
        "id": 1,
    }
    try:
        response = session.post(
            url, json=payload, headers=_rpc_headers(creds), timeout=DEFAULT_TIMEOUT
        )
    except requests.RequestException as error:
        raise PrenlyHTTPError(None, url, f"Nätverksfel mot {url}: {error}") from error
    if response.status_code != 200:
        raise PrenlyHTTPError(response.status_code, url)
    try:
        result = response.json()
    except ValueError as error:
        raise PrenlyHTTPError(response.status_code, url, "Ogiltigt JSON-svar") from error
    if "error" in result:
        raise_for_rpc_error(result["error"])
    return result["result"]


def get_page_checksums(issue_data: dict) -> dict[str, str]:
    """Nyckel i läsordning -> checksumma.

    Nyckeln bygger på uppslagets globala position och sidans plats i
    uppslaget, inte på sidnumret. En tidning med flera delar börjar om
    numreringen i varje del: Expressen 2026-08-21 har tre delar med
    sidorna 1-32, 1-12 och 1-20, alltså tre sidor som heter 1. Med
    sidnumret som nyckel skrev delarna över varandra - hälften av
    sidorna försvann och förstasidan blev sista delens.

    Nycklarna nollutfylls så att lexikal sortering matchar numerisk:
    de blir filnamn, och filordningen avgör ordningen i PDF:en.
    """
    spreads = issue_data.get("replica_spreads", [])
    poster: list[tuple[int, int, str]] = []
    for ordning, spread in enumerate(spreads, start=1):
        try:
            position = int(spread.get("position_number"))
        except (TypeError, ValueError):
            # Utan position får sidnumret bestämma. Det duger så länge
            # utgåvan har en enda del, och API:t kan leverera uppslagen i
            # annan ordning än läsordningen.
            position = 0
        for index, page in enumerate(spread.get("pages", [])):
            if position:
                poster.append((position, index, page["media"][0]["checksum"]))
            else:
                poster.append((int(page["page_no"]), index, page["media"][0]["checksum"]))
    poster.sort(key=lambda post: (post[0], post[1]))
    return {f"{pos:05d}-{index:02d}": checksum for pos, index, checksum in poster}


@dataclass(frozen=True)
class Sida:
    """En sida att hämta: nyckeln ger ordningen, titeln var den bor."""

    nyckel: str
    title_id: int
    checksum: str


def collect_pages(
    session: requests.Session,
    creds: Credentials,
    issue_data: dict,
    title_id: int,
) -> list[Sida]:
    """Alla sidor i en utgåva, bilagorna inräknade och i läsordning.

    En utgåva kan ha bilagor som är egna utgåvor med eget title_id och
    uid - Dagens Nyheter 2026-08-21 har Del B och Del C så. De ligger i
    insert_issues och saknades helt i den nedladdade PDF:en, som bara
    innehöll huvuddelens 24 sidor.

    Bilagornas media bor under deras egen titel, så varje sida bär sitt
    title_id vidare till CDN-anropet.
    """
    sidor = [
        Sida(f"0-{nyckel}", title_id, checksum)
        for nyckel, checksum in get_page_checksums(issue_data).items()
    ]
    for ordning, bilaga in enumerate(issue_data.get("insert_issues") or [], start=1):
        bilaga_titel = bilaga.get("title_id")
        bilaga_uid = bilaga.get("issue_uid") or bilaga.get("issue_id")
        if not bilaga_titel or not bilaga_uid:
            logger.warning("Hoppar över bilaga utan titel eller uid: %s", bilaga)
            continue
        try:
            data = get_issue(session, creds, int(bilaga_titel), str(bilaga_uid))
        except PrenlyError as error:
            # Hellre huvuddelen utan bilaga än ingen utgåva alls.
            logger.warning("Kunde inte hämta bilaga %s: %s", bilaga_uid, error)
            continue
        sidor += [
            Sida(f"{ordning}-{nyckel}", int(bilaga_titel), checksum)
            for nyckel, checksum in get_page_checksums(data).items()
        ]
    return sidor


def front_page_headline(issue_data: dict) -> str:
    """Utgåvans rubrik, alltså förstasidesartikelns rubrik.

    Katalogen ger bara datum som namn för en del titlar, men numret har
    ofta ett rubrik som står som rubrik på förstasidan. Verifierat mot
    Tidningen Hemslöjd 2026-08-21: fyra av fyra utgåvor gav exakt det
    rubrik numret marknadsförs med.
    """
    artiklar = {a.get("id"): a for a in issue_data.get("articles", [])}
    for section in issue_data.get("sections", []):
        if section.get("slug") != "forstasidan":
            continue
        for article_id in section.get("article_ids", []):
            rubrik = (artiklar.get(article_id) or {}).get("headline")
            if rubrik:
                return str(rubrik).strip()
    return ""


def download_media(
    session: requests.Session,
    creds: Credentials,
    title_id: int,
    checksum: str,
    accept: str = "application/pdf",
) -> requests.Response:
    """Hämta media från CDN:en.

    CDN:en svarar utifrån Accept-headern: samma checksumma ger en PDF för
    en sida och en JPEG för ett omslag, så anroparen måste säga vad den
    vill ha.
    """
    url = f"{creds.cdn}/api/v2/media/get/{title_id}/{checksum}?h=abc"
    headers = _origin_headers(creds)
    headers["Accept"] = accept
    try:
        response = session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as error:
        raise PrenlyHTTPError(None, url, f"Nätverksfel mot {url}: {error}") from error
    if response.status_code != 200:
        raise PrenlyHTTPError(response.status_code, url)
    return response
