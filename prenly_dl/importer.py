"""Import av de gamla JSON-konfigfilerna till databasen.

Filerna tillåter `#`-kommentarer, som JSON inte gör, så de strippas först
med hänsyn till strängar (så att `#` i en URL överlever).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from .db import repository as repo
from .db.models import Publication

logger = logging.getLogger(__name__)


def strip_json_comments(text: str) -> str:
    """Ta bort `#`-kommentarer utanför strängliteraler."""
    out: list[str] = []
    in_string = False
    escaped = False
    in_comment = False
    for char in text:
        if in_comment:
            if char == "\n":
                in_comment = False
                out.append(char)
            continue
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            out.append(char)
        elif char == "#":
            in_comment = True
        else:
            out.append(char)
    return "".join(out)


def extract_configs(text: str) -> list[dict]:
    """Plocka ut alla konfigurationer ur en klistrad text.

    Konsolsnutten skriver ett JSON-block per läsbar publikation, med
    rubriker emellan. Den som kopierar hela utskriften får alltså med brus,
    så texten skannas efter balanserade objekt i stället för att tolkas som
    ett enda dokument.
    """
    rensad = strip_json_comments(text)
    funna: list[dict] = []
    djup = 0
    start = None
    in_string = False
    escaped = False

    for index, char in enumerate(rensad):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if djup == 0:
                start = index
            djup += 1
        elif char == "}":
            if djup == 0:
                continue
            djup -= 1
            if djup == 0 and start is not None:
                bit = rensad[start:index + 1]
                try:
                    kandidat = json.loads(bit)
                except ValueError:
                    start = None
                    continue
                # Ett block räknas när det bär inloggningsuppgifter.
                # Vilka publikationer de ger tillgång till frågar vi API:t
                # om, så publication är valfri - äldre konfigurationsfiler
                # har den, konsolsnutten inte.
                if (
                    isinstance(kandidat, dict)
                    and isinstance(kandidat.get("credentials"), dict)
                    and kandidat["credentials"].get("site")
                    and kandidat["credentials"].get("auth")
                ):
                    funna.append(kandidat)
                start = None

    # Samma sajt och titel kan förekomma flera gånger i en klistrad text.
    unika: dict[tuple, dict] = {}
    for conf in funna:
        publication = conf.get("publication") or {}
        nyckel = (
            str(conf["credentials"].get("site", "")).rstrip("/"),
            str(publication.get("title", "")),
        )
        unika[nyckel] = conf
    return list(unika.values())


def load_conf(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(strip_json_comments(text))


def import_conf(session: Session, conf: dict, source_name: str = "") -> Publication:
    """Skapa eller uppdatera en publikation utifrån en conf-dict.

    Uppgifterna sparas på sajten, så samma konfiguration för flera titlar
    skriver dem en gång i stället för en kopia per titel.
    """
    credentials = conf.get("credentials", {})
    publication_conf = conf.get("publication", {})
    site_url = str(credentials.get("site", "")).rstrip("/")
    title_id = int(publication_conf["title"])

    name = conf.get("prefix", "").strip(" -") or conf.get("folder", "") or source_name
    name = name or f"Publikation {title_id}"

    site = repo.upsert_site(session, site_url, credentials)
    publication, _ = repo.upsert_publication(session, site, title_id, name)
    publication.prefix = conf.get("prefix", "") or publication.prefix
    publication.folder = conf.get("folder", "") or publication.folder
    publication.watch_limit = int(publication_conf.get("limit", 1) or 1)
    # Den som pekar ut en publikation vill ha den bevakad.
    publication.enabled = True
    session.add(publication)
    session.flush()

    # Utgåvor som listades explicit i conf-filen sparas som kända utgåvor.
    for uid in publication_conf.get("uids", []) or []:
        repo.upsert_issue(session, publication, {"uid": str(uid), "name": str(uid)})

    return publication


def import_directory(session: Session, directory: Path) -> list[Publication]:
    """Importera alla JSON-filer i en katalog."""
    imported: list[Publication] = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            conf = load_conf(path)
            publication = import_conf(session, conf, source_name=path.stem)
        except (OSError, ValueError, KeyError) as error:
            logger.warning("Hoppar över %s: %s", path.name, error)
            continue
        imported.append(publication)
        logger.info("Importerade %s som %s", path.name, publication.name)
    return imported
