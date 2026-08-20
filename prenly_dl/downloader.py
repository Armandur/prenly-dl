"""Nedladdning och sammanfogning av en utgåva till en PDF.

Allt arbete sker mot absoluta sökvägar och i en egen tempkatalog per
utgåva, så att flera jobb kan köra samtidigt i samma process utan att äta
varandras sidfiler.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import img2pdf
from pypdf import PdfWriter
from pypdf.errors import PdfReadError

from .api import (
    IMAGE_CONTENT_TYPES,
    Credentials,
    download_media,
    get_issue,
    get_page_checksums,
)
from .config import db_path
from .errors import PrenlyError

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PREVIEW_MAX_AGE = timedelta(days=1)


def safe_filename(name: str) -> str:
    """Gör en sträng säker som filnamn utan att tappa svenska tecken."""
    cleaned = _UNSAFE_CHARS.sub("-", name).strip().strip(".")
    return cleaned or "utgava"


def preview_cache_root() -> Path:
    """Cachekatalogen, medvetet utanför utdatakatalogen."""
    return db_path().parent / "prenly-dl-previews"


def preview_output_dir(issue_id: int) -> Path:
    return preview_cache_root() / str(issue_id)


def purge_old_previews(
    max_age: timedelta = PREVIEW_MAX_AGE,
    now: datetime | None = None,
) -> int:
    """Ta bort förhandsgranskningar vars cachepost är äldre än `max_age`."""
    root = preview_cache_root()
    if not root.is_dir():
        return 0

    cutoff = (now or datetime.now(timezone.utc)).timestamp() - max_age.total_seconds()
    borttagna = 0
    for path in root.iterdir():
        try:
            gammal = path.stat().st_mtime < cutoff
        except OSError:
            continue
        if not gammal:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        borttagna += 1
    return borttagna


class DownloadResult:
    def __init__(self, path: Path, page_count: int, skipped: bool = False) -> None:
        self.path = path
        self.page_count = page_count
        self.skipped = skipped


def download_issue(
    session,
    creds: Credentials,
    title_id: int,
    issue_uid: str,
    output_dir: Path,
    prefix: str = "",
    force: bool = False,
    progress: ProgressCallback | None = None,
) -> DownloadResult:
    """Ladda ner en utgåva och skriv en sammanfogad PDF i `output_dir`.

    Returnerar resultatet med `skipped=True` om filen redan fanns och
    `force` är falskt.
    """
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    issue_data = get_issue(session, creds, title_id, issue_uid)
    stem = safe_filename(f"{prefix}{issue_data.get('name', issue_uid)}")
    target = output_dir / f"{stem}.pdf"

    if target.exists() and not force:
        logger.info("Hoppar över %s, filen finns redan", target.name)
        return DownloadResult(target, 0, skipped=True)

    checksums = get_page_checksums(issue_data)
    if not checksums:
        raise PrenlyError(f"Utgåvan {issue_uid} innehåller inga sidor att ladda ner")

    total = len(checksums)
    workdir = Path(tempfile.mkdtemp(prefix=f"prenly-{issue_uid}-", dir=output_dir))
    try:
        page_files: list[Path] = []
        for index, (page_num, checksum) in enumerate(checksums.items(), start=1):
            response = download_media(session, creds, title_id, checksum)
            content = response.content
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            if content_type in IMAGE_CONTENT_TYPES:
                logger.info(
                    "%s sida %s är %s, konverterar till PDF", stem, page_num, content_type
                )
                content = img2pdf.convert(content)
            page_file = workdir / f"{page_num}.pdf"
            page_file.write_bytes(content)
            page_files.append(page_file)
            if progress:
                progress(index, total)

        merge_pdfs(sorted(page_files), target)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    logger.info("Skrev %s (%d sidor)", target, total)
    return DownloadResult(target, total)


def merge_pdfs(page_files: list[Path], target: Path) -> None:
    """Slå ihop sidfilerna till `target` i den ordning de kommer."""
    if not page_files:
        raise PrenlyError(f"Inga sidor att slå ihop till {target.name}")

    writer = PdfWriter()
    current = page_files[0]
    try:
        for page_file in page_files:
            current = page_file
            writer.append(str(page_file))
    except PdfReadError as error:
        writer.close()
        raise PrenlyError(
            f"Kunde inte läsa sidfilen {current.name}: {error}"
        ) from error

    tmp_target = target.with_suffix(".pdf.part")
    try:
        with open(tmp_target, "wb") as handle:
            writer.write(handle)
    finally:
        writer.close()
    tmp_target.replace(target)
