"""Sessionsfabrik och schemainitiering för SQLite-databasen."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
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


def make_session_factory(path: Path | None = None) -> sessionmaker[Session]:
    """Skapa (eller återanvänd) sessionsfabriken och se till att schemat finns."""
    global _engine, _session_factory
    target = Path(path) if path else db_path()
    if _session_factory is None or _engine is None or _engine.url.database != str(target):
        _engine = _make_engine(target)
        Base.metadata.create_all(_engine)
        _lagg_till_saknade_kolumner(_engine)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        logger.info("Databas: %s", target)
    return _session_factory


def _lagg_till_saknade_kolumner(engine) -> None:
    """Lägg till kolumner som modellerna fått sedan databasen skapades.

    create_all skapar bara tabeller som saknas helt, aldrig kolumner i en
    som redan finns. Efter en uppdatering kraschade därför varje sida som
    rörde ett nytt fält med "no such column", medan tjänsten såg frisk ut
    - healthz svarade, och bara vyerna föll.

    Bara tillägg görs. Att ändra eller ta bort en kolumn kräver mer
    eftertanke än ett automatiskt steg vid uppstart tål.
    """
    with engine.begin() as conn:
        for tabell in Base.metadata.sorted_tables:
            fanns = {
                rad[1]
                for rad in conn.exec_driver_sql(
                    f"PRAGMA table_info({tabell.name})"
                ).fetchall()
            }
            if not fanns:
                continue
            for kolumn in tabell.columns:
                if kolumn.name in fanns:
                    continue
                typ = kolumn.type.compile(engine.dialect)
                default = ""
                if kolumn.default is not None and not callable(
                    getattr(kolumn.default, "arg", None)
                ):
                    varde = kolumn.default.arg
                    if isinstance(varde, bool):
                        default = f" DEFAULT {int(varde)}"
                    elif isinstance(varde, (int, float)):
                        default = f" DEFAULT {varde}"
                    elif isinstance(varde, str):
                        default = " DEFAULT '" + varde.replace("'", "''") + "'"
                conn.exec_driver_sql(
                    f"ALTER TABLE {tabell.name} ADD COLUMN {kolumn.name} {typ}{default}"
                )
                logger.info("La till kolumnen %s.%s", tabell.name, kolumn.name)


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
