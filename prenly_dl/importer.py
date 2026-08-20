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
                # Bara objekt som faktiskt är konfigurationer räknas -
                # utskriften innehåller även andra objekt.
                if (
                    isinstance(kandidat, dict)
                    and isinstance(kandidat.get("credentials"), dict)
                    and isinstance(kandidat.get("publication"), dict)
                    and kandidat["publication"].get("title") is not None
                ):
                    funna.append(kandidat)
                start = None

    # Samma publikation kan förekomma flera gånger i en klistrad text.
    unika: dict[tuple, dict] = {}
    for conf in funna:
        nyckel = (
            str(conf["credentials"].get("site", "")).rstrip("/"),
            str(conf["publication"]["title"]),
        )
        unika[nyckel] = conf
    return list(unika.values())


def load_conf(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(strip_json_comments(text))


def import_conf(session: Session, conf: dict, source_name: str = "") -> Publication:
    """Skapa eller uppdatera en publikation utifrån en gammal conf-dict."""
    credentials = conf.get("credentials", {})
    publication_conf = conf.get("publication", {})
    site = str(credentials.get("site", "")).rstrip("/")
    title_id = int(publication_conf["title"])

    name = conf.get("prefix", "").strip(" -") or conf.get("folder", "") or source_name
    name = name or f"Publikation {title_id}"

    existing = repo.find_publication(session, site, title_id)
    publication = existing or Publication(title_id=title_id, site=site)
    publication.name = name
    publication.textalk_auth = credentials.get("textalk-auth", "")
    publication.auth = credentials.get("auth", "")
    publication.cdn = credentials.get("cdn", "") or ""
    publication.prefix = conf.get("prefix", "")
    publication.folder = conf.get("folder", "")
    publication.watch_limit = int(publication_conf.get("limit", 1) or 1)
    if existing is None:
        publication.slug = repo.unique_slug(session, name)
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
