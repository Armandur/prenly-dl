import json

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


def test_import_conf_skapar_publikation(db):
    with db() as session:
        publication = import_conf(session, CONF)
        assert publication.title_id == 4019
        assert publication.site == "https://etidning.example.se"
        assert publication.name == "Exempeltidningen"
        assert publication.watch_limit == 3
        assert publication.slug == "exempeltidningen"
        assert [issue.uid for issue in publication.issues] == ["111"]


def test_import_conf_uppdaterar_befintlig(db):
    with db() as session:
        first = import_conf(session, CONF)
        first_id = first.id
    updated = {**CONF, "credentials": {**CONF["credentials"], "auth": "ny-token"}}
    with db() as session:
        second = import_conf(session, updated)
        assert second.id == first_id
        assert second.auth == "ny-token"
