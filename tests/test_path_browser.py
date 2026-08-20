from pathlib import Path

import pytest
from fastapi.testclient import TestClient



@pytest.fixture
def client(temp_env, monkeypatch):
    import prenly_dl.web.app as app_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
    with TestClient(app_module.create_app()) as test_client:
        yield test_client


def test_katalogendpoint_navigerar_ned_och_upp(client, temp_env):
    child = temp_env / "output" / "underkatalog"
    child.mkdir()

    root = client.get("/settings/directories", params={"root": "output"})
    assert root.status_code == 200
    assert root.json()["directories"][0]["name"] == "underkatalog"
    assert root.json()["directories"][0]["writable"] is True

    below = client.get(
        "/settings/directories", params={"root": "output", "path": "underkatalog"}
    )
    assert below.status_code == 200
    assert below.json()["absolute_path"] == str(child)
    assert below.json()["parent"] == "."
    assert below.json()["directories"] == []


def test_katalogendpoint_avvisar_traversal_och_absolut_sokvag(client, temp_env):
    for path in ("..", "../hemligt", str(temp_env)):
        response = client.get(
            "/settings/directories", params={"root": "output", "path": path}
        )
        assert response.status_code == 403


def test_katalogendpoint_avvisar_symlink_ut_ur_roten(client, temp_env):
    outside = temp_env / "utanfor"
    outside.mkdir()
    (temp_env / "output" / "lank").symlink_to(outside, target_is_directory=True)

    response = client.get(
        "/settings/directories", params={"root": "output", "path": "lank"}
    )
    assert response.status_code == 403
    listing = client.get("/settings/directories", params={"root": "output"}).json()
    assert "lank" not in {entry["name"] for entry in listing["directories"]}


def test_oläsbar_katalog_visas_som_tom(client, temp_env, monkeypatch):
    unreadable = temp_env / "output" / "stangd"
    unreadable.mkdir()
    original = Path.iterdir

    def _iterdir(path):
        if path == unreadable:
            raise PermissionError
        return original(path)

    monkeypatch.setattr(Path, "iterdir", _iterdir)
    response = client.get(
        "/settings/directories", params={"root": "output", "path": "stangd"}
    )
    assert response.status_code == 200
    assert response.json()["directories"] == []


def test_katalogendpoint_markerar_skrivskyddad_katalog(client, temp_env, monkeypatch):
    readonly = temp_env / "output" / "skrivskyddad"
    readonly.mkdir()
    import prenly_dl.web.path_browser as browser

    monkeypatch.setattr(browser.os, "access", lambda path, mode: Path(path) != readonly)
    response = client.get("/settings/directories", params={"root": "output"})
    entry = next(item for item in response.json()["directories"] if item["name"] == readonly.name)
    assert entry["writable"] is False


def test_importendpoint_avvisar_katalog_utanför_vitlistan(client, temp_env):
    outside = temp_env.parent / f"{temp_env.name}-annan-rot"
    outside.mkdir()
    response = client.post(
        "/settings/import", data={"directory": str(outside)}, follow_redirects=True
    )
    assert "inte tillåten" in response.text