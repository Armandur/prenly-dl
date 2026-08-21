"""Läsande anrop mot calibres content server.

calibre-modulen skriver via calibredb. Den här läser: vad biblioteket
innehåller, omslag och filer. Uppdelningen följer mekanismen - subprocess
mot HTTP - och håller båda filerna läsbara.

Servern använder digest-auth, inte basic. Basic ger "Unsupported
authentication method" med status 400.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests
from requests.auth import HTTPDigestAuth

from .calibre import CalibreConfig, CalibreError

logger = logging.getLogger(__name__)

TIMEOUT = 30
# Hela biblioteket kommer i ett anrop: 1167 böcker gav 5,3 MB på 1,3
# sekunder. Det är för mycket per sidvisning men billigt en gång i taget,
# så matchningen slipper ett sökanrop per utgåva.
CACHE_SEKUNDER = 300


@dataclass
class Bibliotek:
    """Calibres böcker, som de såg ut vid hämtningen."""

    bocker: dict[int, dict]
    hamtat: float

    def bok(self, book_id: int) -> dict | None:
        return self.bocker.get(book_id)


_cache: dict[str, Bibliotek] = {}


def _session(config: CalibreConfig) -> requests.Session:
    session = requests.Session()
    if config.username:
        session.auth = HTTPDigestAuth(config.username, config.password)
    return session


def _hamta(config: CalibreConfig, sokvag: str, **kwargs) -> requests.Response:
    url = f"{config.url.strip().rstrip('/')}{sokvag}"
    try:
        svar = _session(config).get(url, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as error:
        raise CalibreError(f"Kunde inte nå {url}: {error}") from error
    if svar.status_code == 401:
        raise CalibreError("Calibre avvisade inloggningsuppgifterna")
    if not svar.ok:
        raise CalibreError(f"{url} svarade {svar.status_code}")
    return svar


def fetch_library(config: CalibreConfig, *, force: bool = False) -> Bibliotek:
    """Alla böcker i biblioteket, med metadata.

    Två anrop: id-listan och en bulkhämtning av metadata. Resultatet
    cachas, annars kostar varje sidvisning av matchningsvyn 5 MB.
    """
    nyckel = config.with_library
    cachad = _cache.get(nyckel)
    if cachad and not force and time.monotonic() - cachad.hamtat < CACHE_SEKUNDER:
        return cachad

    sok = _hamta(
        config, f"/ajax/search/{config.library.strip()}", params={"query": "", "num": 100_000}
    ).json()
    ids = [int(i) for i in sok.get("book_ids", [])]
    if not ids:
        bibliotek = Bibliotek({}, time.monotonic())
        _cache[nyckel] = bibliotek
        return bibliotek

    data = _hamta(
        config,
        f"/ajax/books/{config.library.strip()}",
        params={"ids": ",".join(str(i) for i in ids)},
    ).json()
    bocker = {int(k): v for k, v in data.items() if v}
    logger.info("Läste %d böcker ur Calibre", len(bocker))
    bibliotek = Bibliotek(bocker, time.monotonic())
    _cache[nyckel] = bibliotek
    return bibliotek


def glom_cache() -> None:
    """Töm cachen, så nästa hämtning går till servern."""
    _cache.clear()


def fetch_cover(config: CalibreConfig, book_id: int) -> bytes:
    """Bokens omslag som bytes."""
    return _hamta(config, f"/get/cover/{book_id}/{config.library.strip()}").content


def open_format(config: CalibreConfig, book_id: int) -> requests.Response:
    """Bokens fil som en strömmande respons, för vidarebefordran."""
    return _hamta(
        config, f"/get/PDF/{book_id}/{config.library.strip()}", stream=True
    )


# --------------------------------------------------------------------------
# Fälten vi bryr oss om
# --------------------------------------------------------------------------


def bok_datum(bok: dict) -> str:
    """Utgivningsdatumet som ISO-dag, eller tom sträng.

    Calibre svarar med full tidsstämpel, 2024-04-02T00:00:00+00:00, och
    använder år 0101 för "inget datum".
    """
    ratt = bok.get("pubdate")
    # Bulksvaret serialiserar saknat datum som strängen "None", inte som
    # JSON-null. Uppmätt: 179 av 1167 böcker.
    if ratt in (None, "None", ""):
        return ""
    varde = str(ratt)[:10]
    # Calibre använder år 0101 för "inget datum".
    return "" if varde.startswith("0101") else varde


def bok_storlek(bok: dict) -> int | None:
    """Filens storlek på disk i Calibre, för jämförelse mot vår egen."""
    format_data = bok.get("format_metadata") or {}
    for nyckel in ("pdf", "PDF"):
        storlek = (format_data.get(nyckel) or {}).get("size")
        if storlek:
            return int(storlek)
    return None


def bok_forfattare(bok: dict) -> str:
    forfattare = bok.get("authors") or []
    return ", ".join(str(f) for f in forfattare)
