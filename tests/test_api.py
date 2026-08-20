import pytest

from prenly_dl.api import Credentials, get_page_checksums
from prenly_dl.errors import (
    PrenlyApiError,
    PrenlyAuthError,
    PrenlySubscriptionError,
    raise_for_rpc_error,
)


def test_sidor_sorteras_numeriskt_inte_lexikalt():
    data = {
        "replica_spreads": [
            {"pages": [{"page_no": 10, "media": [{"checksum": "tio"}]}]},
            {"pages": [{"page_no": 2, "media": [{"checksum": "tva"}]}]},
            {"pages": [{"page_no": 1, "media": [{"checksum": "ett"}]}]},
        ]
    }
    assert list(get_page_checksums(data).values()) == ["ett", "tva", "tio"]
    assert list(get_page_checksums(data).keys()) == ["00001", "00002", "00010"]


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
