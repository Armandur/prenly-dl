"""Hitta böcker i Calibre som motsvarar prenly-dl:s utgåvor.

Matchningen är ett förslag, aldrig ett beslut: uppmätt 2026-08-21 gav
sökningen på 2024-04-02 både Tidningen Hemslöjd och romanen "Just for the
Summer". Ett tyst val hade lagt tidningen som format på romanen, så
kandidaterna rankas och användaren avgör.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from . import calibre_api
from .calibre import CalibreConfig, format_title
from .db import repository as repo
from .db.models import Issue, Publication

logger = logging.getLogger(__name__)

# Poäng per tecken på att det är samma utgåva. Titeln väger tyngst: med
# publikationens titelmönster blir den ofta teckenidentisk med den post
# biblioteket redan har.
# Samma livslängd som bibliotekets cache: förslagen bygger på det.
CACHE_SEKUNDER = 300

POANG_TITEL = 100
POANG_FORFATTARE = 30
POANG_DATUM = 10
# Enbart samma utgivningsdatum är inget tecken alls: uppmätt mot ett
# bibliotek på 1167 böcker gav datummatchning 434 kandidater, nästan alla
# romaner som råkade ges ut samma dag. Med tröskeln blev det 12, samtliga
# rätt. Titel eller författare måste alltså också tala för träffen.
MINSTA_POANG = POANG_FORFATTARE


@dataclass
class Kandidat:
    book_id: int
    titel: str
    forfattare: str
    datum: str
    storlek: int | None
    poang: int


@dataclass
class Forslag:
    issue: Issue
    publication: Publication
    vantad_titel: str
    lokal_storlek: int | None
    kandidater: list[Kandidat]


def _lokal_storlek(issue: Issue) -> int | None:
    if not issue.file_path:
        return None
    try:
        return Path(issue.file_path).stat().st_size
    except OSError:
        return None


def _poang(vantad_titel: str, publikation: str, datum: str, bok: dict) -> int:
    poang = 0
    titel = str(bok.get("title") or "").strip()
    if titel and titel.casefold() == vantad_titel.casefold():
        poang += POANG_TITEL
    forfattare = calibre_api.bok_forfattare(bok)
    if forfattare and publikation:
        # "Hemslöjd" mot "Tidningen Hemslöjd": den ena rymmer den andra.
        a, b = forfattare.casefold(), publikation.casefold()
        if a in b or b in a:
            poang += POANG_FORFATTARE
    if datum and calibre_api.bok_datum(bok) == datum:
        poang += POANG_DATUM
    return poang


_forslag_cache: dict[str, tuple[float, list["Forslag"]]] = {}


def glom_forslag() -> None:
    """Kasta cachade förslag, efter att ett beslut ändrat underlaget."""
    _forslag_cache.clear()


def cached_candidates(session, config: CalibreConfig) -> list[Forslag]:
    """Förslagen, cachade en kort stund.

    Proxyrutterna avgränsar mot kandidaternas bok-id, och utan cache hade
    varje omslag på sidan kostat en full matchning mot hela biblioteket.
    """
    nyckel = config.with_library
    cachad = _forslag_cache.get(nyckel)
    if cachad and time.monotonic() - cachad[0] < CACHE_SEKUNDER:
        return cachad[1]
    forslag = find_candidates(session, config)
    _forslag_cache[nyckel] = (time.monotonic(), forslag)
    return forslag


def find_candidates(
    session, config: CalibreConfig, *, limit: int = 500
) -> list[Forslag]:
    """Utgåvor som saknar koppling, med förslag ur biblioteket.

    Hela biblioteket läses i ett anrop och matchningen sker lokalt, så
    kostnaden är densamma för en utgåva som för hundra.
    """
    bibliotek = calibre_api.fetch_library(config)
    per_datum: dict[str, list[dict]] = {}
    per_titel: dict[str, list[dict]] = {}
    for book_id, bok in bibliotek.bocker.items():
        bok = {**bok, "id": book_id}
        datum = calibre_api.bok_datum(bok)
        if datum:
            per_datum.setdefault(datum, []).append(bok)
        titel = str(bok.get("title") or "").strip().casefold()
        if titel:
            per_titel.setdefault(titel, []).append(bok)

    forslag: list[Forslag] = []
    for publication in repo.list_publications(session):
        for issue in repo.issues_without_calibre(session, publication.title_id):
            datum = issue.publication_date or ""
            # Grovsållning först: numret kostar en fråga per utgåva, och de
            # allra flesta utgåvor har ingen kandidat alls.
            grovt = per_datum.get(datum, [])
            if not grovt and not issue.theme:
                continue
            nummer = repo.issue_number_in_year(session, issue)
            vantad = format_title(
                publication.calibre_title,
                publikation=publication.name,
                utgava=issue.name,
                datum=datum,
                tema=issue.theme,
                nummer=nummer,
            )
            traffar = {
                bok["id"]: bok
                for bok in grovt + per_titel.get(vantad.casefold(), [])
            }
            if not traffar:
                continue
            kandidater = [
                Kandidat(
                    book_id=bok["id"],
                    titel=str(bok.get("title") or ""),
                    forfattare=calibre_api.bok_forfattare(bok),
                    datum=calibre_api.bok_datum(bok),
                    storlek=calibre_api.bok_storlek(bok),
                    poang=_poang(vantad, publication.name, datum, bok),
                )
                for bok in traffar.values()
            ]
            kandidater = [k for k in kandidater if k.poang >= MINSTA_POANG]
            if not kandidater:
                continue
            kandidater.sort(key=lambda k: (-k.poang, k.book_id))
            forslag.append(
                Forslag(
                    issue=issue,
                    publication=publication,
                    vantad_titel=vantad,
                    lokal_storlek=_lokal_storlek(issue),
                    kandidater=kandidater,
                )
            )
            if len(forslag) >= limit:
                logger.warning(
                    "Visar de första %d förslagen, fler finns kvar att gå igenom",
                    limit,
                )
                return forslag
    return forslag


def tillatna_bocker(session, config: CalibreConfig) -> set[int]:
    """Bok-id som webbui:t får hämta omslag och filer för.

    Proxyrutterna går med de sparade uppgifterna, så utan den här
    avgränsningen hade prenly-dl blivit en öppen väg in i hela
    biblioteket. Samma resonemang som containment i path_browser.
    """
    tillatna = set(repo.calibre_ids(session))
    try:
        for forslag in cached_candidates(session, config):
            tillatna.update(k.book_id for k in forslag.kandidater)
    except Exception:  # noqa: BLE001 - en trasig server ska inte öppna upp
        logger.warning("Kunde inte läsa kandidater för avgränsningen")
    return tillatna
