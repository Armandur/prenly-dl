import json

from prenly_dl import cli
from prenly_dl.db import repository as repo
from prenly_dl.db.models import AuthStatus


def test_restore_aterstaller_sajter_och_bevakning(db, temp_env, monkeypatch):
    data = [
        {
            "credentials": {"site": "https://a.se", "auth": "a"},
            "_bevakade": [259],
        },
        {
            "credentials": {"site": "https://b.se", "auth": "b"},
            "_bevakade": [],
        },
    ]
    path = temp_env / "mina-sajter.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    def _verifiera(session, site):
        repo.upsert_title_access(
            session, site, 259, "Expressen", readable=site.url == "https://b.se"
        )
        repo.set_site_status(session, site, AuthStatus.OK)
        return AuthStatus.OK

    monkeypatch.setattr(cli, "verify_site", _verifiera)

    assert cli.main(["restore", str(path)]) == 0
    with db() as session:
        assert [site.url for site in repo.list_sites(session)] == [
            "https://a.se",
            "https://b.se",
        ]
        publications = repo.list_publications(session)
        assert len(publications) == 1
        assert publications[0].title_id == 259
        assert publications[0].enabled is True
