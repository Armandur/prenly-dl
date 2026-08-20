"""Säker kataloglistning inom tjänstens monterade och tillåtna rötter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePath

from ..config import db_path, output_root


@dataclass(frozen=True)
class BrowseRoot:
    id: str
    path: Path


def browse_roots() -> list[BrowseRoot]:
    candidates = (("output", output_root()), ("data", db_path().parent))
    roots: list[BrowseRoot] = []
    seen: set[Path] = set()
    for root_id, path in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            roots.append(BrowseRoot(root_id, resolved))
            seen.add(resolved)
    return roots


def _root(root_id: str) -> BrowseRoot | None:
    return next((root for root in browse_roots() if root.id == root_id), None)


def resolve_relative_directory(root_id: str, relative: str) -> Path | None:
    root = _root(root_id)
    pure = PurePath(relative)
    if root is None or pure.is_absolute() or ".." in pure.parts:
        return None
    try:
        target = (root.path / relative).resolve()
    except OSError:
        return None
    if not target.is_relative_to(root.path) or not target.is_dir():
        return None
    return target


def resolve_allowed_directory(path: str) -> Path | None:
    try:
        target = Path(path).expanduser().resolve()
    except OSError:
        return None
    if not target.is_dir():
        return None
    if not any(target.is_relative_to(root.path) for root in browse_roots()):
        return None
    return target


def directory_payload(root_id: str, relative: str) -> dict | None:
    root = _root(root_id)
    current = resolve_relative_directory(root_id, relative)
    if root is None or current is None:
        return None
    directories = []
    try:
        entries = sorted(current.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        entries = []
    for entry in entries:
        try:
            resolved = entry.resolve()
            if not entry.is_dir() or not resolved.is_relative_to(root.path):
                continue
        except OSError:
            continue
        directories.append({
            "name": entry.name,
            "path": str(resolved.relative_to(root.path)),
            "writable": os.access(resolved, os.W_OK),
        })
    rel = current.relative_to(root.path)
    return {
        "root": root.id,
        "root_path": str(root.path),
        "path": "" if str(rel) == "." else str(rel),
        "absolute_path": str(current),
        "parent": None if current == root.path else str(rel.parent),
        "writable": os.access(current, os.W_OK),
        "directories": directories,
    }
