import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Isolerad databas och utdatakatalog per test."""
    monkeypatch.setenv("PRENLY_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("PRENLY_OUTPUT", str(tmp_path / "output"))
    monkeypatch.delenv("PRENLY_PASSWORD", raising=False)

    import prenly_dl.db.session as session_module

    session_module._engine = None
    session_module._session_factory = None
    yield tmp_path
    session_module._engine = None
    session_module._session_factory = None


@pytest.fixture
def db(temp_env):
    from prenly_dl.db.session import get_session, init_db

    init_db()
    return get_session
