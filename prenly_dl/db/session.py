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
        # Skapar tabeller som saknas. Ändras ett befintligt schema behöver
        # databasfilen tas bort - tjänsten är inte i drift och katalogen
        # bygger upp sig själv igen från sajternas titellistor.
        Base.metadata.create_all(_engine)
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
