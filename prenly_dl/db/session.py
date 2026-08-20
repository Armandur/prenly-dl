"""Sessionsfabrik och schemainitiering för SQLite-databasen."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import db_path
from .models import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _make_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        # WAL så webbtjänsten kan läsa medan schemaläggaren skriver.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


# SQLite-typer för de kolumntyper modellerna använder.
_SQLITE_TYPER = {
    "INTEGER": "INTEGER",
    "VARCHAR": "VARCHAR",
    "TEXT": "TEXT",
    "BOOLEAN": "BOOLEAN",
    "DATETIME": "DATETIME",
}


def _add_missing_columns(engine: Engine) -> list[str]:
    """Lägg till kolumner som finns i modellerna men saknas i databasen.

    create_all() skapar bara tabeller som saknas helt, så ett nytt fält på
    en befintlig tabell skulle annars kräva att databasen raderades. Räcker
    för nya nullbara kolumner med default - kolumnbyten och borttagningar
    hanteras inte, och skulle behöva ett riktigt migrationsverktyg.
    """
    tillagda: list[str] = []
    inspector = inspect(engine)
    befintliga_tabeller = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in befintliga_tabeller:
                continue
            har = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in har:
                    continue
                typnamn = column.type.compile(dialect=engine.dialect)
                sats = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {typnamn}"
                default = column.default
                if default is not None and not callable(getattr(default, "arg", None)):
                    varde = default.arg
                    if isinstance(varde, bool):
                        sats += f" DEFAULT {1 if varde else 0}"
                    elif isinstance(varde, int):
                        sats += f" DEFAULT {varde}"
                    elif isinstance(varde, str):
                        sats += f" DEFAULT '{varde}'"
                connection.execute(text(sats))
                tillagda.append(f"{table.name}.{column.name}")
    return tillagda


GAMMAL_TABELL = "publications_fore_sajter"


def _har_gammal_form(engine: Engine) -> bool:
    """Sant när uppgifterna fortfarande ligger på varje publikation."""
    inspector = inspect(engine)
    if "publications" not in inspector.get_table_names():
        return False
    return "site" in {col["name"] for col in inspector.get_columns("publications")}


def _dop_om_gammal_tabell(engine: Engine) -> None:
    """Flytta undan den gamla tabellen så create_all kan skapa den nya.

    DROP COLUMN går inte: den gamla tabellen har en UNIQUE-constraint på
    (site, title_id) vars autoindex blockerar borttagningen. Att bygga om
    tabellen är SQLites egna mönster för schemaändringar.

    legacy_alter_table gör att issues FK fortsätter peka på "publications"
    i stället för att följa med omdöpningen.
    """
    with engine.connect() as connection:
        # issues har ON DELETE CASCADE mot publications. Utan det här
        # tar borttagningen av den gamla tabellen alla utgåvor med sig.
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(text("PRAGMA legacy_alter_table=ON"))
        connection.execute(text(f"ALTER TABLE publications RENAME TO {GAMMAL_TABELL}"))
        connection.execute(text("PRAGMA legacy_alter_table=OFF"))
        connection.commit()


def _flytta_uppgifter_till_sajter(engine: Engine) -> int:
    """Flytta inloggningsuppgifter från publications till sites.

    Uppgifterna låg tidigare på varje publikation, så samma token fanns i
    en kopia per titel. Backfillen skapar en sites-rad per unik URL och
    kopierar publikationerna med samma id, så utgåvorna hänger kvar.

    Returnerar antalet skapade sajter.
    """
    with engine.connect() as connection:
        # Kaskaden mot issues måste vara av under hela flytten.
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        rader = connection.execute(
            text(f"SELECT * FROM {GAMMAL_TABELL}")
        ).mappings().all()

        sajter: dict[str, int] = {}
        for rad in rader:
            url = (rad["site"] or "").rstrip("/")
            if url in sajter:
                continue
            resultat = connection.execute(
                text(
                    "INSERT INTO sites (url, name, textalk_auth, auth, cdn,"
                    " auth_status, auth_message, auth_checked_at, created_at, updated_at)"
                    " VALUES (:url, '', :textalk_auth, :auth, :cdn,"
                    " :auth_status, :auth_message, :auth_checked_at,"
                    " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "url": url,
                    "textalk_auth": rad["textalk_auth"] or "",
                    "auth": rad["auth"] or "",
                    "cdn": rad["cdn"] or "",
                    "auth_status": rad["auth_status"] or "unknown",
                    "auth_message": rad["auth_message"],
                    "auth_checked_at": rad["auth_checked_at"],
                },
            )
            sajter[url] = resultat.lastrowid

        for rad in rader:
            connection.execute(
                text(
                    "INSERT INTO publications (id, site_id, title_id, name, slug,"
                    " readable, enabled, prefix, folder, watch_limit,"
                    " catalogue_offset, catalogue_complete, cover_checksum,"
                    " created_at, updated_at)"
                    " VALUES (:id, :site_id, :title_id, :name, :slug,"
                    " :readable, 1, :prefix, :folder, :watch_limit,"
                    " :catalogue_offset, :catalogue_complete, :cover_checksum,"
                    " :created_at, :updated_at)"
                ),
                {
                    "id": rad["id"],
                    "site_id": sajter[(rad["site"] or "").rstrip("/")],
                    "title_id": rad["title_id"],
                    "name": rad["name"],
                    "slug": rad["slug"],
                    # Publikationer som redan fanns var per definition
                    # bevakade; läsrätten härleds ur den gamla statusen.
                    "readable": 0 if rad["auth_status"] == "subscription_error" else 1,
                    "prefix": rad["prefix"] or "",
                    "folder": rad["folder"] or "",
                    "watch_limit": rad["watch_limit"] or 1,
                    "catalogue_offset": rad["catalogue_offset"] or 0,
                    "catalogue_complete": rad["catalogue_complete"] or 0,
                    "cover_checksum": rad["cover_checksum"],
                    # Gamla rader kan sakna tidsstämplar.
                    "created_at": rad["created_at"] or datetime.now(timezone.utc),
                    "updated_at": rad["updated_at"] or datetime.now(timezone.utc),
                },
            )

        connection.execute(text(f"DROP TABLE {GAMMAL_TABELL}"))
        connection.commit()

        # Kontrollera att inget tappats innan vi lämnar migreringen.
        kvar = connection.execute(
            text("SELECT count(*) FROM publications")
        ).scalar_one()
        if kvar != len(rader):
            raise RuntimeError(
                f"Migreringen tappade publikationer: {len(rader)} före, {kvar} efter"
            )

    return len(sajter)


def make_session_factory(path: Path | None = None) -> sessionmaker[Session]:
    """Skapa (eller återanvänd) sessionsfabriken och se till att schemat finns."""
    global _engine, _session_factory
    target = Path(path) if path else db_path()
    if _session_factory is None or _engine is None or _engine.url.database != str(target):
        _engine = _make_engine(target)
        # Den gamla tabellen flyttas undan innan create_all, så den nya
        # skapas med rätt schema och datan kopieras dit efteråt.
        gammal_form = _har_gammal_form(_engine)
        if gammal_form:
            _dop_om_gammal_tabell(_engine)
        Base.metadata.create_all(_engine)
        tillagda = _add_missing_columns(_engine)
        if tillagda:
            logger.info("Lade till saknade kolumner: %s", ", ".join(tillagda))
        if gammal_form:
            flyttade = _flytta_uppgifter_till_sajter(_engine)
            logger.info("Flyttade inloggningsuppgifter till %d sajter", flyttade)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        logger.info("Databas: %s", target)
    return _session_factory


def init_db(path: Path | None = None) -> None:
    make_session_factory(path)


@contextmanager
def get_session(path: Path | None = None) -> Iterator[Session]:
    """Sessionskontext som committar vid lyckat utfall och rullar tillbaka vid fel."""
    factory = make_session_factory(path)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
