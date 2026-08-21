"""Cache för omslagsbilder.

Katalogen svarar med en omslagsbild per utgåva även när prenumerationen
gått ut, så varje publikation kan visa sin senaste titelsida oavsett om
PDF:en hämtats. Bilderna är innehållsadresserade via checksumman, så
filnamnet räcker som cache-nyckel - ingen extra kolumn behövs.
"""

from __future__ import annotations

import logging
import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .api import Credentials, download_media
from .config import db_path
from .errors import PrenlyError

logger = logging.getLogger(__name__)

# Originalen är runt 1200x1500 och en halv megabyte. Korten visar dem i
# 44x60 och detaljsidan i 110x150, så en nedskalning räcker gott.
MAX_WIDTH = 400
LARGE_MAX_WIDTH = 1200
JPEG_QUALITY = 82


def cover_cache_root() -> Path:
    """Cachekatalogen, medvetet utanför utdatakatalogen.

    Biblioteksvyn skannar utdatakatalogen efter PDF:er och ska inte hitta
    cachade omslag där.
    """
    return db_path().parent / "prenly-dl-covers"


def cover_filename(title_id: int, checksum: str, *, stor: bool = False) -> str:
    suffix = "-stor" if stor else ""
    return f"{title_id}-{checksum}{suffix}.jpg"


def cached_cover_path(
    title_id: int, checksum: str, *, stor: bool = False
) -> Path | None:
    """Sökvägen till ett cachat omslag, eller None om det inte finns."""
    path = cover_cache_root() / cover_filename(title_id, checksum, stor=stor)
    return path if path.is_file() else None


def fetch_and_cache_cover(
    session, creds: Credentials, title_id: int, checksum: str, *, stor: bool = False
) -> Path | None:
    """Hämta, skala ner och spara ett omslag. Returnerar sökvägen.

    Returnerar None vid fel i stället för att kasta: ett trasigt omslag
    ska inte fälla en katalogkontroll som i övrigt lyckades.
    """
    if not checksum:
        return None
    existing = cached_cover_path(title_id, checksum, stor=stor)
    if existing:
        return existing

    try:
        response = download_media(
            session, creds, title_id, checksum, accept="image/webp,image/jpeg,image/*"
        )
    except PrenlyError as error:
        logger.warning("Kunde inte hämta omslag %s: %s", checksum, error)
        return None

    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if not content_type.startswith("image/"):
        logger.warning(
            "Omslag %s hade oväntad innehållstyp %r, hoppar över", checksum, content_type
        )
        return None

    try:
        image = Image.open(BytesIO(response.content))
        image.load()
    except (UnidentifiedImageError, OSError) as error:
        logger.warning("Omslag %s gick inte att läsa: %s", checksum, error)
        return None

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    max_width = LARGE_MAX_WIDTH if stor else MAX_WIDTH
    if image.width > max_width:
        height = round(image.height * max_width / image.width)
        image = image.resize((max_width, height), Image.LANCZOS)

    root = cover_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / cover_filename(title_id, checksum, stor=stor)
    # Skriv till en tempfil först: en samtidig läsare ska aldrig se en
    # halvskriven bild.
    tmp_target = root / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        image.save(tmp_target, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        tmp_target.replace(target)
    except OSError as error:
        logger.warning("Kunde inte skriva omslag %s: %s", target, error)
        tmp_target.unlink(missing_ok=True)
        return None
    return target


def calibre_cover_path(book_id: int) -> Path:
    """Cachefil för ett Calibre-omslag.

    Namnet kan inte krocka med Prenlys omslag, som heter <titel-id>-<checksumma>.
    """
    return cover_cache_root() / f"calibre-{int(book_id)}.jpg"


def cached_calibre_cover(book_id: int) -> Path | None:
    path = calibre_cover_path(book_id)
    return path if path.is_file() else None


def store_calibre_cover(book_id: int, data: bytes) -> Path:
    """Spara ett hämtat omslag. En vy med 80 rader ska inte hämta om dem."""
    path = calibre_cover_path(book_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        bild = Image.open(BytesIO(data))
        bild = bild.convert("RGB")
        if bild.width > MAX_WIDTH:
            hojd = round(bild.height * MAX_WIDTH / bild.width)
            bild = bild.resize((MAX_WIDTH, hojd), Image.LANCZOS)
        bild.save(path, "JPEG", quality=JPEG_QUALITY)
    except (UnidentifiedImageError, OSError):
        # Hellre originalet än inget omslag alls.
        path.write_bytes(data)
    return path


def purge_unused_covers(giltiga: set[str]) -> int:
    """Ta bort cachade omslag som ingen utgåva längre pekar på."""
    root = cover_cache_root()
    if not root.is_dir():
        return 0
    borttagna = 0
    for path in root.glob("*.jpg"):
        # Schemaläggaren känner bara till grundfilnamnen. En stor variant
        # ska leva lika länge som miniatyren med samma checksumma.
        grundnamn = (
            f"{path.name.removesuffix('-stor.jpg')}.jpg"
            if path.name.endswith("-stor.jpg")
            else path.name
        )
        if grundnamn not in giltiga:
            path.unlink(missing_ok=True)
            borttagna += 1
    return borttagna
