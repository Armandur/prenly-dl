"""Skjut in hämtade utgåvor i Calibre via calibredb.

Anropen går till en körande calibre content server och inte mot
biblioteksmappen direkt: servern äger biblioteket, så vi slipper slåss om
låset på metadata.db med calibre-web-automated som läser samma bibliotek.

Ett fel här får aldrig fälla en nedladdning som redan lyckats - PDF:en
ligger på disk och är det som räknas. Anroparen fångar CalibreError och
märker jobbet.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from string import Formatter
from pathlib import Path

logger = logging.getLogger(__name__)

# Nycklarna i settings-tabellen.
ENABLED_KEY = "calibre_enabled"
URL_KEY = "calibre_url"
LIBRARY_KEY = "calibre_library"
USERNAME_KEY = "calibre_username"
PASSWORD_KEY = "calibre_password"

BINARY = "calibredb"
# En stor PDF tar tid att skicka över nätet, men ett hängande anrop får
# inte hålla arbetaren upptagen i evighet.
TIMEOUT = 300

# calibredb skriver "Added book ids: 1234" vid lyckat add. Formatet är
# inte dokumenterat och har varierat mellan versioner, så parsningen är
# tolerant och får misslyckas utan att fälla nedladdningen.
ID_MONSTER = re.compile(
    r"(?:Added|Merged) book ids?:\s*([0-9,\s]+)", re.IGNORECASE
)
# Reserv för när utskriften inte är engelsk. Verifierat 2026-08-21: en
# svensk calibre svarade "Tillagda bokidentifierare: 1775", och miljön
# nedan hann inte hjälpa eftersom språket kom från serversidan.
ID_RESERV = re.compile(r":\s*([0-9]+(?:\s*,\s*[0-9]+)*)\s*$")

# calibredb följer miljöns språk, och utskriften är det enda sättet att
# få reda på bok-id:t. Engelska är det som parsas.
SPRAKMILJO = {"CALIBRE_OVERRIDE_LANG": "en", "LC_ALL": "C", "LANG": "C"}


class CalibreError(RuntimeError):
    """Något gick fel mot Calibre. Nedladdningen är fortfarande giltig."""


@dataclass(frozen=True)
class CalibreConfig:
    enabled: bool = False
    url: str = ""
    library: str = ""
    username: str = ""
    password: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.url.strip() and self.library.strip())

    @property
    def with_library(self) -> str:
        """Adressen calibredb vill ha: http://host:port/#bibliotek."""
        return f"{self.url.strip().rstrip('/')}/#{self.library.strip()}"


def load_config(session) -> CalibreConfig:
    from .db import repository as repo

    return CalibreConfig(
        enabled=repo.get_setting(session, ENABLED_KEY, "") == "on",
        url=repo.get_setting(session, URL_KEY, ""),
        library=repo.get_setting(session, LIBRARY_KEY, ""),
        username=repo.get_setting(session, USERNAME_KEY, ""),
        password=repo.get_setting(session, PASSWORD_KEY, ""),
    )


def build_add_argv(
    config: CalibreConfig,
    path: Path,
    *,
    title: str,
    authors: str,
    series: str = "",
    series_index: str = "",
    cover: Path | None = None,
) -> list[str]:
    """Argumenten till calibredb add, utan lösenordet.

    Lösenordet skickas på stdin med det särskilda värdet <stdin>: argv
    syns i ps för varje användare på maskinen.
    """
    argv = [BINARY, "add", "--with-library", config.with_library]
    if config.username:
        argv += ["--username", config.username]
        if config.password:
            argv += ["--password", "<stdin>"]
    # add får aldrig röra en bok som redan finns: en krock på titel och
    # författare kan vara någon annans post. Uppmätt 2026-08-21: overwrite
    # ersätter den befintliga filen, och ignore skriver bara filnamnet
    # utan bok-id, vilket hade tappat dubblettskyddet. new_record ger
    # alltid ett läsbart id. Medvetna kopplingar görs i matchningsvyn.
    argv += ["--automerge", "new_record"]
    argv += ["--title", title, "--authors", authors]
    if series:
        argv += ["--series", series]
        if series_index:
            argv += ["--series-index", series_index]
    if cover is not None:
        argv += ["--cover", str(cover)]
    argv.append(str(path))
    return argv


