"""HTTP-klient mot Prenly/Textalks web-reader-API.

Alla funktioner tar en `requests.Session` och ett `Credentials`-objekt
explicit, och kastar undantag ur `prenly_dl.errors` vid fel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .errors import PrenlyHTTPError, raise_for_rpc_error

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
) -> list[dict]:
    """De `limit` senaste utgåvorna för en publikation.

    Returnerar råa katalogposter (uid, name, publication_date, media).
    """
    url = (
        f"{CATALOGUE_BASE}/api/web-reader/v1/issues"
        f"?title_ids[]={title_id}&limit={limit}&context_token={context_token}"
    )
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
    """Sidnummer (nollutfyllt) -> checksumma, i läsordning.

    Sidnumren fylls ut till fem tecken så att lexikal sortering matchar
    numerisk, och uppslagen sorteras explicit eftersom API:t kan leverera
    dem i annan ordning än läsordningen.
    """
    checksums: dict[str, str] = {}
    for spread in issue_data.get("replica_spreads", []):
        for page in spread.get("pages", []):
            page_num = str(int(page["page_no"])).zfill(5)
            checksums[page_num] = page["media"][0]["checksum"]
    return dict(sorted(checksums.items(), key=lambda kv: int(kv[0])))


def download_media(
    session: requests.Session, creds: Credentials, title_id: int, checksum: str
) -> requests.Response:
    """Hämta en sida (PDF eller bild) från CDN:en."""
    url = f"{creds.cdn}/api/v2/media/get/{title_id}/{checksum}?h=abc"
    headers = _origin_headers(creds)
    headers["Accept"] = "application/pdf"
    try:
        response = session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as error:
        raise PrenlyHTTPError(None, url, f"Nätverksfel mot {url}: {error}") from error
    if response.status_code != 200:
        raise PrenlyHTTPError(response.status_code, url)
    return response
