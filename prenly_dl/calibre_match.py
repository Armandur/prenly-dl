"""Hitta böcker i Calibre som motsvarar prenly-dl:s utgåvor.

Matchningen är ett förslag, aldrig ett beslut: uppmätt 2026-08-21 gav
sökningen på 2024-04-02 både Tidningen Hemslöjd och romanen "Just for the
Summer". Ett tyst val hade lagt tidningen som format på romanen, så
kandidaterna rankas och användaren avgör.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import calibre_api
from .calibre import CalibreConfig, CalibreError, format_title
from .db import repository as repo
from .db.models import Issue, Publication

logger = logging.getLogger(__name__)

# Poäng per tecken på att det är samma utgåva. Titeln väger tyngst: med
# publikationens titelmönster blir den ofta teckenidentisk med den post
# biblioteket redan har.
# Samma livslängd som bibliotekets cache: förslagen bygger på det.
CACHE_SEKUNDER = 300

POANG_TITEL = 100
# År och nummer ur bokens egen titel. Räknas bara tillsammans med
# författarstöd: kombinationen är unik inom en tidning, inte i ett
# bibliotek med flera.
POANG_ARNUMMER = 60
POANG_FORFATTARE = 30
POANG_DATUM = 10

# Namngivning på formen "2011-2 - Drömmare Nördar och Visionärer" eller
# "2023-1 Ylle". Uppmätt mot biblioteket: 82 böcker följer den, samtliga
# Hemslöjd, och 179 böcker saknar utgivningsdatum helt - för dem är det
# här enda vägen till en matchning.
ARNUMMER = re.compile(r"^(\d{4})-(\d{1,2})\b")
# Enbart samma utgivningsdatum är inget tecken alls: uppmätt mot ett
# bibliotek på 1167 böcker gav datummatchning 434 kandidater, nästan alla
# romaner som råkade ges ut samma dag. Med tröskeln blev det 12, samtliga
# rätt. Titel eller författare måste alltså också tala för träffen.
MINSTA_POANG = POANG_FORFATTARE


# Calibre skriver in metadata i PDF:en, vilket ensamt gav 59 416 bytes
# skillnad på en fil om 15 MB. Under en procent är alltså inte ett tecken
# på olika innehåll.
STORLEKSTOLERANS = 0.01


@dataclass
class Kandidat:
    book_id: int
    titel: str
    forfattare: str
    datum: str
    storlek: int | None
    poang: int
    # None när någon av filerna saknas och det inte går att jämföra.
    samma_storlek: bool | None = None


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


def ar_och_nummer(titel: str) -> tuple[int, int] | None:
    """År och nummer ur en boktitel, om den är namngiven på det viset."""
    traff = ARNUMMER.match(titel.strip())
    if not traff:
        return None
    return int(traff.group(1)), int(traff.group(2))


def _samma_storlek(lokal: int | None, i_calibre: int | None) -> bool | None:
    """Om filerna verkar vara samma. None när jämförelsen inte går att göra.

    Lika storlek är inget bevis, men det säger att en ersättning inte
    skulle ändra något - och då är varningen om oåterkallelig radering
    fel besked.
    """
    if not lokal or not i_calibre:
        return None
    return abs(lokal - i_calibre) <= i_calibre * STORLEKSTOLERANS


def _poang(
    vantad_titel: str,
    publikation: str,
    datum: str,
    bok: dict,
    arnummer: tuple[int, int] | None = None,
) -> int:
    poang = 0
    titel = str(bok.get("title") or "").strip()
    if titel and titel.casefold() == vantad_titel.casefold():
        poang += POANG_TITEL

    forfattare = calibre_api.bok_forfattare(bok)
    samma_utgivare = False
    if forfattare and publikation:
        # "Hemslöjd" mot "Tidningen Hemslöjd": den ena rymmer den andra.
        a, b = forfattare.casefold(), publikation.casefold()
        samma_utgivare = a in b or b in a
    if samma_utgivare:
        poang += POANG_FORFATTARE

    # År och nummer är unikt inom EN tidning, inte över biblioteket.
    # Utan författarkravet matchade Sundsvalls Tidnings femte utgåva 2024
    # mot Hemslöjds "2024-5 - Slöjd & bildning".
    if samma_utgivare and arnummer:
        if ar_och_nummer(titel) == arnummer:
            poang += POANG_ARNUMMER

    if datum and calibre_api.bok_datum(bok) == datum:
        poang += POANG_DATUM
    return poang


_forslag_cache: dict[str, tuple[float, list["Forslag"]]] = {}


def glom_forslag() -> None:
    """Kasta cachade förslag helt. Nästa hämtning matchar om allt."""
    _forslag_cache.clear()


def ta_bort_forslag(issue_id: int) -> None:
    """Plocka bort en avgjord utgåva ur cachen.

    Ett beslut rör bara den utgåvan, så att kasta hela cachen vore att
    matcha om hela biblioteket i onödan: uppmätt tog varje klick 5,6
    sekunder av den anledningen.
    """
    for nyckel, (tid, forslag) in list(_forslag_cache.items()):
        _forslag_cache[nyckel] = (
            tid, [f for f in forslag if f.issue.id != issue_id]
        )


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
    # Böcker som saknar utgivningsdatum går bara att nå den här vägen.
    per_arnummer: dict[tuple[int, int], list[dict]] = {}
    for book_id, bok in bibliotek.bocker.items():
        bok = {**bok, "id": book_id}
        datum = calibre_api.bok_datum(bok)
        if datum:
            per_datum.setdefault(datum, []).append(bok)
        titel = str(bok.get("title") or "").strip()
        if titel:
            per_titel.setdefault(titel.casefold(), []).append(bok)
        nyckel = ar_och_nummer(titel)
        if nyckel:
            per_arnummer.setdefault(nyckel, []).append(bok)

    forslag: list[Forslag] = []
    for publication in repo.list_publications(session):
        # Numren för hela titeln på en gång: en fråga per utgåva blev
        # tusentals och tog sju sekunder.
        nummer_per_datum = repo.issue_numbers_by_date(session, publication.title)
        for issue in repo.issues_without_calibre(session, publication.title_id):
            datum = issue.publication_date or ""
            # Grovsållning först: numret kostar en fråga per utgåva, och de
            # allra flesta utgåvor har ingen kandidat alls.
            grovt = per_datum.get(datum, [])
            # Numret kostar en fråga per utgåva, men det är enda vägen till
            # böcker utan utgivningsdatum, så det räcker inte att sålla på
            # datum här.
            nummer = nummer_per_datum.get(datum)
            arnummer = (
                (int(datum[:4]), nummer)
                if nummer is not None and datum[:4].isdigit()
                else None
            )
            grovt = grovt + per_arnummer.get(arnummer, []) if arnummer else grovt
            if not grovt and not issue.theme:
                continue
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
            lokal_storlek = _lokal_storlek(issue)
            kandidater = [
                Kandidat(
                    book_id=bok["id"],
                    titel=str(bok.get("title") or ""),
                    forfattare=calibre_api.bok_forfattare(bok),
                    datum=calibre_api.bok_datum(bok),
                    storlek=calibre_api.bok_storlek(bok),
                    poang=_poang(vantad, publication.name, datum, bok, arnummer),
                    samma_storlek=_samma_storlek(
                        lokal_storlek, calibre_api.bok_storlek(bok)
                    ),
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
                    lokal_storlek=lokal_storlek,
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


@dataclass
class CalibreStatus:
    """Vad Calibre vet om en utgåva, för utgåvetabellen."""

    book_id: int | None = None
    titel: str = ""
    storlek: int | None = None
    samma_storlek: bool | None = None
    # Utgåvan är inte kopplad men har förslag i matchningsvyn.
    har_forslag: bool = False


def status_per_utgava(
    session, config: CalibreConfig, issues
) -> dict[int, CalibreStatus]:
    """Calibre-status för en handfull utgåvor, för publikationens tabell.

    Bygger på samma cachade bibliotek som matchningsvyn, så kostnaden är
    en uppslagning per rad.
    """
    if not config.configured:
        return {}
    try:
        bibliotek = calibre_api.fetch_library(config)
        med_forslag = {f.issue.id for f in cached_candidates(session, config)}
    except CalibreError as error:
        logger.warning("Kunde inte läsa Calibre för utgåvetabellen: %s", error)
        return {}

    status: dict[int, CalibreStatus] = {}
    for issue in issues:
        if issue.calibre_id:
            bok = bibliotek.bok(issue.calibre_id) or {}
            i_calibre = calibre_api.bok_storlek(bok)
            status[issue.id] = CalibreStatus(
                book_id=issue.calibre_id,
                titel=str(bok.get("title") or ""),
                storlek=i_calibre,
                samma_storlek=_samma_storlek(_lokal_storlek(issue), i_calibre),
            )
        elif issue.id in med_forslag:
            status[issue.id] = CalibreStatus(har_forslag=True)
    return status


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