def build_add_format_argv(
    config: CalibreConfig, calibre_id: int, path: Path
) -> list[str]:
    """Ersätt filen på en bok som redan finns, i stället för att lägga en till."""
    argv = [BINARY, "add_format", "--with-library", config.with_library]
    if config.username:
        argv += ["--username", config.username]
        if config.password:
            argv += ["--password", "<stdin>"]
    argv += [str(calibre_id), str(path)]
    return argv


def build_metadata_argv(
    config: CalibreConfig, calibre_id: int, falt: dict[str, str]
) -> list[str]:
    """Sätt ett eller flera metadatafält på en bok.

    Verifierat mot en riktig server att flera --field går i samma anrop,
    så en berikning kostar ett anrop per bok och inte ett per fält.
    """
    argv = [BINARY, "set_metadata", "--with-library", config.with_library]
    if config.username:
        argv += ["--username", config.username]
        if config.password:
            argv += ["--password", "<stdin>"]
    for namn, varde in falt.items():
        argv += ["--field", f"{namn}:{varde}"]
    argv.append(str(calibre_id))
    return argv


def build_pubdate_argv(
    config: CalibreConfig, calibre_id: int, pubdate: str
) -> list[str]:
    """Utgivningsdatumet går inte att sätta i add, bara efteråt."""
    return build_metadata_argv(config, calibre_id, {"pubdate": pubdate})


def build_list_argv(config: CalibreConfig) -> list[str]:
    """Ett anrop som bara läser, för att prova uppgifterna."""
    argv = [BINARY, "list", "--with-library", config.with_library, "--limit", "1"]
    if config.username:
        argv += ["--username", config.username]
        if config.password:
            argv += ["--password", "<stdin>"]
    return argv


# Vad titelmönstret får innehålla. Fler fält kostar bara en rad här, men
# varje nytt är ett som kan stå fel i någons sparade mönster.
TITELFALT = ("publikation", "utgåva", "datum", "rubrik", "nummer", "år")
STANDARDTITEL = "{publikation} {utgåva}"


def format_title(
    monster: str,
    *,
    publikation: str,
    utgava: str,
    datum: str = "",
    rubrik: str = "",
    nummer: int | None = None,
) -> str:
    """Bokens titel ur publikationens mönster.

    Ett mönster med okänt fält eller obalanserad klammer får inte fälla en
    nedladdning som lyckats - då används standarden i stället.
    """
    varden = {
        "publikation": publikation,
        "utgåva": utgava,
        "datum": datum,
        "rubrik": rubrik,
        "nummer": nummer if nummer is not None else "",
        "år": datum[:4] if len(datum) >= 4 else "",
    }
    for kandidat in (monster.strip(), STANDARDTITEL):
        if not kandidat:
            continue
        try:
            falt = [namn for _, namn, _, _ in Formatter().parse(kandidat) if namn]
            # Alla fält mönstret använder måste ha värde. Annars ger
            # "{år}-{nummer} - {rubrik}" strängen "2024- -" den gång rubriken
            # saknas, vilket är sämre än standardtiteln.
            if any(not str(varden.get(namn, "")).strip() for namn in falt):
                continue
            titel = kandidat.format(**varden).strip()
        except (KeyError, IndexError, ValueError):
            logger.warning("Titelmönstret %r går inte att använda", kandidat)
            continue
        if titel:
            return titel
    return f"{publikation} {utgava}".strip()


