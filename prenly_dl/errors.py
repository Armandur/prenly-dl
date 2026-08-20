"""Undantag för prenly-dl.

Biblioteksfunktionerna kastar undantag i stället för att kalla sys.exit(),
så att en långlivad process (webbtjänst, schemaläggare) kan fånga felet,
märka jobbet och gå vidare.
"""

from __future__ import annotations


class PrenlyError(Exception):
    """Basklass för alla fel mot Prenly/Textalk."""


class PrenlyAuthError(PrenlyError):
    """Autentiseringen gäller inte längre (JSON-RPC -32001).

    Prenlys auth-token har begränsad livslängd trots vad äldre
    dokumentation påstod. Kräver att användaren hämtar nya uppgifter.
    """


class PrenlySubscriptionError(PrenlyError):
    """Publikationen ingår inte i prenumerationen (JSON-RPC -32006)."""


class PrenlyApiError(PrenlyError):
    """Övriga JSON-RPC-fel."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PrenlyHTTPError(PrenlyError):
    """HTTP-anropet misslyckades (fel statuskod eller nätverksfel)."""

    def __init__(self, status_code: int | None, url: str, message: str = "") -> None:
        text = message or f"HTTP {status_code} för {url}"
        super().__init__(text)
        self.status_code = status_code
        self.url = url


# JSON-RPC-koder som Prenly returnerar och som vi särskiljer.
RPC_NOT_AUTHENTICATED = -32001
RPC_NO_SUBSCRIPTION = -32006


def raise_for_rpc_error(error: dict) -> None:
    """Översätt ett JSON-RPC-felobjekt till rätt undantagstyp."""
    code = error.get("code")
    message = error.get("message", "Okänt fel från Prenly")
    if code == RPC_NOT_AUTHENTICATED:
        raise PrenlyAuthError(message)
    if code == RPC_NO_SUBSCRIPTION:
        raise PrenlySubscriptionError(message)
    raise PrenlyApiError(code, message)
