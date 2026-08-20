"""Biblioteksvy: PDF:erna som faktiskt ligger på disk."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import output_root

# Nedladdningen arbetar i tempkataloger med det här prefixet inne i
# utdatakatalogen. De innehåller enstaka sidor och hör inte hemma i
# biblioteket.
TEMP_PREFIX = "prenly-"


@dataclass
class LibraryFile:
    name: str
    folder: str
    rel_path: str
    size: int
    mtime: datetime


def scan_library(root: Path | None = None) -> list[LibraryFile]:
    """Alla PDF:er under utdatakatalogen, utan pågående nedladdningar."""
    base = Path(root) if root else output_root()
    if not base.is_dir():
        return []

    files: list[LibraryFile] = []
    for path in base.rglob("*.pdf"):
        relative = path.relative_to(base)
        if any(part.startswith(TEMP_PREFIX) for part in relative.parts[:-1]):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(
            LibraryFile(
                name=path.name,
                folder=str(relative.parent) if str(relative.parent) != "." else "",
                rel_path=str(relative),
                size=stat.st_size,
                mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(
                    tzinfo=None
                ),
            )
        )
    return sorted(files, key=lambda f: (f.folder, f.name))


def group_by_folder(files: list[LibraryFile]) -> list[tuple[str, list[LibraryFile]]]:
    grupper: dict[str, list[LibraryFile]] = {}
    for item in files:
        grupper.setdefault(item.folder, []).append(item)
    return sorted(grupper.items(), key=lambda kv: kv[0])


def folder_usage(folder: Path) -> tuple[int, int]:
    """Antal PDF:er och deras sammanlagda storlek i en katalog.

    Pågående nedladdningar räknas inte: deras sidfiler ligger i en
    tempkatalog och är inte färdiga utgåvor.
    """
    if not folder.is_dir():
        return 0, 0
    antal = 0
    storlek = 0
    for path in folder.rglob("*.pdf"):
        if any(
            part.startswith(TEMP_PREFIX)
            for part in path.relative_to(folder).parts[:-1]
        ):
            continue
        try:
            storlek += path.stat().st_size
        except OSError:
            continue
        antal += 1
    return antal, storlek


def resolve_file(rel_path: str, root: Path | None = None) -> Path | None:
    """Slå upp en fil i biblioteket, med skydd mot vägar ut ur katalogen."""
    base = (Path(root) if root else output_root()).resolve()
    try:
        target = (base / rel_path).resolve()
    except OSError:
        return None
    if not target.is_relative_to(base) or not target.is_file():
        return None
    if target.suffix.lower() != ".pdf":
        return None
    return target