def series_index(publication_date: str | None, nummer: int | None = None) -> str:
    """Serienumret som Calibre ska sortera serien på.

    Med årets löpnummer blir det 2024.2, alltså det utgåvan faktiskt
    kallas. Utan nummer får datumet duga: 20240402 sorterar rätt men
    läser sig som ett löpnummer på tio miljoner.
    """
    if not publication_date:
        return ""
    ar = publication_date[:4]
    if nummer is not None and ar.isdigit():
        return f"{ar}.{nummer}"
    siffror = publication_date.replace("-", "")[:8]
    return siffror if siffror.isdigit() and len(siffror) == 8 else ""


def parse_added_id(utdata: str) -> int | None:
    """Bok-id:t ur calibredb:s utskrift, eller None om det inte gick att läsa."""
    traff = ID_MONSTER.search(utdata)
    if traff is None:
        for rad in reversed(utdata.strip().splitlines()):
            traff = ID_RESERV.search(rad.strip())
            if traff:
                break
    if not traff:
        return None
    forsta = traff.group(1).split(",")[0].strip()
    return int(forsta) if forsta.isdigit() else None


def run(config: CalibreConfig, argv: list[str]) -> str:
    """Kör calibredb och returnera dess utskrift."""
    try:
        klar = subprocess.run(
            argv,
            # Alltid en pipe: med --username men utan lösenord frågar
            # calibredb efter ett, och ärvd stdin hade hängt arbetaren
            # tills timeouten löste ut.
            input=f"{config.password}\n" if config.password else "",
            capture_output=True,
            text=True,
            env={**os.environ, **SPRAKMILJO},
            timeout=TIMEOUT,
            check=False,
        )
    except FileNotFoundError as error:
        raise CalibreError(
            f"{BINARY} finns inte i PATH - calibre måste vara installerat"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise CalibreError(f"{BINARY} svarade inte inom {TIMEOUT} sekunder") from error
    if klar.returncode != 0:
        besked = (klar.stderr or klar.stdout or "").strip().splitlines()
        raise CalibreError(besked[-1] if besked else f"{BINARY} gav fel utan besked")
    return klar.stdout or ""


def replace_format(config: CalibreConfig, path: Path, calibre_id: int) -> int:
    """Byt ut filen på en bok som redan finns.

    Ingen metadata skickas: boken finns och dess uppgifter ska stå kvar.
    Att ta titel och författare här hade bara varit argument som ignoreras.
    """
    run(config, build_add_format_argv(config, calibre_id, path))
    return calibre_id


def push(
    config: CalibreConfig,
    path: Path,
    *,
    title: str,
    authors: str,
    series: str = "",
    series_index: str = "",
    pubdate: str = "",
    cover: Path | None = None,
    calibre_id: int | None = None,
) -> int | None:
    """Lägg in utgåvan i Calibre och returnera dess bok-id.

    Finns id:t redan ersätts bara filen: en tvingad omhämtning ska
    uppdatera boken, inte skapa en till.
    """
    if calibre_id is not None:
        return replace_format(config, path, calibre_id)

    utdata = run(config, build_add_argv(
        config, path, title=title, authors=authors,
        series=series, series_index=series_index, cover=cover,
    ))
    nytt_id = parse_added_id(utdata)
    if nytt_id is None:
        # Boken kan mycket väl ha lagts in ändå. Utan id går dubblettskyddet
        # förlorat, så det ska synas på jobbet.
        raise CalibreError(
            "Kunde inte läsa bok-id ur calibredb:s svar: " + utdata.strip()[:200]
        )
    if pubdate:
        try:
            run(config, build_pubdate_argv(config, nytt_id, pubdate))
        except CalibreError as error:
            # Boken ligger inne som nytt_id. Kastar vi här går id:t
            # förlorat och nästa tvingade omhämtning skapar en dubblett.
            logger.warning("Kunde inte sätta utgivningsdatum: %s", error)
    return nytt_id
