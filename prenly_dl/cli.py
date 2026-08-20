"""Kommandoradsgränssnitt.

`prenly-dl serve` startar webbui med schemaläggare. `prenly-dl download`
kör en engångsnedladdning från en JSON-konfiguration, som det gamla
skriptet, utan att röra databasen.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .api import Credentials, build_session, get_catalogue_issues, get_context_token
from .config import output_root
from .db import repository as repo
from .db.session import get_session, init_db
from .downloader import download_issue
from .errors import PrenlyError
from .importer import import_directory, load_conf
from .services import execute_job, poll_publication, verify_site

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "prenly_dl.web.app:app",
        host=args.host,
        port=args.port,
        log_level="debug" if args.verbose else "info",
    )
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    """Engångsnedladdning från en JSON-konfiguration."""
    try:
        conf = load_conf(Path(args.json))
    except OSError as error:
        print(f"Kunde inte läsa {args.json}: {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"Trasig JSON i {args.json}: {error}", file=sys.stderr)
        return 1

    credentials_conf = conf.get("credentials", {})
    creds = Credentials(
        site=credentials_conf.get("site", ""),
        textalk_auth=credentials_conf.get("textalk-auth", ""),
        auth=credentials_conf.get("auth", ""),
        cdn=credentials_conf.get("cdn", "") or "",
    )
    publication = conf.get("publication", {})
    title_id = int(publication["title"])
    prefix = conf.get("prefix", "")
    force = args.force or str(conf.get("force", "")).lower() == "true"

    output_dir = Path(args.output) if args.output else output_root()
    if conf.get("folder"):
        output_dir = output_dir / conf["folder"]

    session = build_session()
    uids = [str(uid) for uid in publication.get("uids", []) or []]
    if not uids:
        limit = int(publication.get("limit", 1) or 1)
        try:
            token = get_context_token(session, creds)
            entries = get_catalogue_issues(session, creds, token, title_id, limit)
        except PrenlyError as error:
            print(f"Kunde inte hämta katalogen: {error}", file=sys.stderr)
            return 1
        uids = [str(entry["uid"]) for entry in entries]

    if not uids:
        print("Inga utgåvor att ladda ner", file=sys.stderr)
        return 1

    failures = 0
    for uid in uids:
        try:
            result = download_issue(
                session, creds, title_id, uid, output_dir, prefix=prefix, force=force
            )
        except PrenlyError as error:
            # En trasig utgåva ska inte stoppa resten av listan.
            print(f"Utgåva {uid} misslyckades: {error}", file=sys.stderr)
            failures += 1
            continue
        if result.skipped:
            print(f"{result.path.name} fanns redan, använd --force för att hämta om")
        else:
            print(f"Laddade ner {result.path}")
    return 1 if failures else 0


def cmd_import(args: argparse.Namespace) -> int:
    init_db()
    with get_session() as session:
        imported = import_directory(session, Path(args.directory))
    print(f"Importerade {len(imported)} konfigurationsfiler")
    for publication in imported:
        print(f"  {publication.slug}: {publication.name} (titel {publication.title_id})")
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    """Kör katalogkontroll och eventuella nedladdningar direkt."""
    init_db()
    with get_session() as session:
        publications = repo.list_publications(session, enabled_only=True)
        if args.publication:
            publications = [p for p in publications if p.slug == args.publication]
        for publication in publications:
            try:
                result = poll_publication(session, publication, enqueue=not args.check_only)
            except PrenlyError as error:
                print(f"{publication.name}: {error}", file=sys.stderr)
                continue
            print(
                f"{publication.name}: {result['found']} i katalogen, "
                f"{result['new']} nya, {result['queued']} köade"
            )
        if args.check_only:
            return 0
        while True:
            job = repo.claim_next_job(session)
            if job is None:
                break
            session.commit()
            execute_job(session, job)
            session.commit()
            print(job.error or job.message or f"Jobb {job.id}: {job.status}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Testa uppgifterna en gång per sajt, inte en gång per titel."""
    init_db()
    with get_session() as session:
        for site in repo.list_sites(session):
            status = verify_site(session, site)
            note = f" - {site.auth_message}" if site.auth_message else ""
            print(f"{site.url}: {status.value}{note}")
            for publication in repo.list_publications(session, site_id=site.id):
                lasrätt = "läsbar" if publication.readable else "ingen läsrätt"
                aktiv = "aktiv" if publication.enabled else "inaktiv"
                print(f"  {publication.title_id:>6} {publication.name} ({lasrätt}, {aktiv})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prenly-dl", description="Hämta tidningar från Prenly")
    parser.add_argument("-v", "--verbose", action="store_true", help="Utförlig loggning")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="Starta webbui och schemaläggare")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    download = subparsers.add_parser("download", help="Engångsnedladdning från JSON-konfig")
    download.add_argument("--json", required=True, help="Sökväg till konfigurationsfilen")
    download.add_argument("--output", help="Utdatakatalog (annars PRENLY_OUTPUT)")
    download.add_argument("--force", action="store_true", help="Hämta om även om filen finns")
    download.set_defaults(func=cmd_download)

    importer = subparsers.add_parser("import", help="Importera JSON-konfigurationer till databasen")
    importer.add_argument("directory", help="Katalog med *.json")
    importer.set_defaults(func=cmd_import)

    poll = subparsers.add_parser("poll", help="Kör katalogkontroll och nedladdningar nu")
    poll.add_argument("--publication", help="Begränsa till en publikation (slug)")
    poll.add_argument("--check-only", action="store_true", help="Köa inte nedladdningar")
    poll.set_defaults(func=cmd_poll)

    verify = subparsers.add_parser("verify", help="Testa alla sparade inloggningsuppgifter")
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Bakåtkompatibelt: "prenly-dl --json=conf.json [--force]" utan underkommando.
    if argv and argv[0].startswith("--json"):
        argv = ["download", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)
