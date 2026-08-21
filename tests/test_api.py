import pytest

from prenly_dl.api import Credentials, get_page_checksums
from prenly_dl.errors import (
    PrenlyApiError,
    PrenlyAuthError,
    PrenlySubscriptionError,
    raise_for_rpc_error,
)


def test_sidor_sorteras_numeriskt_inte_lexikalt():
    """Utan position_number får sidnumret bestämma ordningen."""
    data = {
        "replica_spreads": [
            {"pages": [{"page_no": 10, "media": [{"checksum": "tio"}]}]},
            {"pages": [{"page_no": 2, "media": [{"checksum": "tva"}]}]},
            {"pages": [{"page_no": 1, "media": [{"checksum": "ett"}]}]},
        ]
    }
    assert list(get_page_checksums(data).values()) == ["ett", "tva", "tio"]
    assert list(get_page_checksums(data).keys()) == ["00001-00", "00002-00", "00010-00"]


def test_delar_med_samma_sidnummer_skriver_inte_over_varandra():
    """Uppmätt: Expressen har tre delar med sidorna 1-32, 1-12 och 1-20.

    Med sidnumret som nyckel försvann hälften av sidorna och förstasidan
    blev sista delens.
    """
    data = {
        "replica_spreads": [
            {"part": "Del 1", "position_number": 1,
             "pages": [{"page_no": 1, "media": [{"checksum": "a1"}]}]},
            {"part": "Del 1", "position_number": 2,
             "pages": [{"page_no": 2, "media": [{"checksum": "a2"}]}]},
            {"part": "Del 2", "position_number": 3,
             "pages": [{"page_no": 1, "media": [{"checksum": "b1"}]}]},
        ]
    }
    sidor = get_page_checksums(data)
    assert len(sidor) == 3, "ingen sida får skrivas över"
    # Del 1 först, i sin ordning, sedan Del 2.
    assert list(sidor.values()) == ["a1", "a2", "b1"]


def test_rpc_fel_oversatts_till_ratt_undantag():
    with pytest.raises(PrenlyAuthError):
        raise_for_rpc_error({"code": -32001, "message": "Inte autentiserad."})
    with pytest.raises(PrenlySubscriptionError):
        raise_for_rpc_error({"code": -32006, "message": "Ingår inte."})
    with pytest.raises(PrenlyApiError):
        raise_for_rpc_error({"code": -1, "message": "Annat"})


def test_credentials_normaliserar_slutande_snedstreck():
    creds = Credentials(site="https://a.se/", textalk_auth="k", auth="t", cdn="")
    assert creds.site == "https://a.se"
    assert creds.cdn == "https://mediacdn.prenly.com"


def test_bilagor_laggs_efter_huvuddelen():
    """Uppmätt: DN 2026-08-21 har Del B och Del C som egna utgåvor.

    De ligger i insert_issues med eget title_id och uid, och saknades
    helt i den nedladdade PDF:en, som bara hade huvuddelens 24 sidor.
    """
    from prenly_dl.api import collect_pages
    import prenly_dl.api as api

    huvud = {
        "replica_spreads": [
            {"position_number": 1, "pages": [{"page_no": 1, "media": [{"checksum": "a1"}]}]},
        ],
        "insert_issues": [
            {"title_id": 2359, "issue_uid": "2430600"},
            {"title_id": 2361, "issue_uid": "2431444"},
        ],
    }
    bilagor = {
        ("2359", "2430600"): {
            "replica_spreads": [
                {"position_number": 1, "pages": [{"page_no": 1, "media": [{"checksum": "b1"}]}]},
            ]
        },
        ("2361", "2431444"): {
            "replica_spreads": [
                {"position_number": 1, "pages": [{"page_no": 1, "media": [{"checksum": "c1"}]}]},
            ]
        },
    }

    def _hamta(session, creds, title_id, uid):
        return bilagor[(str(title_id), str(uid))]

    original = api.get_issue
    api.get_issue = _hamta
    try:
        sidor = collect_pages(None, None, huvud, 2357)
    finally:
        api.get_issue = original

    assert [s.checksum for s in sidor] == ["a1", "b1", "c1"]
    # Bilagornas media bor under deras egen titel.
    assert [s.title_id for s in sidor] == [2357, 2359, 2361]
    # Nyckeln håller huvuddelen först.
    assert [s.nyckel.split("-")[0] for s in sidor] == ["0", "1", "2"]


def test_trasig_bilaga_faller_inte_hela_utgavan():
    from prenly_dl.api import collect_pages
    import prenly_dl.api as api
    from prenly_dl.errors import PrenlyError

    huvud = {
        "replica_spreads": [
            {"position_number": 1, "pages": [{"page_no": 1, "media": [{"checksum": "a1"}]}]},
        ],
        "insert_issues": [{"title_id": 2359, "issue_uid": "trasig"}],
    }

    def _spricker(*args, **kwargs):
        raise PrenlyError("bilagan gick inte att läsa")

    original = api.get_issue
    api.get_issue = _spricker
    try:
        sidor = collect_pages(None, None, huvud, 2357)
    finally:
        api.get_issue = original

    # Hellre huvuddelen utan bilaga än ingen utgåva alls.
    assert [s.checksum for s in sidor] == ["a1"]
