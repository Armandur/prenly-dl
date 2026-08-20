import json

from prenly_dl.db import repository as repo

from prenly_dl.importer import import_conf, strip_json_comments


def test_strip_json_comments_behaller_hash_i_strang():
    text = '{"url": "https://example.se/a#b", "n": 1} # kommentar'
    assert strip_json_comments(text).strip() == '{"url": "https://example.se/a#b", "n": 1}'


def test_strip_json_comments_klarar_escapad_citattecken():
    text = '{"a": "x\\"# inte kommentar"} # kommentar'
    assert json.loads(strip_json_comments(text)) == {"a": 'x"# inte kommentar'}


CONF = {
    "credentials": {
        "site": "https://etidning.example.se/",
        "textalk-auth": "klientnyckel",
        "auth": "sessionstoken",
        "cdn": "https://cdn.example.se",
    },
    "publication": {"title": 4019, "limit": 3, "uids": ["111"]},
    "prefix": "Exempeltidningen - ",
    "folder": "Exempeltidningen",
}


def test_import_conf_skapar_publikation_under_sajten(db):
    with db() as session:
        publication = import_conf(session, CONF)
        assert publication.title_id == 4019
        assert publication.name == "Exempeltidningen"
        assert publication.watch_limit == 3
        assert publication.slug == "exempeltidningen"
        assert publication.enabled is True
        assert [issue.uid for issue in publication.title.issues] == ["111"]

        # Uppgifterna ligger på sajten, inte på publikationen.
        assert publication.accesses[0].site.url == "https://etidning.example.se"
        assert publication.accesses[0].site.auth == "sessionstoken"
        assert publication.accesses[0].site.cdn == "https://cdn.example.se"
        assert not hasattr(publication, "auth")


def test_import_conf_uppdaterar_befintlig(db):
    with db() as session:
        first = import_conf(session, CONF)
        first_id = first.id
    updated = {**CONF, "credentials": {**CONF["credentials"], "auth": "ny-token"}}
    with db() as session:
        second = import_conf(session, updated)
        assert second.id == first_id
        assert repo.list_sites(session)[0].auth == "ny-token"


def test_tva_titlar_pa_samma_sajt_delar_uppgifter(db):
    """Uppgifterna ska skrivas en gång, inte en kopia per titel."""
    andra = {
        **CONF,
        "publication": {"title": 4020, "limit": 1},
        "prefix": "Bilagan - ",
        "folder": "Bilagan",
    }
    with db() as session:
        forsta = import_conf(session, CONF)
        bilaga = import_conf(session, andra)
        assert forsta.accesses[0].site_id == bilaga.accesses[0].site_id
        assert len(repo.list_sites(session)) == 1

    # En förnyelse via den ena gäller båda.
    fornyad = {**CONF, "credentials": {**CONF["credentials"], "auth": "farsk"}}
    with db() as session:
        import_conf(session, fornyad)
        for publication in repo.list_publications(session):
            assert publication.accesses[0].site.auth == "farsk"
